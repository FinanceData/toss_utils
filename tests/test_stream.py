"""실시간(WebSocket) 계층 테스트 — 로컬 WebSocket 가짜 서버(websockets.serve)로 프로토콜을 재현한다."""
import asyncio
import json
from http import HTTPStatus

import pytest

pytest.importorskip("websockets")
from websockets.asyncio.server import serve  # noqa: E402

from toss_utils import Credentials, ForbiddenError, TossClient  # noqa: E402
from toss_utils.cli import main as cli_main  # noqa: E402
from toss_utils.ratelimit import RateLimiter  # noqa: E402
from toss_utils.stream import RealtimeStream, collect_async, declaration, parse_topic  # noqa: E402
from toss_utils.tools import build_tools  # noqa: E402

from .fake_server import FakeToss  # noqa: E402


async def fast_sleep(_):
    await asyncio.sleep(0)


class FakeWS:
    """토스 WebSocket 서버 동작 재현: Bearer 검증, 선언→ack, PING→pong, 오류 프레임."""

    def __init__(self, rest: FakeToss):
        self.rest = rest
        self.declarations: list[list] = []
        self.auth_seen: list[str] = []
        self.pings = 0
        self.connections = 0
        self.reject: set[str] = set()
        self.http_status: list[int] = []        # 다음 핸드셰이크들에 강제로 돌려줄 HTTP 상태
        self.after_declare = self.push_one_each

    async def process_request(self, connection, request):
        auth = request.headers.get("Authorization", "")
        self.auth_seen.append(auth)
        if self.http_status:
            return connection.respond(HTTPStatus(self.http_status.pop(0)), "nope\n")
        if auth != f"Bearer {self.rest.valid_token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "bad token\n")
        return None

    async def handler(self, ws):
        self.connections += 1
        n = self.connections
        async for raw in ws:
            if raw == "PING":
                self.pings += 1
                await ws.send('{"type":"pong"}')
                continue
            arr = json.loads(raw)
            self.declarations.append(arr)
            rid = next((x["id"] for x in arr if "id" in x and "type" not in x), None)
            keys = [f'{x["type"]}:{c}' for x in arr if "type" in x for c in x["codes"]]
            sub = [k for k in keys if k not in self.reject]
            rej = [{"target": k, "code": "stock-not-found", "message": "x"} for k in keys if k in self.reject]
            await ws.send(json.dumps({"type": "subscriptions", "id": rid, "subscribed": sub, "rejected": rej}))
            await self.after_declare(ws, n, sub)

    @staticmethod
    async def push_one_each(ws, n, sub):
        for k in sub:
            await ws.send(json.dumps({"type": "message", "topic": k, "data": {"conn": n, "price": "100"}}))


def make_client(rest, tmp_path):
    return TossClient(Credentials("tsck_live_test", "secret"), transport=rest.transport(), home=tmp_path,
                      account_seq=1, sleep=lambda s: None, limiter=RateLimiter(sleep=lambda s: None))


@pytest.fixture
def setup(tmp_path):
    rest = FakeToss()
    rest.route("/api/v1/orders", {"orders": [{"orderId": "o-1", "status": "PENDING"}], "hasNext": False})
    rest.route("/api/v1/conditional-orders", {"conditionalOrders": [], "hasNext": False})
    return rest, FakeWS(rest), make_client(rest, tmp_path)


def run_with_server(fws, fn):
    async def main():
        async with serve(fws.handler, "127.0.0.1", 0, process_request=fws.process_request) as server:
            port = server.sockets[0].getsockname()[1]
            return await fn(f"ws://127.0.0.1:{port}/ws/v1")
    return asyncio.run(main())


# ---------------------------------------------------------------- 토픽·선언 형식
def test_parse_topic_and_declaration():
    assert parse_topic("trade:kr:005930", None) == ("trade:kr", "005930")
    assert parse_topic("orderbook:us:aapl", None) == ("orderbook:us", "AAPL")
    assert parse_topic("order", 3) == ("personal:order", "3")
    assert parse_topic("cond", 3) == ("personal:conditional-order", "3")
    with pytest.raises(ValueError):
        parse_topic("order", None)            # 계좌 없으면 주문 채널 불가
    with pytest.raises(ValueError):
        parse_topic("quote:kr:005930", 1)
    d = declaration([("trade:kr", "005930"), ("trade:kr", "000660"), ("personal:order", "1")], "req-1")
    assert d == [{"id": "req-1"}, {"type": "personal:order", "codes": ["1"]},
                 {"type": "trade:kr", "codes": ["000660", "005930"]}]


def test_topic_limit_is_enforced_locally(setup):
    rest, fws, c = setup
    with pytest.raises(ValueError, match="100"):
        RealtimeStream(c, [f"trade:kr:{i:06d}" for i in range(101)])


# ---------------------------------------------------------------- 연결·수신
def test_declares_and_receives_messages_with_resync(setup):
    rest, fws, c = setup
    out = run_with_server(fws, lambda url: collect_async(
        c, ["trade:kr:005930", "orderbook:us:aapl", "order"], seconds=3, max_messages=3, url=url))
    assert len(out["messages"]) == 3
    assert {m["topic"] for m in out["messages"]} == {"trade:kr:005930", "orderbook:us:AAPL", "personal:order:1"}
    decl = fws.declarations[0]
    assert decl[0] == {"id": "req-1"} and {"type": "personal:order", "codes": ["1"]} in decl
    assert fws.auth_seen[0] == f"Bearer {rest.valid_token}"
    assert out["resync"]["orders"][0]["orderId"] == "o-1"      # 주문 채널 → 연결 직후 REST 스냅샷
    assert out["statuses"][0]["state"] == "connected"


def test_ping_keepalive(setup):
    rest, fws, c = setup

    async def idle(ws, n, sub):
        pass
    fws.after_declare = idle
    run_with_server(fws, lambda url: collect_async(c, ["trade:kr:005930"], seconds=0.5, url=url,
                                                   ping_interval=0.05))
    assert fws.pings >= 3          # 텍스트 "PING" 을 주기적으로 보냄 (서버는 pong)


def test_auth_revoked_reconnects_with_new_token_and_redeclares(setup):
    rest, fws, c = setup

    async def revoke_then_push(ws, n, sub):
        if n == 1:
            rest.issued.append("tok-external")              # 다른 곳에서 재발급 → 지금 토큰 무효
            await ws.send(json.dumps({"type": "error", "error": {"code": "auth-revoked", "message": "x"}}))
            await ws.close()
        else:
            await FakeWS.push_one_each(ws, n, sub)
    fws.after_declare = revoke_then_push
    out = run_with_server(fws, lambda url: collect_async(c, ["trade:kr:005930"], seconds=3, max_messages=1,
                                                         url=url, sleep=fast_sleep))
    assert [s["state"] for s in out["statuses"]] == ["connected", "reconnecting", "connected"]
    assert out["statuses"][1]["reason"] == "auth-revoked"
    assert len(fws.declarations) == 2                                  # 재연결 후 재선언
    assert fws.auth_seen[-1] == f"Bearer {rest.valid_token}" != fws.auth_seen[0]
    assert out["messages"][0]["data"]["conn"] == 2


def test_rejected_topic_is_dropped_from_next_declaration(setup):
    rest, fws, c = setup
    fws.reject = {"trade:kr:999999"}

    async def shutdown_first(ws, n, sub):
        if n == 1:
            await ws.send(json.dumps({"type": "error", "error": {"code": "server-shutdown", "message": "deploy"}}))
            await ws.close()
        else:
            await FakeWS.push_one_each(ws, n, sub)
    fws.after_declare = shutdown_first
    s_holder = {}

    async def go(url):
        s = RealtimeStream(c, ["trade:kr:005930", "trade:kr:999999"], url=url, sleep=fast_sleep)
        s_holder["s"] = s
        agen = s.events()
        try:
            async for ev in agen:
                if ev["type"] == "message":
                    return ev
        finally:
            await agen.aclose()
    ev = run_with_server(fws, go)
    codes2 = [x["codes"] for x in fws.declarations[1] if x.get("type") == "trade:kr"][0]
    assert codes2 == ["005930"] and s_holder["s"].rejected == {"trade:kr:999999": "stock-not-found"}
    assert ev["topic"] == "trade:kr:005930"


def test_handshake_401_recovers_token_once(setup):
    rest, fws, c = setup
    fws.http_status = [401]
    out = run_with_server(fws, lambda url: collect_async(c, ["trade:kr:005930"], seconds=3, max_messages=1,
                                                         url=url, sleep=fast_sleep))
    assert len(out["messages"]) == 1 and len(rest.issued) == 2       # 401 → 토큰 재발급 후 연결 성공


def test_handshake_403_raises_forbidden(setup):
    rest, fws, c = setup
    fws.http_status = [403]
    with pytest.raises(ForbiddenError):
        run_with_server(fws, lambda url: collect_async(c, ["trade:kr:005930"], seconds=3, url=url))


def test_slow_consumer_does_not_lose_order_events(setup):
    rest, fws, c = setup

    async def burst(ws, n, sub):
        for i in range(300):
            await ws.send(json.dumps({"type": "message", "topic": "personal:order:1", "data": {"i": i}}))
    fws.after_declare = burst

    async def go(url):
        got = []
        agen = RealtimeStream(c, ["order"], url=url, resync=False).events()
        try:
            async for ev in agen:
                if ev["type"] == "message":
                    got.append(ev["data"]["i"])
                    await asyncio.sleep(0.001)      # 느린 소비자
                    if len(got) == 300:
                        return got
        finally:
            await agen.aclose()
    assert run_with_server(fws, go) == list(range(300))


# ---------------------------------------------------------------- CLI · MCP 도구
def test_cli_stream_prints_ndjson(setup, monkeypatch, capsys):
    rest, fws, c = setup

    async def go(url):
        monkeypatch.setenv("TOSS_WS_URL", url)
        return await asyncio.to_thread(cli_main, ["stream", "trade:kr:005930", "trade:kr:000660", "--count", "2"],
                                       client_factory=lambda: c)
    assert run_with_server(fws, go) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert [x["type"] for x in lines].count("message") == 2 and lines[0]["type"] == "status"


def test_mcp_sample_realtime_tool(setup, monkeypatch):
    rest, fws, c = setup
    tool = {t.name: t for t in build_tools(lambda: c)}["sample_realtime"]
    assert tool.kind == "read"

    async def go(url):
        monkeypatch.setenv("TOSS_WS_URL", url)
        return await asyncio.to_thread(tool.fn, topics=["trade:kr:005930"], seconds=2, max_messages=1)
    out = run_with_server(fws, go)
    assert out["ok"] and out["result"]["messages"][0]["topic"] == "trade:kr:005930"

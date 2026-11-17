"""실시간(WebSocket) 계층.

공식 AsyncAPI(spec/asyncapi.json, v1.2.3) 기준 규칙
- 주소 wss://openapi-ws.tossinvest.com/ws/v1, 핸드셰이크에 REST 와 같은 `Authorization: Bearer` (허용 IP 동일)
- 구독은 **선언형 full-replace**: JSON 배열 1개가 곧 현재 구독 전체. 빈 배열 = 전체 해제.
- 수신 프레임: subscriptions(ack) / message(topic+data) / error / pong
- 클라이언트가 180초간 아무것도 안 보내면 서버가 끊음 → 텍스트 `PING` 을 60초마다
- 한도: 계정당 동시 연결 2개(3번째가 붙으면 가장 오래된 연결이 끊김), 연결당 구독 100건, 선언 5회/초
- auth-revoked(토큰 무효) → 새 토큰으로 재연결, server-shutdown → 재연결 후 재선언
- 시세는 LOSSY(유실 가능), 주문·조건주문은 연결 안에서만 LOSSLESS → 재연결 후 REST 로 재동기화
- 주문 채널은 수신이 2초 이상 막히면 서버가 끊음 → 수신 전용 태스크가 큐로 빨리 옮긴다

이 모듈은 위 규칙을 지키는 재연결형 비동기 스트림과, 몇 초간 모아 돌려주는 동기 헬퍼를 제공한다.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import random
import time
from typing import TYPE_CHECKING, AsyncIterator, Iterable

from .errors import ForbiddenError, TossError

if TYPE_CHECKING:
    from .client import TossClient

WS_URL = os.environ.get("TOSS_WS_URL", "wss://openapi-ws.tossinvest.com/ws/v1")
MAX_TOPICS = 100
DECLARE_MIN_INTERVAL = 0.2          # 선언 5회/초
MARKET_TYPES = {"trade:kr", "trade:us", "orderbook:kr", "orderbook:us"}
PERSONAL_TYPES = {"personal:order", "personal:conditional-order"}
ALIASES = {"order": "personal:order", "orders": "personal:order",
           "cond": "personal:conditional-order", "conditional": "personal:conditional-order"}


def parse_topic(spec: str, account_seq: int | None) -> tuple[str, str]:
    """'trade:kr:005930' · 'orderbook:us:AAPL' · 'order' · 'cond' · 'personal:order:3' → (type, code)"""
    s = spec.strip()
    if s in ALIASES:
        if account_seq is None:
            raise ValueError("주문·조건주문 채널은 계좌가 필요합니다 (`toss account use <seq>`).")
        return ALIASES[s], str(account_seq)
    for t in sorted(MARKET_TYPES | PERSONAL_TYPES, key=len, reverse=True):
        if s.startswith(t + ":"):
            code = s[len(t) + 1:]
            if not code:
                break
            return t, code.upper() if t.endswith(":us") else code
    raise ValueError(f"알 수 없는 토픽: {spec!r}. 예: trade:kr:005930, orderbook:us:AAPL, order, cond")


def declaration(topics: Iterable[tuple[str, str]], req_id: str | None = None) -> list[dict]:
    """(type, code) 집합 → 서버에 보낼 선언 배열 (같은 type 끼리 codes 로 묶음)."""
    grouped: dict[str, list[str]] = {}
    for t, c in sorted(set(topics)):
        grouped.setdefault(t, []).append(c)
    arr: list[dict] = [{"id": req_id}] if req_id else []
    arr += [{"type": t, "codes": cs} for t, cs in grouped.items()]
    return arr


class _Closed:
    def __init__(self, exc: BaseException | None):
        self.exc = exc


class RealtimeStream:
    """재연결·재선언·PING·재동기화를 처리하는 실시간 스트림.

        async for ev in RealtimeStream(client, ["trade:kr:005930", "order"]).events():
            ...

    ev["type"]:
      message        실시간 데이터 {topic, data}
      subscriptions  구독 ack {subscribed, rejected}  (거부된 토픽은 이후 선언에서 자동 제외)
      status         {state: connected|reconnecting, reason, attempt}
      resync         (주문 채널 구독 시) 연결 직후 REST 스냅샷 {orders, conditionalOrders}
      error          연결은 유지되는 오류 {code, message}
    """

    def __init__(self, client: "TossClient", topics: Iterable[str], *, url: str | None = None,
                 ping_interval: float = 60.0, max_backoff: float = 30.0, resync: bool = True,
                 max_reconnects: int | None = None, sleep=asyncio.sleep):
        """sleep: 재연결 백오프·rate-limit 대기에만 쓰인다 (테스트에서 즉시 진행하도록 주입 가능)."""
        self.c = client
        self.url = url or os.environ.get("TOSS_WS_URL", WS_URL)
        self.ping_interval = ping_interval
        self.max_backoff = max_backoff
        self.resync = resync
        self.max_reconnects = max_reconnects
        self._sleep = sleep
        self.topics: set[tuple[str, str]] = set()
        self.rejected: dict[str, str] = {}
        self._ws = None
        self._last_declare = 0.0
        self._req = 0
        self.set_topics_now(topics)

    # ------------------------------------------------------------------ 구독 관리
    def set_topics_now(self, topics: Iterable[str]) -> None:
        parsed = {parse_topic(t, self.c.account_seq) for t in topics}
        if len(parsed) > MAX_TOPICS:
            raise ValueError(f"구독은 연결당 {MAX_TOPICS}건까지입니다 (요청 {len(parsed)}건).")
        self.topics = parsed

    async def set_topics(self, topics: Iterable[str]) -> None:
        """구독 목록을 통째로 바꾸고(full-replace) 연결 중이면 즉시 재선언."""
        self.set_topics_now(topics)
        if self._ws is not None:
            await self._declare()

    @property
    def has_personal(self) -> bool:
        return any(t in PERSONAL_TYPES for t, _ in self.topics)

    async def _declare(self) -> None:
        wait = DECLARE_MIN_INTERVAL - (time.monotonic() - self._last_declare)
        if wait > 0:
            await asyncio.sleep(wait)
        self._req += 1
        await self._ws.send(json.dumps(declaration(self.topics, f"req-{self._req}"), ensure_ascii=False))
        self._last_declare = time.monotonic()

    # ------------------------------------------------------------------ 연결
    async def _open(self, token: str):
        from websockets.asyncio.client import connect
        return await connect(self.url, additional_headers={"Authorization": f"Bearer {token}"},
                             user_agent_header="toss-utils-py", ping_interval=None, max_size=2 ** 22)

    async def _keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            await ws.send("PING")      # JSON 이 아닌 대문자 텍스트 프레임

    @staticmethod
    async def _reader(ws, q: asyncio.Queue) -> None:
        """수신만 하는 태스크: 소비자가 느려도 서버 쪽 backpressure(2초) 에 걸리지 않게 큐로 옮긴다."""
        try:
            async for raw in ws:
                q.put_nowait(raw)
            q.put_nowait(_Closed(None))
        except Exception as e:  # noqa: BLE001
            q.put_nowait(_Closed(e))

    def _resync_snapshot(self) -> dict:
        snap: dict = {"type": "resync", "ts": time.time()}
        if any(t == "personal:order" for t, _ in self.topics):
            snap["orders"] = self.c.orders.list("OPEN").get("orders", [])
        if any(t == "personal:conditional-order" for t, _ in self.topics):
            snap["conditionalOrders"] = self.c.orders.conditional_list("OPEN").get("conditionalOrders", [])
        return snap

    async def events(self) -> AsyncIterator[dict]:
        from websockets.exceptions import InvalidStatus

        token = await asyncio.to_thread(self.c.tokens.get)
        attempt, reconnects, handshake_401 = 0, 0, False
        reason = "start"
        while True:
            if attempt:
                if self.max_reconnects is not None and reconnects >= self.max_reconnects:
                    return
                reconnects += 1
                delay = min(self.max_backoff, 2 ** (attempt - 1)) + random.uniform(0, 0.3)
                yield {"type": "status", "state": "reconnecting", "reason": reason, "attempt": attempt,
                       "delay_sec": round(delay, 2)}
                await self._sleep(delay)
            try:
                ws = await self._open(token)
            except InvalidStatus as e:
                code = e.response.status_code
                if code == 401 and not handshake_401:
                    handshake_401 = True
                    token = await asyncio.to_thread(self.c.tokens.recover, token)
                    attempt, reason = max(attempt, 1), "handshake-401"
                    continue
                if code == 403:
                    raise ForbiddenError(403, "ip-not-allowed",
                                         "웹소켓 연결이 거부되었습니다. 허용 IP 를 확인하세요 (WTS 설정 > Open API).")
                attempt, reason = attempt + 1, f"handshake-{code}"
                continue
            except OSError as e:
                attempt, reason = attempt + 1, f"network: {e}"
                continue

            handshake_401 = False
            self._ws = ws
            q: asyncio.Queue = asyncio.Queue()
            reader = asyncio.create_task(self._reader(ws, q))
            pinger = asyncio.create_task(self._keepalive(ws))
            try:
                await self._declare()
                yield {"type": "status", "state": "connected", "attempt": attempt}
                if self.resync and self.has_personal:
                    yield await asyncio.to_thread(self._resync_snapshot)
                while True:
                    item = await q.get()
                    if isinstance(item, _Closed):
                        attempt, reason = attempt + 1, f"closed: {item.exc!r}" if item.exc else "closed"
                        break
                    try:
                        frame = json.loads(item)
                    except (TypeError, ValueError):
                        continue
                    kind = frame.get("type")
                    if kind == "pong":
                        continue
                    if kind == "subscriptions":
                        attempt = 0          # 정상 연결 확인 → 백오프 초기화
                        for r in frame.get("rejected") or []:
                            self.rejected[r.get("target", "?")] = r.get("code", "?")
                            self._drop(r.get("target", ""))
                        yield frame
                        continue
                    if kind == "error":
                        err = frame.get("error") or {}
                        code = err.get("code")
                        if code == "auth-revoked":
                            token = await asyncio.to_thread(self.c.tokens.recover, token)
                            attempt, reason = max(attempt, 1), "auth-revoked"
                            break
                        if code == "server-shutdown":
                            attempt, reason = max(attempt, 1), "server-shutdown"
                            break
                        if code == "rate-limit-exceeded":
                            await self._sleep(1.0)
                            await self._declare()
                        yield {"type": "error", "code": code, "message": err.get("message"), "id": frame.get("id")}
                        continue
                    yield frame
            finally:
                self._ws = None
                pinger.cancel()
                reader.cancel()
                await ws.close()      # 재연결 전에 이전 연결을 먼저 닫는다 (동시 연결 2개 한도)

    def _drop(self, target: str) -> None:
        """거부된 full key('trade:kr:XXXX')를 이후 선언에서 뺀다 (그대로 두면 재선언마다 같은 이유로 거부됨)."""
        for t, c in list(self.topics):
            if f"{t}:{c}" == target:
                self.topics.discard((t, c))


async def collect_async(client: "TossClient", topics: Iterable[str], seconds: float = 5.0,
                        max_messages: int = 50, **kw) -> dict:
    """seconds 동안(또는 max_messages 개가 모일 때까지) 받은 이벤트를 모아서 돌려준다."""
    stream = RealtimeStream(client, topics, **kw)
    out = {"topics": [f"{t}:{c}" for t, c in sorted(stream.topics)], "messages": [], "acks": [],
           "statuses": [], "errors": [], "resync": None}

    async def run():
        agen = stream.events()
        try:
            await _consume(agen)
        finally:
            await agen.aclose()      # 연결을 확실히 닫는다 (동시 연결 2개 한도)

    async def _consume(agen):
        async for ev in agen:
            k = ev.get("type")
            if k == "message":
                out["messages"].append({"topic": ev.get("topic"), "data": ev.get("data")})
                if len(out["messages"]) >= max_messages:
                    return
            elif k == "subscriptions":
                out["acks"].append({"subscribed": ev.get("subscribed"), "rejected": ev.get("rejected")})
            elif k == "status":
                out["statuses"].append(ev)
            elif k == "resync":
                out["resync"] = ev
            elif k == "error":
                out["errors"].append(ev)

    try:
        await asyncio.wait_for(run(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    out["seconds"] = seconds
    return out


def collect(client: "TossClient", topics: Iterable[str], seconds: float = 5.0, max_messages: int = 50,
            **kw) -> dict:
    """동기 코드(CLI·MCP 도구)용. 이미 이벤트 루프가 돌고 있는 스레드에서도 안전하게 별도 스레드에서 실행."""
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, collect_async(client, topics, seconds, max_messages, **kw)).result()


def ensure_available() -> None:
    try:
        import websockets  # noqa: F401
    except ImportError:
        raise TossError("실시간 기능에는 websockets 패키지가 필요합니다: uv tool install --editable '.[ws]' --force")

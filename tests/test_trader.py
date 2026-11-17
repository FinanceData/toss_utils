"""주문 계층(안전장치) 테스트 — 가짜 서버만 사용, 실제 주문 없음."""
import json
import time

import httpx
import pytest

from toss_utils import Credentials, SafetyError, TossClient
from toss_utils.cli import main as cli_main
from toss_utils.ratelimit import RateLimiter
from toss_utils.safety import Policy

from .fake_server import FakeToss, body_of

CREDS = Credentials("tsck_live_test", "secret")

STOCKS = {
    "005930": {"symbol": "005930", "name": "삼성전자", "currency": "KRW", "status": "ACTIVE", "market": "KOSPI",
               "koreanMarketDetail": {"krxTradingSuspended": False, "liquidationTrading": False}},
    "AAPL": {"symbol": "AAPL", "name": "애플", "currency": "USD", "status": "ACTIVE", "market": "NASDAQ",
             "koreanMarketDetail": None},
}
PRICES = {"005930": "70000", "AAPL": "200"}


@pytest.fixture
def env(tmp_path):
    f = FakeToss()
    state = {"warnings": [], "bp": {"KRW": "1000000", "USD": "500"}, "sellable": "10", "open": [], "posted": []}

    def sym(req):
        return req.url.params.get("symbol") or req.url.params.get("symbols")

    f.route("/api/v1/stocks", lambda r: [STOCKS[s] for s in sym(r).split(",") if s in STOCKS])
    f.route("/api/v1/prices", lambda r: [{"symbol": s, "lastPrice": PRICES[s], "currency": "KRW"} for s in sym(r).split(",")])
    f.route("/api/v1/exchange-rate", {"rate": "1400", "midRate": "1395"})
    f.route("/api/v1/price-limits", {"upperLimitPrice": "91000", "lowerLimitPrice": "49000", "currency": "KRW"})
    f.route("/api/v1/commissions", [{"marketCountry": "KR", "commissionRate": "0.00015"},
                                    {"marketCountry": "US", "commissionRate": "0.001"}])
    f.route("/api/v1/buying-power", lambda r: {"currency": r.url.params["currency"],
                                               "cashBuyingPower": state["bp"][r.url.params["currency"]]})
    f.route("/api/v1/sellable-quantity", lambda r: {"sellableQuantity": state["sellable"]})
    for s in STOCKS:
        f.route(f"/api/v1/stocks/{s}/warnings", lambda r: state["warnings"])
    f.route("/api/v1/conditional-orders", lambda r: (state["posted"].append(body_of(r)) or
                                                     {"conditionalOrderId": "c-1", "clientOrderId": body_of(r)["clientOrderId"]})
            if r.method == "POST" else {"conditionalOrders": [], "nextCursor": None, "hasNext": False})

    def orders(r):
        if r.method == "POST":
            b = body_of(r)
            state["posted"].append(b)
            return {"orderId": f"o-{len(state['posted'])}", "clientOrderId": b.get("clientOrderId")}
        return {"orders": state["open"], "nextCursor": None, "hasNext": False}

    f.route("/api/v1/orders", orders)
    f.route("/api/v1/orders/o-9", {"orderId": "o-9", "symbol": "005930", "side": "BUY", "status": "PENDING",
                                   "price": "60000", "quantity": "1", "execution": {"filledQuantity": "0"}})
    f.route("/api/v1/orders/o-9/modify", lambda r: (state["posted"].append(body_of(r)) or {"orderId": "o-10"}))
    f.route("/api/v1/orders/o-9/cancel", {"orderId": "o-11"})

    c = TossClient(CREDS, transport=f.transport(), home=tmp_path, account_seq=1,
                   sleep=lambda s: None, limiter=RateLimiter(sleep=lambda s: None))
    return f, state, c, tmp_path


def failed(pv):
    return {x["name"] for x in pv["checks"] if not x["ok"]}


def test_prepare_does_not_send_and_submit_sends_exact_body(env):
    f, st, c, home = env
    pv = c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")
    assert pv["ok"], pv["checks"]
    assert st["posted"] == []                       # 미리보기만으로는 서버에 주문이 가지 않음
    assert pv["estimated"]["krw"] == "60000"
    res = c.trader.submit(pv["confirm_id"])
    assert st["posted"] == [pv["body"]] and res["result"]["orderId"] == "o-1"
    assert st["posted"][0]["clientOrderId"] == pv["confirm_id"]
    with pytest.raises(SafetyError):                # 같은 확인 ID 재사용 불가
        c.trader.submit(pv["confirm_id"])


def test_per_order_and_daily_caps(env):
    f, st, c, home = env
    pv = c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="2", price="60000")   # 12만 > 10만
    assert not pv["ok"] and "max_order_krw" in failed(pv) and "confirm_id" not in pv
    Policy(max_order_krw=100_000, max_daily_krw=100_000).save(home)
    c.trader.submit(c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")["confirm_id"])
    pv = c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")  # 누적 12만 > 10만
    assert "max_daily_krw" in failed(pv)


def test_market_orders_blocked_by_default_and_usd_conversion(env):
    f, st, c, home = env
    pv = c.trader.prepare_order("AAPL", "BUY", "MARKET", amount="10")
    assert "market_order_allowed" in failed(pv)
    assert pv["estimated"]["krw"] == "14000"        # $10 × 1400
    Policy(allow_market_orders=True).save(home)
    pv = c.trader.prepare_order("AAPL", "BUY", "MARKET", amount="10")
    assert pv["ok"] and pv["body"]["orderAmount"] == "10" and "quantity" not in pv["body"]


def test_kill_switch_blocks_submit_but_allows_cancel(env):
    f, st, c, home = env
    pv = c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")
    (home / "KILL").write_text("x")
    with pytest.raises(SafetyError):
        c.trader.submit(pv["confirm_id"])
    assert st["posted"] == []
    assert c.trader.cancel("o-9")["result"]["orderId"] == "o-11"


def test_confirmation_expires(env):
    f, st, c, home = env
    Policy(confirm_ttl_sec=0).save(home)
    pv = c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")
    time.sleep(0.01)
    with pytest.raises(SafetyError, match="유효 시간"):
        c.trader.submit(pv["confirm_id"])
    assert st["posted"] == []


def test_order_checks(env):
    f, st, c, home = env
    assert "price_in_limits" in failed(c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="95000"))
    st["warnings"] = [{"warningType": "VI_DYNAMIC"}]
    assert "no_buy_warnings" in failed(c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000"))
    st["warnings"] = []
    st["bp"]["KRW"] = "100"
    assert "buying_power" in failed(c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000"))
    st["sellable"] = "0"
    assert "sellable_quantity" in failed(c.trader.prepare_order("005930", "SELL", "LIMIT", quantity="1", price="60000"))
    st["sellable"] = "10"
    st["open"] = [{"orderId": "o-5", "side": "BUY", "symbol": "005930"}]
    assert "no_opposite_pending" in failed(c.trader.prepare_order("005930", "SELL", "LIMIT", quantity="1", price="60000"))
    assert "price_precision" in failed(c.trader.prepare_order("AAPL", "BUY", "LIMIT", quantity="1", price="10.123"))


def test_input_validation(env):
    f, st, c, home = env
    t = c.trader
    with pytest.raises(ValueError):
        t.prepare_order("005930", "BUY", "LIMIT", quantity="1")                  # 지정가에 가격 없음
    with pytest.raises(ValueError):
        t.prepare_order("005930", "BUY", "LIMIT", quantity="1.5", price="60000")  # 국내 소수점
    with pytest.raises(ValueError):
        t.prepare_order("005930", "BUY", "MARKET", amount="10")                  # 금액 주문은 미국만
    with pytest.raises(ValueError):
        t.prepare_order("AAPL", "BUY", "LIMIT", quantity="1", price="10", time_in_force="OPG")


O9 = {"orderId": "o-9", "symbol": "005930", "side": "BUY", "status": "PENDING", "price": "60000",
      "quantity": "1", "execution": {"filledQuantity": "0"}}


def test_modify_and_cancel(env):
    f, st, c, home = env
    st["open"] = [O9]
    pv = c.trader.prepare_modify("o-9", price="61000")
    assert pv["ok"] and pv["body"] == {"orderType": "LIMIT", "price": "61000", "quantity": "1"}
    assert pv["estimated"]["daily_added_krw"] == "1000"     # 증가분만 일일 한도에 반영
    res = c.trader.submit(pv["confirm_id"])
    assert res["result"]["orderId"] == "o-10"


def test_conditional_oco_rules_and_submit(env):
    f, st, c, home = env
    t = c.trader
    with pytest.raises(ValueError, match="현재가"):
        t.prepare_conditional("005930", "OCO", "1", {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "65000"},
                              {"orderSide": "SELL", "triggerPrice": "60000", "orderPrice": "60000"})
    pv = t.prepare_conditional("005930", "OCO", "1",
                               {"orderSide": "SELL", "triggerPrice": "80000", "orderPrice": "80000"},
                               {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"})
    assert pv["ok"], pv["checks"]
    t.submit(pv["confirm_id"])
    assert st["posted"][-1]["type"] == "OCO" and st["posted"][-1]["second"]["orderPrice"] == "64900"


def test_order_create_retried_with_same_client_order_id_on_network_error(env):
    f, st, c, home = env
    pv = c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")
    real = f.handler
    calls = {"n": 0}

    def flaky(req):
        if req.url.path == "/api/v1/orders" and req.method == "POST":
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom")
        return real(req)

    c.http._transport = httpx.MockTransport(flaky)
    c.trader.submit(pv["confirm_id"])
    assert calls["n"] == 2 and st["posted"][0]["clientOrderId"] == pv["confirm_id"]


def test_cli_dry_run_then_submit(env, capsys):
    f, st, c, home = env
    factory = lambda: TossClient(CREDS, transport=f.transport(), home=home, account_seq=1,
                                 sleep=lambda s: None, limiter=RateLimiter(sleep=lambda s: None))
    assert cli_main(["order", "buy", "005930", "1", "--limit", "60000"], client_factory=factory) == 0
    out = json.loads(capsys.readouterr().out)
    assert st["posted"] == [] and "toss order submit" in out["next"]
    assert cli_main(["order", "submit", out["confirm_id"]], client_factory=factory) == 0
    assert len(st["posted"]) == 1
    capsys.readouterr()
    assert cli_main(["order", "buy", "005930", "5", "--limit", "60000"], client_factory=factory) == 3  # 한도 초과
    assert json.loads(capsys.readouterr().err)["error"]["type"] == "safety"


# ---------------------------------------------------------------- 주문 ID 계보 추적
def _lineage_env(env):
    """서버 동작 재현: 정정·취소 때마다 새 ID, 이전 ID 의 상세 조회는 PENDING 으로 갱신되지 않음."""
    f, st, c, home = env
    details = {}

    def make(oid, price, status):
        details[oid] = {"orderId": oid, "symbol": "005930", "side": "BUY", "status": status, "price": price,
                        "quantity": "1", "execution": {"filledQuantity": "0"}}

    make("o-A", "60000", "PENDING")
    st["open"] = [details["o-A"]]

    def detail_route(oid):
        return lambda r: details[oid]

    def modify_route(r):
        make("o-B", body_of(r)["price"], "PENDING")      # 새 ID, o-A 상세는 그대로 PENDING (stale)
        st["open"] = [details["o-B"]]
        f.route("/api/v1/orders/o-B", detail_route("o-B"))
        return {"orderId": "o-B"}

    def cancel_route(r):
        make("o-C", "61000", "CANCELED")
        st["open"] = []
        st["closed"] = [dict(details["o-B"], status="CANCELED")]   # 목록에는 정정 ID 로 CANCELED
        f.route("/api/v1/orders/o-C", detail_route("o-C"))
        return {"orderId": "o-C"}

    def orders(r):
        if r.method == "POST":
            return {"orderId": "o-A", "clientOrderId": body_of(r)["clientOrderId"]}
        key = "closed" if r.url.params.get("status") == "CLOSED" else "open"
        return {"orders": st.get(key, []), "nextCursor": None, "hasNext": False}

    f.route("/api/v1/orders", orders)
    f.route("/api/v1/orders/o-A", detail_route("o-A"))
    f.route("/api/v1/orders/o-A/modify", modify_route)
    f.route("/api/v1/orders/o-B/cancel", cancel_route)
    return f, st, c, home, details


def test_track_follows_modify_and_cancel(env):
    f, st, c, home, details = _lineage_env(env)
    from toss_utils.tracking import track
    c.trader.submit(c.trader.prepare_order("005930", "BUY", "LIMIT", quantity="1", price="60000")["confirm_id"])
    res = c.trader.submit(c.trader.prepare_modify("o-A", price="61000")["confirm_id"])
    assert res["latestOrderId"] == "o-B"

    t = track(c, "o-A")                               # 예전 ID 로 조회해도
    assert t["latestOrderId"] == "o-B" and t["status"] == "PENDING" and t["order"]["price"] == "61000"
    assert [x["id"] for x in t["chain"]] == ["o-A", "o-B"]

    out = c.trader.cancel("o-A")                      # 예전 ID 로 취소해도 최신 ID(o-B)를 취소
    assert out["canceled"] == "o-B" and out["latestOrderId"] == "o-C"
    for oid in ("o-A", "o-B", "o-C"):
        t = track(c, oid)
        assert t["status"] == "CANCELED" and t["latestOrderId"] == "o-C", (oid, t)
    assert details["o-A"]["status"] == "PENDING"     # 서버의 예전 ID 상세는 여전히 stale
    with pytest.raises(ValueError, match="이미 취소"):
        c.trader.cancel("o-B")


def test_track_flags_stale_detail_without_lineage(env):
    f, st, c, home, details = _lineage_env(env)
    from toss_utils.tracking import track
    st["open"] = []                                   # 앱에서 정정·취소됨 (계보 기록 없음)
    st["closed"] = [{"orderId": "o-X", "side": "BUY", "price": "59000", "quantity": "1", "status": "CANCELED"}]
    t = track(c, "o-A")
    assert t["status"] == "UNKNOWN" and t["warnings"] and t["recentClosedForSymbol"][0]["orderId"] == "o-X"
    with pytest.raises(ValueError, match="정정할 수 없는"):
        c.trader.prepare_modify("o-A", price="61000")

"""`toss` CLI — 라이브러리의 얇은 래퍼. 주문·조건주문은 기본이 미리보기(dry-run)이고 --yes 또는 submit 으로만 전송.
 출력은 항상 JSON(stdout), 오류는 JSON(stderr) + 종료 코드.

종료 코드: 0 성공 · 1 설정 오류 · 2 요청/인증 오류(4xx) · 3 안전장치 거부(예약) · 4 rate limit · 5 서버/네트워크 오류
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import islice

import httpx

from . import __version__
from .client import TossClient
from .config import Settings, load_dotenv, toss_home
from dataclasses import asdict

from .errors import ConfigError, RateLimitError, SafetyError, ServerError, TossAPIError
from .safety.policy import Ledger, Policy, is_killed, kill_switch_path
from .tracking import Lineage, track

EXIT_OK, EXIT_CONFIG, EXIT_REQUEST, EXIT_SAFETY, EXIT_RATE, EXIT_SERVER = 0, 1, 2, 3, 4, 5


def _out(obj, compact: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=None if compact else 2, default=str))


def _err(obj) -> None:
    print(json.dumps({"error": obj}, ensure_ascii=False), file=sys.stderr)


def _syms(values: list[str]) -> list[str]:
    return [s for v in values for s in v.split(",") if s]


# --------------------------------------------------------------------------- handlers
def h_auth(c: TossClient, a):
    if a.action == "status":
        st = c.tokens.status()
        try:
            st["public_ip"] = httpx.get("https://api.ipify.org", timeout=5).text.strip()
        except httpx.HTTPError:
            st["public_ip"] = None
        st["account_seq"] = c.account_seq
        st["home"] = str(c.home)
        return st
    if a.action == "token":   # 토큰 확보 (캐시 재사용, 없으면 발급)
        c.tokens.get()
        return c.tokens.status()
    if a.action == "refresh":  # 강제 재발급 — 다른 프로세스의 토큰은 무효가 된다
        c.tokens.clear()
        c.tokens.get()
        return c.tokens.status()
    if a.action == "logout":
        c.tokens.clear()
        return {"cleared": True}


def h_quote(c, a):
    m = c.market
    if a.kind == "price":
        return m.prices(_syms(a.symbols))
    sym = a.symbols[0]
    return {"book": m.orderbook, "trades": m.trades, "limits": m.price_limits}[a.kind](sym)


def h_candles(c, a):
    if a.limit > 200:
        return list(c.market.iter_candles(a.symbol, a.interval, a.limit, a.adjusted))
    return c.market.candles(a.symbol, a.interval, a.limit, a.before, a.adjusted)


def h_stock(c, a):
    m = c.market
    if a.action == "info":
        return m.stocks(_syms(a.args))
    if a.action == "warnings":
        return m.warnings(a.args[0])
    if a.action == "list":
        return m.stocks_all(a.args[0].upper(), a.status, a.type, a.common)
    if a.action == "trend":
        kind, sym = a.args[0], a.args[1]
        return m.trend(kind, sym, a.count)


def h_market(c, a):
    m = c.market
    if a.action == "calendar":
        return m.calendar(a.arg or "KR", a.date)
    if a.action == "fx":
        return m.exchange_rate(a.base, a.quote)
    if a.action == "rankings":
        return m.rankings(a.arg or "MARKET_TRADING_AMOUNT", a.country, a.duration, a.exclude_caution, a.count)
    if a.action == "investor-rankings":
        return m.investor_rankings(a.count)
    if a.action == "indices":
        return m.indicator_prices(_syms([a.arg]) if a.arg else ["KOSPI", "KOSDAQ"])
    if a.action == "sectors":
        return m.sectors()
    if a.action == "sector":
        return m.sector(a.arg, a.country)
    if a.action == "sector-stocks":
        return m.sector_stocks(a.arg, a.country)
    if a.action == "sector-rankings":
        return m.sector_rankings(a.type, a.country, a.duration)


def h_account(c, a):
    if a.action == "list":
        return c.account.accounts()
    if a.action == "use":
        seq = int(a.seq)
        valid = {x["accountSeq"] for x in c.account.accounts()}
        if seq not in valid:
            raise ConfigError(f"accountSeq {seq} 는 이 키의 계좌가 아닙니다. 가능한 값: {sorted(valid)}")
        Settings(c.home).set("account_seq", seq)
        return {"account_seq": seq, "saved_to": str(Settings(c.home).path)}


def h_holdings(c, a):
    return c.account.holdings(a.symbol, a.account)


def h_tx(c, a):
    types = _syms([a.type]) if a.type else None
    if a.all:
        return list(islice(c.account.iter_transactions(types, a.from_, a.to, a.account), a.max))
    return c.account.transactions(types, a.from_, a.to, a.cursor, a.limit, a.account)


def h_cash(c, a):
    return c.account.buying_power(a.currency.upper(), a.account)


def h_sellable(c, a):
    return c.account.sellable_quantity(a.symbol, a.account)


def h_commissions(c, a):
    return c.account.commissions(a.account)


def _preview_result(preview):
    """미리보기 결과: 검사 실패면 SafetyError 로 종료코드 3."""
    if not preview["ok"]:
        raise SafetyError("안전장치 검사를 통과하지 못해 주문을 만들지 않았습니다.", preview["checks"], preview)
    return preview


def _maybe_submit(c, a, preview):
    preview = _preview_result(preview)
    if not getattr(a, "yes", False):
        preview["next"] = f"검토 후 전송: toss order submit {preview['confirm_id']}  (또는 같은 명령에 --yes)"
        return preview
    return {"preview": preview, "submit": c.trader.submit(preview["confirm_id"])}


def h_order(c, a):
    o, t = c.orders, c.trader
    act = a.action
    if act == "list":
        if a.closed and a.all:
            return list(islice(o.iter_closed(a.symbol, a.from_, a.to, a.account), a.max))
        return o.list("CLOSED" if a.closed else "OPEN", a.symbol, a.from_, a.to, a.cursor, a.limit, a.account)
    if act == "show":
        d = o.get(a.id, a.account)
        chain = Lineage(c.home).chain(a.id)
        if chain and chain[-1]["id"] != a.id:
            d["_warning"] = (f"이 ID 이후 정정·취소되었습니다. 상세 정보가 갱신되지 않으니 `toss order track {a.id}` "
                             f"를 쓰세요 (최신 ID {chain[-1]['id']}).")
        return d
    if act == "track":
        return track(c, a.id, a.account)
    if act in ("buy", "sell"):
        order_type = "MARKET" if a.market else "LIMIT"
        if not a.market and a.limit_price is None:
            raise ValueError("--limit PRICE(지정가) 또는 --market(시장가) 중 하나를 지정하세요.")
        if a.quantity is None and a.amount is None:
            raise ValueError("수량(QTY) 또는 --amount(미국 시장가 금액) 중 하나를 지정하세요.")
        pv = t.prepare_order(a.symbol, act.upper(), order_type, a.quantity, a.limit_price, a.amount, a.tif, a.account)
        return _maybe_submit(c, a, pv)
    if act == "modify":
        pv = t.prepare_modify(a.id, a.limit_price, a.quantity, "MARKET" if a.market else "LIMIT", a.account)
        return _maybe_submit(c, a, pv)
    if act == "submit":
        return t.submit(a.id)
    if act == "pending":
        return t.pending()
    if act == "discard":
        return {"discarded": t.discard(a.id)}
    if act == "cancel":
        return t.cancel(a.id, a.account)
    if act == "cancel-all":
        if not a.yes:
            targets = o.list("OPEN", a.symbol, account=a.account).get("orders", [])
            return {"would_cancel": [{"orderId": x["orderId"], "symbol": x["symbol"], "side": x["side"],
                                      "price": x.get("price"), "quantity": x["quantity"]} for x in targets],
                    "next": "실제로 취소하려면 --yes 를 붙이세요."}
        return t.cancel_all(a.symbol, a.account)


def h_cond(c, a):
    o, t = c.orders, c.trader
    act = a.action
    if act == "list":
        return o.conditional_list("CLOSED" if a.closed else "OPEN", a.symbol, a.cursor, a.limit, a.account)
    if act == "show":
        return o.conditional_get(a.id, a.account)
    if act == "cancel":
        return t.cancel_conditional(a.id, a.with_order, a.account)
    order_type = "MARKET" if a.market else "LIMIT"

    def leg(side, trig, price):
        if trig is None:
            raise ValueError("감시가(trigger)가 필요합니다.")
        return {"orderSide": side, "triggerPrice": trig, **({"orderPrice": price} if price is not None else {})}

    if act == "single":
        first, second = leg(a.side.upper(), a.trigger, a.price), None
    elif act == "oco":   # 익절(first) + 손절(second), 둘 다 SELL
        first, second = leg("SELL", a.tp_trigger, a.tp_price), leg("SELL", a.sl_trigger, a.sl_price)
    else:                # oto: 매수 후 매도
        first, second = leg("BUY", a.buy_trigger, a.buy_price), leg("SELL", a.sell_trigger, a.sell_price)
    pv = t.prepare_conditional(a.symbol, act.upper(), a.quantity, first, second, order_type, a.expire, a.account)
    return _maybe_submit(c, a, pv)


def h_stream(c, a):
    """실시간 이벤트를 NDJSON(한 줄에 JSON 하나)으로 stdout 에 흘려보낸다."""
    import asyncio
    import time as _time

    from .stream import RealtimeStream, ensure_available
    ensure_available()
    stream = RealtimeStream(c, a.topics, resync=not a.no_resync)
    logf = open(a.log, "a") if a.log else None

    async def run():
        agen = stream.events()
        n = 0
        try:
            async for ev in agen:
                ev.setdefault("ts", _time.time())
                line = json.dumps(ev, ensure_ascii=False)
                print(line, flush=True)
                if logf:
                    logf.write(line + "\n")
                    logf.flush()
                if ev.get("type") == "message":
                    n += 1
                    if a.count and n >= a.count:
                        return
        finally:
            await agen.aclose()

    try:
        asyncio.run(asyncio.wait_for(run(), a.seconds) if a.seconds else run())
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        if logf:
            logf.close()
    return None


def h_record(c, a):
    """WebSocket 연결 1개로 원본 NDJSON.gz + SQLite(WAL) 에 기록. 다른 프로세스는 `toss live` 로 동시에 조회."""
    import asyncio
    import sys as _sys
    from pathlib import Path as _P

    from .recorder import record_async
    from .stream import ensure_available
    ensure_available()

    def progress(ev):
        if ev.get("type") in ("status", "subscriptions", "error"):
            print(json.dumps(ev, ensure_ascii=False), file=_sys.stderr, flush=True)

    try:
        return asyncio.run(record_async(c, a.topics, db=_P(a.db) if a.db else None,
                                        raw_dir=_P(a.raw_dir) if a.raw_dir else None, raw=not a.no_raw,
                                        seconds=a.seconds, max_messages=a.count, book_interval=a.book_interval,
                                        on_event=progress, resync=not a.no_resync))
    except KeyboardInterrupt:
        return {"stopped": "ctrl-c"}


def h_live(c, a):
    from pathlib import Path as _P

    from . import recorder as R
    db = _P(a.db) if a.db else R.default_db()
    if a.action == "status":
        return R.query_status(db)
    if a.action == "orders":
        return R.query_orders(db)
    if not a.symbol:
        raise ValueError(f"사용법: toss live {a.action} SYMBOL")
    sym = a.symbol.upper()
    if a.action == "bars":
        return R.query_bars(db, sym, a.minutes, a.interval)
    if a.action == "book":
        return R.query_book(db, sym)
    if a.action == "trades":
        return R.query_trades(db, sym, a.limit)


def h_archive(c, a):
    from datetime import datetime as _dt
    from pathlib import Path as _P

    from . import recorder as R
    db = _P(a.db) if a.db else R.default_db()
    date = a.date or _dt.now(R.KST).date().isoformat()
    out = _P(a.out) if a.out else R.data_home() / "parquet"
    return R.archive(db, out, date, a.prune_days)


def h_policy(c, a):
    home = c.home
    pol = Policy.load(home)
    if a.action == "show":
        return {"policy": asdict(pol), "kill_switch": is_killed(home), "today_used_krw": str(Ledger(home).today_krw()),
                "file": str(home / "policy.json")}
    if a.action == "set":
        if a.key is None or a.value is None:
            raise ValueError("사용법: toss policy set KEY VALUE")
        pol.set(a.key, a.value)
        pol.save(home)
        return {"policy": asdict(pol)}
    if a.action == "kill":
        kill_switch_path(home).parent.mkdir(parents=True, exist_ok=True)
        kill_switch_path(home).write_text("killed\n")
        return {"kill_switch": True, "note": "새 주문·정정·조건주문 등록이 차단됩니다. 취소는 가능합니다."}
    if a.action == "unkill":
        kill_switch_path(home).unlink(missing_ok=True)
        return {"kill_switch": False}


# --------------------------------------------------------------------------- parser
def _need(a, n, usage):
    if len(a.target) < n:
        raise ValueError(f"사용법: {usage}")


def _order_args(a):
    a.id = a.symbol_arg = a.quantity = None
    if a.action in ("buy", "sell"):
        _need(a, 1, f"toss order {a.action} SYMBOL [QTY] (--limit PRICE | --market) [--amount USD]")
        a.symbol = a.target[0]
        a.quantity = a.target[1] if len(a.target) > 1 else None
    elif a.action in ("show", "track", "modify", "submit", "discard", "cancel"):
        _need(a, 1, f"toss order {a.action} ID")
        a.id = a.target[0]
        if a.action == "modify":
            a.quantity = a.qty_opt


def _cond_args(a):
    a.id = a.quantity = None
    if a.action in ("show", "cancel"):
        _need(a, 1, f"toss cond {a.action} ID")
        a.id = a.target[0]
    elif a.action in ("single", "oco", "oto"):
        _need(a, 2, f"toss cond {a.action} SYMBOL QTY ...")
        a.symbol, a.quantity = a.target[0], a.target[1]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="toss", description="토스증권 Open API CLI")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--compact", action="store_true", help="JSON 한 줄 출력 (에이전트·파이프용)")
    p.add_argument("--env-file", default=None,
                   help="자격증명 .env 경로 (기본: ./.env → ~/.config/toss/.env 순서로 찾음)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def acct(sp):
        sp.add_argument("--account", type=int, help="accountSeq (미지정 시 기본 계좌)")

    s = sub.add_parser("auth", help="토큰 상태·발급·삭제")
    s.add_argument("action", choices=["status", "token", "refresh", "logout"])
    s.set_defaults(fn=h_auth)

    s = sub.add_parser("quote", help="현재가·호가·체결·상하한가")
    s.add_argument("kind", choices=["price", "book", "trades", "limits"])
    s.add_argument("symbols", nargs="+", help="예: 005930 AAPL (price 는 여러 개, 콤마 가능)")
    s.set_defaults(fn=h_quote)

    s = sub.add_parser("candles", help="캔들 (최신순)")
    s.add_argument("symbol")
    s.add_argument("-i", "--interval", choices=["1m", "1d"], default="1d")
    s.add_argument("-n", "--limit", type=int, default=100, help="봉 개수 (200 초과 시 자동 페이지네이션)")
    s.add_argument("--before")
    s.add_argument("--raw-price", dest="adjusted", action="store_false", default=None, help="수정주가 미적용")
    s.set_defaults(fn=h_candles)

    s = sub.add_parser("stock", help="종목 정보")
    s.add_argument("action", choices=["info", "warnings", "list", "trend"])
    s.add_argument("args", nargs="+", help="info: SYMBOLS · warnings: SYMBOL · list: MARKET · trend: KIND SYMBOL")
    s.add_argument("--status"), s.add_argument("--type"), s.add_argument("--count", type=int)
    s.add_argument("--common", type=lambda v: v.lower() == "true", default=None)
    s.set_defaults(fn=h_stock)

    s = sub.add_parser("market", help="장 운영·환율·랭킹·지수·섹터")
    s.add_argument("action", choices=["calendar", "fx", "rankings", "investor-rankings", "indices",
                                      "sectors", "sector", "sector-stocks", "sector-rankings"])
    s.add_argument("arg", nargs="?", help="calendar: KR|US · rankings: TYPE · sector*: SECTOR_ID")
    s.add_argument("--date"), s.add_argument("--base", default="USD"), s.add_argument("--quote", default="KRW")
    s.add_argument("--country", default="KR"), s.add_argument("--duration", default="1d")
    s.add_argument("--type", default="TOP_GAINERS"), s.add_argument("--count", type=int)
    s.add_argument("--exclude-caution", action="store_true", default=None)
    s.set_defaults(fn=h_market)

    s = sub.add_parser("account", help="계좌 목록·기본 계좌 지정")
    s.add_argument("action", choices=["list", "use"])
    s.add_argument("seq", nargs="?")
    s.set_defaults(fn=h_account)

    s = sub.add_parser("holdings", help="보유 주식"); acct(s)
    s.add_argument("--symbol"); s.set_defaults(fn=h_holdings)

    s = sub.add_parser("tx", help="거래내역 (결제일 기준)"); acct(s)
    s.add_argument("--type", help="BUY,SELL,DEPOSIT,... (콤마)")
    s.add_argument("--from", dest="from_"), s.add_argument("--to"), s.add_argument("--cursor")
    s.add_argument("--limit", type=int), s.add_argument("--all", action="store_true")
    s.add_argument("--max", type=int, default=1000); s.set_defaults(fn=h_tx)

    s = sub.add_parser("cash", help="매수 가능 금액"); acct(s)
    s.add_argument("--currency", default="KRW"); s.set_defaults(fn=h_cash)

    s = sub.add_parser("sellable", help="판매 가능 수량"); acct(s)
    s.add_argument("symbol"); s.set_defaults(fn=h_sellable)

    s = sub.add_parser("commissions", help="매매 수수료율"); acct(s); s.set_defaults(fn=h_commissions)

    s = sub.add_parser("order", aliases=["orders"], help="주문 조회·생성·정정·취소 (생성·정정은 미리보기 후 확인)")
    acct(s)
    s.add_argument("action", choices=["list", "show", "track", "buy", "sell", "modify", "submit", "pending",
                                      "discard", "cancel", "cancel-all"])
    s.add_argument("target", nargs="*", help="buy/sell: SYMBOL [QTY] · show/modify/cancel: ORDER_ID · "
                                            "submit/discard: CONFIRM_ID")
    s.add_argument("--limit", dest="limit_price", help="지정가 가격")
    s.add_argument("--market", action="store_true", help="시장가 (정책에서 허용해야 함)")
    s.add_argument("--amount", help="미국 시장가 금액 주문(USD, 소수점 매수)")
    s.add_argument("--qty", dest="qty_opt", help="정정 수량 (국내 전용)")
    s.add_argument("--tif", choices=["DAY", "CLS", "OPG"])
    s.add_argument("--yes", action="store_true", help="미리보기 후 바로 전송 / cancel-all 실행")
    s.add_argument("--closed", action="store_true", help="list: 종료된 주문")
    s.add_argument("--symbol"), s.add_argument("--from", dest="from_"), s.add_argument("--to")
    s.add_argument("--cursor"), s.add_argument("--page-size", dest="limit", type=int)
    s.add_argument("--all", action="store_true"), s.add_argument("--max", type=int, default=1000)
    s.set_defaults(fn=h_order, post=_order_args)

    s = sub.add_parser("cond", help="조건주문 조회·등록·취소 (등록은 미리보기 후 확인)")
    acct(s)
    s.add_argument("action", choices=["list", "show", "cancel", "single", "oco", "oto"])
    s.add_argument("target", nargs="*", help="show/cancel: ID · single/oco/oto: SYMBOL QTY")
    s.add_argument("--side", default="BUY", help="single: BUY|SELL")
    s.add_argument("--trigger"), s.add_argument("--price", help="single 지정가 (시장가면 생략 + --market)")
    s.add_argument("--tp-trigger"), s.add_argument("--tp-price"), s.add_argument("--sl-trigger"),
    s.add_argument("--sl-price")
    s.add_argument("--buy-trigger"), s.add_argument("--buy-price"), s.add_argument("--sell-trigger"),
    s.add_argument("--sell-price")
    s.add_argument("--market", action="store_true"), s.add_argument("--expire", help="만료일 YYYY-MM-DD (기본 30일)")
    s.add_argument("--with-order", action="store_true", help="cancel: 이미 접수된 주문도 함께 취소")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--closed", action="store_true"), s.add_argument("--symbol")
    s.add_argument("--cursor"), s.add_argument("--page-size", dest="limit", type=int)
    s.set_defaults(fn=h_cond, post=_cond_args)

    s = sub.add_parser("stream", help="실시간 체결·호가·주문 이벤트 (NDJSON). 계정당 동시 연결 2개 한도 주의")
    s.add_argument("topics", nargs="+", help="trade:kr:005930 · orderbook:us:AAPL · order · cond")
    s.add_argument("--seconds", type=float, help="이 시간(초)이 지나면 종료")
    s.add_argument("--count", type=int, help="실시간 데이터(message) N개를 받으면 종료")
    s.add_argument("--no-resync", action="store_true", help="주문 채널 연결 직후 REST 스냅샷 생략")
    s.add_argument("--log", help="받은 이벤트를 이 파일에 JSONL 로 추가 기록")
    s.set_defaults(fn=h_stream)

    s = sub.add_parser("record", help="실시간 기록: 원본 NDJSON.gz + SQLite(WAL). 연결 1개 사용")
    s.add_argument("topics", nargs="+", help="trade:kr:005930 · orderbook:kr:005930 · order · cond")
    s.add_argument("--seconds", type=float), s.add_argument("--count", type=int, help="실시간 데이터 N건 후 종료")
    s.add_argument("--db", help="SQLite 경로 (기본 ~/.local/share/toss/live.db, TOSS_DATA 로 변경)")
    s.add_argument("--raw-dir"), s.add_argument("--no-raw", action="store_true", help="원본 NDJSON 기록 안 함")
    s.add_argument("--book-interval", type=float, default=1.0, help="최우선 호가가 그대로일 때 기록 간격(초)")
    s.add_argument("--no-resync", action="store_true")
    s.set_defaults(fn=h_record)

    s = sub.add_parser("live", help="기록 중인 SQLite 를 조회 (WebSocket 연결을 쓰지 않음, 키 불필요)")
    s.add_argument("action", choices=["status", "bars", "book", "trades", "orders"])
    s.add_argument("symbol", nargs="?")
    s.add_argument("--minutes", type=float, default=30), s.add_argument("--interval", choices=["1s", "1m"], default="1m")
    s.add_argument("--limit", type=int, default=20), s.add_argument("--db")
    s.set_defaults(fn=h_live, offline=True)

    s = sub.add_parser("archive", help="하루치를 Parquet 로 보관하고 오래된 SQLite 행 정리 (duckdb 필요)")
    s.add_argument("--date", help="YYYY-MM-DD (KST, 기본 오늘)"), s.add_argument("--out"), s.add_argument("--db")
    s.add_argument("--prune-days", type=int, help="이 일수보다 오래된 SQLite 행 삭제")
    s.set_defaults(fn=h_archive, offline=True)

    s = sub.add_parser("policy", help="거래 안전 정책·킬 스위치")
    s.add_argument("action", choices=["show", "set", "kill", "unkill"])
    s.add_argument("key", nargs="?"), s.add_argument("value", nargs="?")
    s.set_defaults(fn=h_policy)
    return p


def main(argv: list[str] | None = None, client_factory=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "post", None):
            args.post(args)
        if getattr(args, "offline", False):
            _out(args.fn(None, args), args.compact)
            return EXIT_OK
        if client_factory is None:
            if args.env_file:
                load_dotenv(args.env_file)
            else:  # 먼저 읽은 값이 우선 (load_dotenv 는 기존 값을 덮어쓰지 않음)
                load_dotenv(".env")
                load_dotenv(toss_home() / ".env")
            client = TossClient.from_env()
        else:
            client = client_factory()
        with client:
            result = args.fn(client, args)
            if result is not None:          # stream 은 직접 NDJSON 을 출력하고 None 을 돌려줌
                _out(result, args.compact)
        return EXIT_OK
    except SafetyError as e:
        _err(e.to_dict()); return EXIT_SAFETY
    except ConfigError as e:
        _err({"type": "config", "message": str(e)}); return EXIT_CONFIG
    except RateLimitError as e:
        _err(e.to_dict()); return EXIT_RATE
    except ServerError as e:
        _err(e.to_dict()); return EXIT_SERVER
    except TossAPIError as e:
        _err(e.to_dict()); return EXIT_REQUEST
    except ValueError as e:
        _err({"type": "invalid-argument", "message": str(e)}); return EXIT_REQUEST
    except httpx.HTTPError as e:
        _err({"type": "network", "message": str(e)}); return EXIT_SERVER
    except FileNotFoundError as e:
        _err({"type": "not-found", "message": str(e)}); return EXIT_CONFIG
    except RuntimeError as e:
        _err({"type": "runtime", "message": str(e)}); return EXIT_CONFIG
    except Exception as e:  # noqa: BLE001  — 마지막 안전망: 트레이스백 대신 JSON 오류
        _err({"type": "internal", "error": type(e).__name__, "message": str(e)}); return EXIT_SERVER


if __name__ == "__main__":
    sys.exit(main())

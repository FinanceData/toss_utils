"""실시간 기록 계층 테스트: 집계, SQLite(WAL) 동시 읽기, 재연결 공백, 원본 NDJSON.gz, Parquet 보관, CLI·MCP."""
import asyncio
import gzip
import json
import sqlite3
import threading
import time

import pytest

pytest.importorskip("websockets")

from toss_utils import recorder as R  # noqa: E402
from toss_utils.cli import main as cli_main  # noqa: E402
from toss_utils.tools import build_tools  # noqa: E402

from .test_stream import FakeWS, fast_sleep, run_with_server, setup  # noqa: E402,F401

T0 = "2026-11-17T12:00:00.100+09:00"


def trade(topic, price, vol, ts=T0):
    return {"type": "message", "topic": topic, "data": {"price": price, "volume": vol, "timestamp": ts,
                                                         "currency": "KRW"}}


def book(sym, bid, ask, ts=T0):
    return {"type": "message", "topic": f"orderbook:kr:{sym}",
            "data": {"timestamp": ts, "currency": "KRW",
                     "asks": [{"price": ask, "volume": "10"}, {"price": str(int(ask) + 100), "volume": "5"}],
                     "bids": [{"price": bid, "volume": "7"}, {"price": str(int(bid) - 100), "volume": "3"}]}}


# ---------------------------------------------------------------- 스케일·시간 변환
def test_scaling_is_exact():
    assert R.to_e4("193600") == 1_936_000_000 and R.to_e4("0.0001") == 1 and R.to_e4("353.1") == 3_531_000
    assert R.to_e6("0.670593") == 670_593
    assert R.from_e4(3_531_000) == "353.1" and R.from_e6(670_593) == "0.670593"
    ns = R.iso_to_ns("2026-11-17T12:38:46.473+09:00", 0)
    assert ns % 1_000_000_000 == 473_000_000


# ---------------------------------------------------------------- 집계
def test_bars_ohlcv_and_vwap():
    agg = R.Aggregator()
    for p, v in [("100", "1"), ("103", "2"), ("99", "1"), ("101", "4")]:
        agg.on_event(trade("trade:kr:005930", p, v), time.time_ns())
    b = agg.drain()
    assert len(b.trades) == 4 and len(b.bars_1s) == 1 and len(b.bars_1m) == 1
    sym, ts_m, o, h, l, c, v, n, amt = b.bars_1m[0]
    assert (from_ := [R.from_e4(x) for x in (o, h, l, c)]) == ["100", "103", "99", "101"], from_
    assert R.from_e6(v) == "8" and n == 4
    assert R.from_e4(amt) == "809" and R.from_e4(amt * 1_000_000 // v) == "101.125"   # 809 / 8


def test_book_rows_only_on_change_or_interval_and_crossed_flag():
    agg = R.Aggregator(book_interval=10)
    now = time.time_ns()
    agg.on_event(book("005930", "100", "101"), now)
    agg.on_event(book("005930", "100", "101"), now + 1_000)        # 같음 → 기록 안 함
    agg.on_event(book("005930", "101", "101"), now + 2_000)        # 변경 + 교차(매도1 ≤ 매수1)
    rows = agg.drain().book
    assert len(rows) == 2 and rows[0][8] == 0 and rows[1][8] == 1 and rows[1][7] == 0     # 1 = 같음(locked)
    agg.on_event(book("005930", "102", "101"), now + 3_000)                                   # 역전
    assert agg.drain().book[0][8] == 2
    assert json.loads(rows[0][9])[0]["price"] == "100"
    agg.on_event(book("005930", "101", "101"), now + 11 * 10**9)   # 그대로지만 10초 경과 → 기록
    assert len(agg.drain().book) == 1


def test_bar_increments_add_up_across_flushes(tmp_path):
    """flush 를 여러 번 나눠도(=늦게 온 체결 포함) DB 의 봉은 전체 합과 같아야 한다."""
    con = R.connect_db(tmp_path / "live.db")
    agg = R.Aggregator()
    for i, (p, v) in enumerate([("100", "1"), ("105", "2"), ("98", "3"), ("101", "4")]):
        agg.on_event(trade("trade:kr:005930", p, v), time.time_ns())
        R.write_batch(con, agg.drain())          # 체결마다 flush
    row = con.execute("SELECT o_e4, h_e4, l_e4, c_e4, v_e6, n FROM bars_1m").fetchone()
    assert [R.from_e4(x) for x in row[:4]] == ["100", "105", "98", "101"] and R.from_e6(row[4]) == "10" and row[5] == 4


def test_high_price_large_volume_does_not_overflow(tmp_path):
    """실측 회귀: SK하이닉스(약 111만 원) 대량 체결에서 price_e4 × volume_e6 누적이 SQLite 정수 한도를 넘었음."""
    con = R.connect_db(tmp_path / "live.db")
    agg = R.Aggregator()
    for _ in range(50):
        agg.on_event(trade("trade:kr:000660", "1119000", "100000"), time.time_ns())   # 1,119억 원 × 50
    R.write_batch(con, agg.drain())
    r = R.query_bars(tmp_path / "live.db", "000660", minutes=10**8)[0]
    assert r["volume"] == "5000000" and r["vwap"] == "1119000"


def test_old_schema_db_is_refused(tmp_path):
    db = tmp_path / "old.db"
    sqlite3.connect(db).execute("CREATE TABLE bars_1m (symbol TEXT, pv INTEGER)")
    with pytest.raises(RuntimeError, match="이전 형식"):
        R.connect_db(db)


def test_gap_from_reconnect_statuses():
    agg = R.Aggregator()
    agg.on_event(trade("trade:kr:005930", "100", "1"), 1_000)
    agg.on_event({"type": "status", "state": "reconnecting", "reason": "auth-revoked"}, 5_000)
    agg.on_event({"type": "status", "state": "connected"}, 9_000)
    assert agg.drain().gaps == [(1_000, 9_000, "auth-revoked")]   # 마지막 데이터 ~ 재연결까지


def test_order_events_and_resync_marks_missing_open_orders(tmp_path):
    db = tmp_path / "live.db"
    con = R.connect_db(db)
    agg = R.Aggregator()
    ev = {"type": "message", "topic": "personal:order:1",
          "data": {"event": "PENDING", "accountSeq": "1",
                   "order": {"orderId": "o-1", "symbol": "005930", "status": "PENDING"}}}
    agg.on_event(ev, 1)
    R.write_batch(con, agg.drain())
    agg.on_event({"type": "resync", "orders": []}, 2)               # 재연결 스냅샷에 o-1 이 없음
    R.write_batch(con, agg.drain())
    assert con.execute("SELECT status FROM orders_latest WHERE order_id='o-1'").fetchone()[0] == "CHECK_REST"
    assert con.execute("SELECT count(*) FROM order_events").fetchone()[0] == 1


# ---------------------------------------------------------------- 기록 + 동시 읽기
def test_record_end_to_end_with_concurrent_reader(setup, tmp_path):  # noqa: F811
    rest, fws, c = setup
    db, raw = tmp_path / "live.db", tmp_path / "raw"
    seen_while_recording = []

    async def push(ws, n, sub):
        for i in range(40):
            await ws.send(json.dumps(trade("trade:kr:005930", str(100 + i % 5), "1")))
            await ws.send(json.dumps(book("005930", str(99 + i % 3), str(101 + i % 3))))
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "message", "topic": "personal:order:1",
                                  "data": {"event": "FILL", "order": {"orderId": "o-9", "symbol": "005930",
                                                                     "status": "FILLED"}}}))
    fws.after_declare = push

    def reader():   # 다른 "프로세스" 역할: 읽기 전용 연결로 기록 중에 조회
        for _ in range(60):
            time.sleep(0.05)
            try:
                bars = R.query_bars(db, "005930", minutes=10**8, interval="1s")
            except (FileNotFoundError, sqlite3.OperationalError):
                continue
            if bars:
                seen_while_recording.append(len(bars))

    th = threading.Thread(target=reader)
    th.start()
    summary = run_with_server(fws, lambda url: R.record_async(
        c, ["trade:kr:005930", "orderbook:kr:005930", "order"], db=db, raw_dir=raw, max_messages=81,
        url=url, flush_interval=0.05))
    th.join()
    assert summary["trades"] == 40 and summary["order_events"] == 1 and summary["messages"] == 81
    assert seen_while_recording, "기록 중에 다른 연결에서 읽을 수 있어야 함 (WAL)"
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM trades").fetchone()[0] == 40
    assert con.execute("SELECT sum(n) FROM bars_1m").fetchone()[0] == 40
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("SELECT status FROM orders_latest WHERE order_id='o-9'").fetchone()[0] == "FILLED"
    lines = [json.loads(x) for f in raw.rglob("*.ndjson.gz") for x in gzip.open(f, "rt")]
    assert sum(1 for x in lines if x["type"] == "message") == 81 and all("recv_ns" in x for x in lines)


def test_record_writes_gap_on_reconnect(setup, tmp_path):  # noqa: F811
    rest, fws, c = setup

    async def revoke_then_push(ws, n, sub):
        if n == 1:
            await ws.send(json.dumps(trade("trade:kr:005930", "100", "1")))
            rest.issued.append("tok-external")
            await ws.send(json.dumps({"type": "error", "error": {"code": "auth-revoked", "message": "x"}}))
            await ws.close()
        else:
            await ws.send(json.dumps(trade("trade:kr:005930", "101", "1")))
    fws.after_declare = revoke_then_push
    db = tmp_path / "live.db"
    run_with_server(fws, lambda url: R.record_async(c, ["trade:kr:005930"], db=db, raw=False, max_messages=2,
                                                    url=url, sleep=fast_sleep))
    st = R.query_status(db)
    assert st["rows"]["gaps"] == 1 and st["recent_gaps"][0]["reason"] == "auth-revoked"
    assert st["rows"]["trades"] == 2


# ---------------------------------------------------------------- 보관 · CLI · MCP
def _seed(db):
    con = R.connect_db(db)
    agg = R.Aggregator()
    for p, v in [("193600", "10"), ("193700", "5")]:
        agg.on_event(trade("trade:kr:005930", p, v), time.time_ns())
    agg.on_event(book("005930", "193500", "193600"), time.time_ns())
    R.write_batch(con, agg.drain())
    con.close()


def test_archive_to_parquet_keeps_exact_decimals(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "live.db"
    _seed(db)
    out = R.archive(db, tmp_path / "parquet", "2026-11-17")
    assert set(out["files"]) == {"trades", "bars_1m", "book_top"}
    t = duckdb.sql(f"SELECT price, volume, typeof(price) FROM '{out['files']['trades']['path']}' ORDER BY price").fetchall()
    assert [str(x[0]) for x in t] == ["193600.0000", "193700.0000"] and t[0][2] == "DECIMAL(18,4)"
    bar = duckdb.sql(f"SELECT open, close, volume, vwap FROM '{out['files']['bars_1m']['path']}'").fetchall()[0]
    assert [str(x) for x in bar] == ["193600.0000", "193700.0000", "15.000000", "193633.3333"]


def test_cli_live_reads_without_credentials(tmp_path, monkeypatch, capsys):
    db = tmp_path / "live.db"
    _seed(db)
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    assert cli_main(["--compact", "live", "book", "005930", "--db", str(db)]) == 0
    b = json.loads(capsys.readouterr().out)
    assert b["bid1"] == "193500" and b["ask1"] == "193600" and b["spread"] == "100" and b["state"] == "normal"
    assert cli_main(["live", "bars", "005930", "--db", str(db), "--minutes", "100000000"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["volume"] == "15"
    assert cli_main(["live", "status", "--db", str(tmp_path / "none.db")]) == 1      # 기록 없음 → 설정 오류


def test_mcp_live_tools_read_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TOSS_DATA", str(tmp_path))
    tools = {t.name: t for t in build_tools(lambda: None)}
    assert tools["get_live_status"].fn()["error"]["type"] == "not-found"
    _seed(tmp_path / "live.db")
    out = tools["get_live_top_of_book"].fn(symbol="005930")
    assert out["ok"] and out["result"]["ask1"] == "193600"
    assert tools["get_live_status"].fn()["result"]["rows"]["trades"] == 2

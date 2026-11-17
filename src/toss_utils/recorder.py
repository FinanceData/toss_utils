"""실시간 기록 계층: 저장하면서 동시에 분석.

구조 (프로세스 1개가 WebSocket 연결 1개를 쥔다)
  RealtimeStream ─▶ ① 원본 NDJSON.gz (시간별 파일, 받은 그대로 + recv_ns)
                 ─▶ ② Aggregator (이벤트 루프 안, 메모리에서 즉시 계산)
                        체결 행 · 1초/1분 봉(OHLCV, VWAP) · 최우선 호가(바뀔 때 또는 N초마다) · 공백 · 주문 이벤트
                 ─▶ ③ Writer 스레드가 0.25초마다 묶어서 SQLite(WAL) 에 기록
  다른 프로세스(분석 노트북·전략·MCP 도구)는 같은 SQLite 를 읽기 전용으로 동시에 조회한다.
  장 마감 후 archive() 로 Parquet 에 옮기고 오래된 행은 정리한다.

가격·수량은 float 대신 정수로 스케일한다: price_e4 = 가격×10,000, volume_e6 = 수량×1,000,000.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .client import TossClient

KST = timezone(timedelta(hours=9))
P_SCALE, Q_SCALE = 10_000, 1_000_000
OPEN_ORDER = ("PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE")


def data_home() -> Path:
    return Path(os.environ.get("TOSS_DATA", "~/.local/share/toss")).expanduser()


def default_db() -> Path:
    return data_home() / "live.db"


def to_e4(s) -> int:
    return int((Decimal(str(s)) * P_SCALE).to_integral_value(ROUND_HALF_EVEN))


def to_e6(s) -> int:
    return int((Decimal(str(s)) * Q_SCALE).to_integral_value(ROUND_HALF_EVEN))


def from_e4(v: int | None) -> str | None:
    return None if v is None else format((Decimal(v) / P_SCALE).normalize(), "f")


def from_e6(v: int | None) -> str | None:
    return None if v is None else format((Decimal(v) / Q_SCALE).normalize(), "f")


def iso_to_ns(s: str | None, default: int) -> int:
    if not s:
        return default
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return default
    return int(dt.replace(microsecond=0).timestamp()) * 1_000_000_000 + dt.microsecond * 1000


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS trades (ts_ns INTEGER, recv_ns INTEGER, symbol TEXT, market TEXT,
    price_e4 INTEGER, volume_e6 INTEGER);
CREATE INDEX IF NOT EXISTS trades_sym_ts ON trades(symbol, ts_ns);
CREATE TABLE IF NOT EXISTS bars_1s (symbol TEXT, ts_s INTEGER, o_e4 INTEGER, h_e4 INTEGER, l_e4 INTEGER,
    c_e4 INTEGER, v_e6 INTEGER, n INTEGER, amt_e4 INTEGER, PRIMARY KEY(symbol, ts_s));
CREATE TABLE IF NOT EXISTS bars_1m (symbol TEXT, ts_m INTEGER, o_e4 INTEGER, h_e4 INTEGER, l_e4 INTEGER,
    c_e4 INTEGER, v_e6 INTEGER, n INTEGER, amt_e4 INTEGER, PRIMARY KEY(symbol, ts_m));
CREATE TABLE IF NOT EXISTS book_top (symbol TEXT, ts_ns INTEGER, recv_ns INTEGER, bid1_e4 INTEGER,
    bid1_q_e6 INTEGER, ask1_e4 INTEGER, ask1_q_e6 INTEGER, spread_e4 INTEGER,
    crossed INTEGER,          -- 0 정상(매도1 > 매수1) · 1 같음(locked) · 2 역전(crossed). 통합 호가(KRX+NXT)라 1·2 가 흔함
    bids5 TEXT, asks5 TEXT);
CREATE INDEX IF NOT EXISTS book_sym_ts ON book_top(symbol, ts_ns);
CREATE TABLE IF NOT EXISTS gaps (start_ns INTEGER, end_ns INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS order_events (recv_ns INTEGER, kind TEXT, event TEXT, ref_id TEXT, symbol TEXT,
    raw TEXT);
CREATE TABLE IF NOT EXISTS orders_latest (order_id TEXT PRIMARY KEY, symbol TEXT, status TEXT, raw TEXT,
    updated_ns INTEGER);
CREATE TABLE IF NOT EXISTS cond_latest (cond_id TEXT PRIMARY KEY, symbol TEXT, status TEXT, raw TEXT,
    updated_ns INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# --------------------------------------------------------------------------- 메모리 집계
@dataclass
class Bar:
    o: int
    h: int
    l: int
    c: int
    v: int = 0
    n: int = 0
    amt: int = 0       # 거래대금 × 10^4 (Σ price × volume). VWAP_e4 = amt × 10^6 / v_e6
                       # ※ price_e4 × volume_e6 를 그대로 더하면 고가 종목(예: 111만 원 × 1,000주)에서
                       #   SQLite 정수 한도(9.2×10^18)를 넘는다 → 10^6 으로 나눠 거래대금 단위로 저장

    def add(self, p: int, q: int) -> None:
        self.h, self.l, self.c = max(self.h, p), min(self.l, p), p
        self.v += q
        self.n += 1
        self.amt += (p * q + 500_000) // 1_000_000


@dataclass
class Batch:
    trades: list = field(default_factory=list)
    bars_1s: list = field(default_factory=list)
    bars_1m: list = field(default_factory=list)
    book: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    order_events: list = field(default_factory=list)
    orders_latest: list = field(default_factory=list)
    cond_latest: list = field(default_factory=list)
    resync_open_orders: list | None = None      # resync 스냅샷의 진행 중 주문 ID (없어진 건 CHECK_REST 표시)

    def empty(self) -> bool:
        return not any((self.trades, self.bars_1s, self.bars_1m, self.book, self.gaps, self.order_events,
                        self.orders_latest, self.cond_latest)) and self.resync_open_orders is None


class Aggregator:
    """이벤트를 받아 메모리에서 봉·최우선 호가·공백을 계산하고, drain() 때 쓸 행을 돌려준다."""

    def __init__(self, book_interval: float = 1.0):
        self.book_interval_ns = int(book_interval * 1e9)
        self.b1s: dict[tuple, Bar] = {}       # flush 사이의 증분 봉
        self.b1m: dict[tuple, Bar] = {}
        self.last_book: dict[str, tuple] = {}     # symbol → (bid1, ask1, recv_ns)
        self.gap_start: int | None = None
        self.gap_reason: str | None = None
        self.last_msg_ns: int | None = None
        self.batch = Batch()
        self.counts = {"trades": 0, "book_rows": 0, "book_frames": 0, "order_events": 0, "gaps": 0}

    def on_event(self, ev: dict, recv_ns: int) -> None:
        kind = ev.get("type")
        if kind == "message":
            self.last_msg_ns = recv_ns
            topic = ev.get("topic") or ""
            data = ev.get("data") or {}
            if topic.startswith("trade:"):
                self._trade(topic, data, recv_ns)
            elif topic.startswith("orderbook:"):
                self._book(topic, data, recv_ns)
            elif topic.startswith("personal:order:"):
                self._order_event("order", data, recv_ns)
            elif topic.startswith("personal:conditional-order:"):
                self._order_event("cond", data, recv_ns)
        elif kind == "status":
            if ev.get("state") == "reconnecting" and self.gap_start is None:
                self.gap_start, self.gap_reason = self.last_msg_ns or recv_ns, ev.get("reason")
            elif ev.get("state") == "connected" and self.gap_start is not None:
                self.batch.gaps.append((self.gap_start, recv_ns, self.gap_reason))
                self.counts["gaps"] += 1
                self.gap_start = None
        elif kind == "resync":
            orders = ev.get("orders") or []
            for o in orders:
                self.batch.orders_latest.append((o.get("orderId"), o.get("symbol"), o.get("status"),
                                                 json.dumps(o, ensure_ascii=False), recv_ns))
            if "orders" in ev:
                self.batch.resync_open_orders = [o.get("orderId") for o in orders]
            for co in ev.get("conditionalOrders") or []:
                self.batch.cond_latest.append((co.get("conditionalOrderId"), co.get("symbol"), co.get("status"),
                                               json.dumps(co, ensure_ascii=False), recv_ns))

    @staticmethod
    def _split(topic: str) -> tuple[str, str]:
        parts = topic.split(":")              # trade:kr:005930
        return parts[1].upper(), ":".join(parts[2:])

    def _trade(self, topic, d, recv_ns):
        market, sym = self._split(topic)
        p, q = to_e4(d["price"]), to_e6(d["volume"])
        ts = iso_to_ns(d.get("timestamp"), recv_ns)
        self.batch.trades.append((ts, recv_ns, sym, market, p, q))
        self.counts["trades"] += 1
        for store, key in ((self.b1s, (sym, ts // 1_000_000_000)), (self.b1m, (sym, ts // 60_000_000_000))):
            bar = store.get(key)
            if bar is None:
                store[key] = bar = Bar(p, p, p, p)
            bar.add(p, q)

    def _book(self, topic, d, recv_ns):
        market, sym = self._split(topic)
        self.counts["book_frames"] += 1
        asks, bids = d.get("asks") or [], d.get("bids") or []
        if not asks or not bids:
            return
        a1, b1 = to_e4(asks[0]["price"]), to_e4(bids[0]["price"])
        last = self.last_book.get(sym)
        changed = last is None or (b1, a1) != last[:2]
        if not changed and recv_ns - last[2] < self.book_interval_ns:
            return                            # 최우선 호가가 그대로면 book_interval 마다 한 번만
        self.last_book[sym] = (b1, a1, recv_ns)
        self.batch.book.append((sym, iso_to_ns(d.get("timestamp"), recv_ns), recv_ns, b1, to_e6(bids[0]["volume"]),
                                a1, to_e6(asks[0]["volume"]), a1 - b1, 0 if a1 > b1 else 1 if a1 == b1 else 2,
                                json.dumps(bids[:5]), json.dumps(asks[:5])))
        self.counts["book_rows"] += 1

    def _order_event(self, kind, d, recv_ns):
        snap = d.get("order") if kind == "order" else d.get("conditionalOrder")
        snap = snap or {}
        ref = snap.get("orderId") if kind == "order" else snap.get("conditionalOrderId")
        raw = json.dumps(d, ensure_ascii=False)
        self.batch.order_events.append((recv_ns, kind, d.get("event"), ref, snap.get("symbol"), raw))
        self.counts["order_events"] += 1
        row = (ref, snap.get("symbol"), snap.get("status"), json.dumps(snap, ensure_ascii=False), recv_ns)
        (self.batch.orders_latest if kind == "order" else self.batch.cond_latest).append(row)

    def drain(self) -> Batch:
        """이번 flush 까지의 행을 돌려준다. 봉은 증분(이번 구간에 들어온 체결만)이며 DB 에서 합산된다."""
        b, self.batch = self.batch, Batch()
        b.bars_1s = [(k[0], k[1], *self._bar_cols(bar)) for k, bar in self.b1s.items()]
        b.bars_1m = [(k[0], k[1], *self._bar_cols(bar)) for k, bar in self.b1m.items()]
        self.b1s, self.b1m = {}, {}
        return b

    @staticmethod
    def _bar_cols(bar: Bar):
        return (bar.o, bar.h, bar.l, bar.c, bar.v, bar.n, bar.amt)


# --------------------------------------------------------------------------- SQLite
def connect_db(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not Path(path).exists():
            raise FileNotFoundError(f"실시간 DB 가 없습니다: {path}. 먼저 `toss record ...` 로 기록을 시작하세요.")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=5, check_same_thread=False)
        cols = [r[1] for r in con.execute("PRAGMA table_info(bars_1m)")]
        if "pv" in cols:
            con.close()
            raise RuntimeError(f"이전 형식(v1)의 DB 입니다: {path}. 다른 경로(--db)를 쓰거나 파일을 옮긴 뒤 다시 시작하세요.")
        con.executescript(SCHEMA)
    con.execute("PRAGMA busy_timeout = 5000")
    return con


# 봉은 flush 사이의 "증분"만 보내고 DB 에서 더한다 → 메모리에 봉을 오래 들고 있을 필요가 없고,
# 늦게 도착한 체결도 기존 봉에 정확히 합쳐진다 (시가는 처음 기록된 값 유지).
BAR_UPSERT = """INSERT INTO {t} VALUES (?,?,?,?,?,?,?,?,?)
  ON CONFLICT({pk}) DO UPDATE SET h_e4=max(h_e4, excluded.h_e4), l_e4=min(l_e4, excluded.l_e4),
  c_e4=excluded.c_e4, v_e6=v_e6+excluded.v_e6, n=n+excluded.n, amt_e4=amt_e4+excluded.amt_e4"""


def write_batch(con: sqlite3.Connection, b: Batch) -> None:
    with con:     # 트랜잭션 하나로 묶어서 기록
        con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?)", b.trades)
        con.executemany(BAR_UPSERT.format(t="bars_1s", pk="symbol, ts_s"), b.bars_1s)
        con.executemany(BAR_UPSERT.format(t="bars_1m", pk="symbol, ts_m"), b.bars_1m)
        con.executemany("INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,?,?)", b.book)
        con.executemany("INSERT INTO gaps VALUES (?,?,?)", b.gaps)
        con.executemany("INSERT INTO order_events VALUES (?,?,?,?,?,?)", b.order_events)
        if b.resync_open_orders is not None:
            # 재연결 사이에 끝난 주문: 스냅샷(진행 중)에 없는데 우리 기록상 진행 중이면 REST 확인 필요로 표시
            ids = b.resync_open_orders
            q = ",".join("?" * len(ids)) or "''"
            con.execute(f"UPDATE orders_latest SET status='CHECK_REST' WHERE status IN "
                        f"({','.join(repr(s) for s in OPEN_ORDER)}) AND order_id NOT IN ({q})", ids)
        con.executemany("INSERT INTO orders_latest VALUES (?,?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET "
                        "status=excluded.status, raw=excluded.raw, updated_ns=excluded.updated_ns", b.orders_latest)
        con.executemany("INSERT INTO cond_latest VALUES (?,?,?,?,?) ON CONFLICT(cond_id) DO UPDATE SET "
                        "status=excluded.status, raw=excluded.raw, updated_ns=excluded.updated_ns", b.cond_latest)
        con.execute("INSERT INTO meta VALUES ('last_flush_ns', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(time.time_ns()),))


class Writer(threading.Thread):
    """SQLite 쓰기 전용 스레드. DB 쓰기가 잠깐 느려져도 WebSocket 수신이 막히지 않게 한다."""

    def __init__(self, db: Path):
        super().__init__(daemon=True, name="toss-writer")
        self.q: queue.Queue = queue.Queue()
        self.db = db
        self.error: BaseException | None = None
        self.batches = 0

    def run(self):
        con = connect_db(self.db)
        try:
            while True:
                b = self.q.get()
                if b is None:
                    return
                write_batch(con, b)
                self.batches += 1
        except BaseException as e:  # noqa: BLE001
            self.error = e
        finally:
            con.close()


class RawLog:
    """원본 NDJSON.gz. 시간(KST)마다 파일을 바꾸고, 각 줄은 받은 이벤트 + recv_ns."""

    def __init__(self, root: Path):
        self.root = root
        self.f = None
        self.key = None
        self.lines = 0

    def write(self, ev: dict, recv_ns: int) -> None:
        now = datetime.fromtimestamp(recv_ns / 1e9, KST)
        key = (now.date().isoformat(), now.hour)
        if key != self.key:
            self.close()
            d = self.root / f"date={key[0]}"
            d.mkdir(parents=True, exist_ok=True)
            self.f = gzip.open(d / f"{key[1]:02d}.ndjson.gz", "at", encoding="utf-8")   # 이어 쓰기(gzip 멤버 추가)
            self.key = key
        self.f.write(json.dumps({**ev, "recv_ns": recv_ns}, ensure_ascii=False) + "\n")
        self.lines += 1

    def flush(self):
        if self.f:
            self.f.flush()

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


# --------------------------------------------------------------------------- 기록 실행
async def record_async(client: "TossClient", topics: Iterable[str], *, db: Path | None = None,
                       raw_dir: Path | None = None, raw: bool = True, seconds: float | None = None,
                       max_messages: int | None = None, flush_interval: float = 0.25,
                       book_interval: float = 1.0, on_event=None, **stream_kw) -> dict:
    from .stream import RealtimeStream
    db = Path(db or default_db())
    stream = RealtimeStream(client, topics, **stream_kw)
    agg = Aggregator(book_interval)
    writer = Writer(db)
    writer.start()
    rawlog = RawLog(Path(raw_dir or data_home() / "raw")) if raw else None
    started, n_msg = time.time(), 0

    async def flusher():
        while True:
            await asyncio.sleep(flush_interval)
            b = agg.drain()
            if not b.empty():
                writer.q.put(b)
            if rawlog:
                rawlog.flush()
            if writer.error:
                raise writer.error

    async def consume():
        nonlocal n_msg
        agen = stream.events()
        try:
            async for ev in agen:
                recv = time.time_ns()
                if rawlog:
                    rawlog.write(ev, recv)
                agg.on_event(ev, recv)
                if on_event:
                    on_event(ev)
                if ev.get("type") == "message":
                    n_msg += 1
                    if max_messages and n_msg >= max_messages:
                        return
        finally:
            await agen.aclose()

    ft = asyncio.create_task(flusher())
    try:
        if seconds:
            try:
                await asyncio.wait_for(consume(), seconds)
            except asyncio.TimeoutError:
                pass
        else:
            await consume()
    finally:
        ft.cancel()
        writer.q.put(agg.drain())     # 마지막 묶음
        writer.q.put(None)
        await asyncio.to_thread(writer.join)
        if rawlog:
            rawlog.close()
    if writer.error:
        raise writer.error
    return {"db": str(db), "raw_dir": str(rawlog.root) if rawlog else None, "seconds": round(time.time() - started, 1),
            "messages": n_msg, **agg.counts, "raw_lines": rawlog.lines if rawlog else 0,
            "topics": [f"{t}:{c}" for t, c in sorted(stream.topics)], "rejected": stream.rejected}


# --------------------------------------------------------------------------- 조회 (다른 프로세스에서 동시에)
def _bar_dict(r, unit):
    sym, t, o, h, l, c, v, n, amt = r
    return {"symbol": sym, "time": datetime.fromtimestamp(t * unit, KST).isoformat(), "open": from_e4(o),
            "high": from_e4(h), "low": from_e4(l), "close": from_e4(c), "volume": from_e6(v), "trades": n,
            "vwap": from_e4(amt * 1_000_000 // v) if v else None}


def query_bars(db: Path, symbol: str, minutes: float = 30, interval: str = "1m") -> list[dict]:
    con = connect_db(db, readonly=True)
    try:
        if interval == "1s":
            since = int(time.time() - minutes * 60)
            rows = con.execute("SELECT * FROM bars_1s WHERE symbol=? AND ts_s>=? ORDER BY ts_s", (symbol, since))
            return [_bar_dict(r, 1) for r in rows]
        since = int(time.time() / 60 - minutes)
        rows = con.execute("SELECT * FROM bars_1m WHERE symbol=? AND ts_m>=? ORDER BY ts_m", (symbol, since))
        return [_bar_dict(r, 60) for r in rows]
    finally:
        con.close()


def query_book(db: Path, symbol: str) -> dict | None:
    con = connect_db(db, readonly=True)
    try:
        r = con.execute("SELECT * FROM book_top WHERE symbol=? ORDER BY recv_ns DESC LIMIT 1", (symbol,)).fetchone()
        if not r:
            return None
        sym, ts, recv, b1, bq, a1, aq, spread, crossed, bids5, asks5 = r
        return {"symbol": sym, "time": datetime.fromtimestamp(ts / 1e9, KST).isoformat(),
                "age_sec": round(time.time() - recv / 1e9, 1), "bid1": from_e4(b1), "bid1_qty": from_e6(bq),
                "ask1": from_e4(a1), "ask1_qty": from_e6(aq), "spread": from_e4(spread),
                "state": ("normal", "locked", "crossed")[crossed], "crossed": bool(crossed),
                "bids5": json.loads(bids5), "asks5": json.loads(asks5)}
    finally:
        con.close()


def query_trades(db: Path, symbol: str, limit: int = 20) -> list[dict]:
    con = connect_db(db, readonly=True)
    try:
        rows = con.execute("SELECT ts_ns, price_e4, volume_e6 FROM trades WHERE symbol=? ORDER BY ts_ns DESC LIMIT ?",
                           (symbol, limit)).fetchall()
        return [{"time": datetime.fromtimestamp(t / 1e9, KST).isoformat(timespec="milliseconds"),
                 "price": from_e4(p), "volume": from_e6(v)} for t, p, v in rows]
    finally:
        con.close()


def query_status(db: Path) -> dict:
    con = connect_db(db, readonly=True)
    try:
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("trades", "bars_1s", "bars_1m", "book_top", "gaps", "order_events", "orders_latest")}
        last = con.execute("SELECT value FROM meta WHERE key='last_flush_ns'").fetchone()
        syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM bars_1m ORDER BY 1")]
        gaps = [{"start": datetime.fromtimestamp(s / 1e9, KST).isoformat(timespec="seconds"),
                 "end": datetime.fromtimestamp(e / 1e9, KST).isoformat(timespec="seconds"),
                 "seconds": round((e - s) / 1e9, 1), "reason": r}
                for s, e, r in con.execute("SELECT * FROM gaps ORDER BY start_ns DESC LIMIT 20")]
        return {"db": str(db), "size_mb": round(Path(db).stat().st_size / 1e6, 2), "rows": counts, "symbols": syms,
                "last_flush_age_sec": round(time.time() - int(last[0]) / 1e9, 1) if last else None,
                "recent_gaps": gaps}
    finally:
        con.close()


def query_orders(db: Path) -> list[dict]:
    con = connect_db(db, readonly=True)
    try:
        return [{"orderId": o, "symbol": s, "status": st, "updated": datetime.fromtimestamp(u / 1e9, KST).isoformat(
            timespec="seconds")} for o, s, st, _, u in con.execute("SELECT * FROM orders_latest ORDER BY updated_ns DESC")]
    finally:
        con.close()


# --------------------------------------------------------------------------- 보관 (Parquet) · 정리
ARCHIVE_TABLES = {
    # 테이블: (시간 열, 단위(초당), Parquet 변환 SELECT)
    "trades": ("ts_ns", 1e9, "make_timestamp_ns(ts_ns) AS ts, make_timestamp_ns(recv_ns) AS recv_ts, symbol, market, "
               "CAST(price_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS price, "
               "CAST(volume_e6 AS DECIMAL(18,0)) * CAST(0.000001 AS DECIMAL(7,6)) AS volume"),
    "bars_1m": ("ts_m", 1 / 60, "make_timestamp(ts_m * 60000000::BIGINT) AS ts, symbol, "
                "CAST(o_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS open, "
                "CAST(h_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS high, "
                "CAST(l_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS low, "
                "CAST(c_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS close, "
                "CAST(v_e6 AS DECIMAL(18,0)) * CAST(0.000001 AS DECIMAL(7,6)) AS volume, n AS trades, "
                "CAST(amt_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS amount, "
                "CASE WHEN v_e6 > 0 THEN CAST(amt_e4 * 1000000 // v_e6 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) END AS vwap"),
    "book_top": ("ts_ns", 1e9, "make_timestamp_ns(ts_ns) AS ts, make_timestamp_ns(recv_ns) AS recv_ts, symbol, "
                 "CAST(bid1_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS bid1, "
                 "CAST(bid1_q_e6 AS DECIMAL(18,0)) * CAST(0.000001 AS DECIMAL(7,6)) AS bid1_qty, "
                 "CAST(ask1_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS ask1, "
                 "CAST(ask1_q_e6 AS DECIMAL(18,0)) * CAST(0.000001 AS DECIMAL(7,6)) AS ask1_qty, "
                 "CAST(spread_e4 AS DECIMAL(18,0)) * CAST(0.0001 AS DECIMAL(5,4)) AS spread, crossed > 0 AS crossed, crossed = 1 AS locked, "
                 "bids5, asks5"),
}


def archive(db: Path, out_dir: Path, date: str, prune_days: int | None = None) -> dict:
    """date(KST, YYYY-MM-DD) 하루치를 Parquet 로 옮기고, prune_days 보다 오래된 SQLite 행을 지운다."""
    try:
        import duckdb
    except ImportError:
        raise RuntimeError("Parquet 보관에는 duckdb 가 필요합니다: uv tool install --editable '.[mcp,analytics]' --force")
    day = datetime.fromisoformat(date).replace(tzinfo=KST)
    start, end = day.timestamp(), (day + timedelta(days=1)).timestamp()
    con = connect_db(db, readonly=True)
    duck = duckdb.connect()
    out = {"date": date, "files": {}}
    tmp_dir = Path(out_dir) / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for table, (col, per_sec, select) in ARCHIVE_TABLES.items():
            lo, hi = int(start * per_sec), int(end * per_sec)
            cur = con.execute(f"SELECT * FROM {table} WHERE {col} >= ? AND {col} < ?", (lo, hi))
            names = [d[0] for d in cur.description]
            tmp = tmp_dir / f"{table}.csv"
            n = 0
            with open(tmp, "w", newline="") as f:     # 정수·문자열만 있으므로 CSV 를 거쳐도 값이 바뀌지 않음
                w = csv.writer(f)
                w.writerow(names)
                for row in cur:
                    w.writerow(row)
                    n += 1
            if not n:
                tmp.unlink()
                continue
            dest = Path(out_dir) / table / f"date={date}"
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / "part-0.parquet"
            types = {c: "VARCHAR" if c in ("symbol", "market", "bids5", "asks5") else
                     "HUGEINT" if c == "amt_e4" else "BIGINT" for c in names}
            duck.execute(f"COPY (SELECT {select} FROM read_csv('{tmp}', header=true, columns={types!r})) "
                         f"TO '{target}' (FORMAT parquet, COMPRESSION zstd)")
            tmp.unlink()
            out["files"][table] = {"path": str(target), "rows": n}
    finally:
        con.close()
        duck.close()
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
    if prune_days is not None:
        out["pruned"] = prune(db, prune_days)
    return out


def prune(db: Path, keep_days: int) -> dict:
    cutoff = time.time() - keep_days * 86400
    con = connect_db(db)
    try:
        with con:
            res = {
                "trades": con.execute("DELETE FROM trades WHERE ts_ns < ?", (int(cutoff * 1e9),)).rowcount,
                "bars_1s": con.execute("DELETE FROM bars_1s WHERE ts_s < ?", (int(cutoff),)).rowcount,
                "bars_1m": con.execute("DELETE FROM bars_1m WHERE ts_m < ?", (int(cutoff / 60),)).rowcount,
                "book_top": con.execute("DELETE FROM book_top WHERE ts_ns < ?", (int(cutoff * 1e9),)).rowcount,
            }
        return res
    finally:
        con.close()

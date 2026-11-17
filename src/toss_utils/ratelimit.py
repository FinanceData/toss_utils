"""API 그룹별 초당 요청 수 제한 (공식 overview.md 기준, 2026-11-17).

프로세스 내부 token bucket 이다. 여러 프로세스가 동시에 호출하면 합산 한도를 넘을 수 있으니,
그 경우 서버 429 + Retry-After 재시도(client.py)가 최종 안전망이 된다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

LIMITS: dict[str, float] = {
    "AUTH": 5, "ACCOUNT": 1, "ASSET": 5, "TRANSACTION": 5,
    "STOCK": 5, "STOCK_ALL": 1, "STOCK_TRADING_TREND": 10,
    "MARKET_INFO": 3, "MARKET_DATA": 15, "MARKET_DATA_CHART": 20, "RANKING": 5,
    "MARKET_INDICATOR_PRICE": 10, "MARKET_INDICATOR": 10, "MARKET_INDICATOR_CHART": 5,
    "SECTOR": 5, "SECTOR_RANKING": 5,
    "ORDER": 10, "ORDER_HISTORY": 5, "ORDER_INFO": 6,
    "CONDITIONAL_ORDER": 5, "CONDITIONAL_ORDER_HISTORY": 10,
}
# 09:00~09:10 KST 피크 시간 한도
PEAK_LIMITS: dict[str, float] = {"ORDER_INFO": 3, "ORDER": 10}


def _is_peak(now: datetime | None = None) -> bool:
    t = (now or datetime.now(KST)).astimezone(KST)
    return t.hour == 9 and t.minute < 10


class RateLimiter:
    def __init__(self, limits: dict[str, float] | None = None, clock=time.monotonic, sleep=time.sleep):
        self.limits = dict(limits or LIMITS)
        self._clock, self._sleep = clock, sleep
        self._buckets: dict[str, tuple[float, float]] = {}  # group -> (tokens, last_ts)
        self._lock = threading.Lock()

    def _rate(self, group: str) -> float:
        if _is_peak() and group in PEAK_LIMITS:
            return PEAK_LIMITS[group]
        return self.limits.get(group, 1)

    def acquire(self, group: str) -> None:
        while True:
            with self._lock:
                rate = self._rate(group)
                now = self._clock()
                tokens, last = self._buckets.get(group, (rate, now))
                tokens = min(rate, tokens + (now - last) * rate)
                if tokens >= 1:
                    self._buckets[group] = (tokens - 1, now)
                    return
                self._buckets[group] = (tokens, now)
                wait = (1 - tokens) / rate
            self._sleep(wait)

    def observe(self, group: str, remaining: str | None) -> None:
        """응답 헤더 X-RateLimit-Remaining 으로 버킷을 서버 상태에 맞춰 보정(더 보수적인 쪽으로만)."""
        if remaining is None:
            return
        try:
            r = float(remaining)
        except ValueError:
            return
        with self._lock:
            tokens, last = self._buckets.get(group, (self._rate(group), self._clock()))
            self._buckets[group] = (min(tokens, r), last)

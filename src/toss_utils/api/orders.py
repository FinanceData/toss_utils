"""주문·조건주문 **조회 전용**. 생성·정정·취소는 안전장치(safety 계층)와 함께 다음 단계에서 추가한다."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from ..client import TossClient


class OrdersReadAPI:
    def __init__(self, client: "TossClient"):
        self._c = client

    def _get(self, path, group, account=None, **params):
        return self._c.request("GET", path, group=group, params=params, account=account, needs_account=True)

    def list(self, status: str = "OPEN", symbol: str | None = None, from_: str | None = None,
             to: str | None = None, cursor: str | None = None, limit: int | None = None,
             account: int | None = None):
        """status=OPEN 은 전량 반환(cursor·limit 무시), CLOSED 는 커서 페이징(최대 100)."""
        status = status.upper()
        if status not in ("OPEN", "CLOSED"):
            raise ValueError("status 는 OPEN 또는 CLOSED")
        return self._get("/api/v1/orders", "ORDER_HISTORY", account, status=status, symbol=symbol,
                         **{"from": from_}, to=to, cursor=cursor, limit=limit)

    def iter_closed(self, symbol=None, from_=None, to=None, account=None) -> Iterator[dict]:
        cursor = None
        while True:
            page = self.list("CLOSED", symbol, from_, to, cursor, 100, account)
            yield from page.get("orders", [])
            cursor = page.get("nextCursor")
            if not page.get("hasNext") or not cursor:
                return

    def get(self, order_id: str, account: int | None = None):
        return self._get(f"/api/v1/orders/{order_id}", "ORDER_HISTORY", account)

    def conditional_list(self, status: str = "OPEN", symbol: str | None = None, cursor: str | None = None,
                         limit: int | None = None, account: int | None = None):
        return self._get("/api/v1/conditional-orders", "CONDITIONAL_ORDER_HISTORY", account,
                         status=status.upper(), symbol=symbol, cursor=cursor, limit=limit)

    def conditional_get(self, conditional_order_id: str, account: int | None = None):
        return self._get(f"/api/v1/conditional-orders/{conditional_order_id}", "CONDITIONAL_ORDER_HISTORY", account)

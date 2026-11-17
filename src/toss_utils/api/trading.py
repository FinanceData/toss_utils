"""원시 쓰기 API (주문·조건주문 생성·정정·취소).

⚠️ 안전장치가 없다. 애플리케이션·에이전트는 이 모듈 대신 `client.trader`(safety.trader.Trader)를 사용한다.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import TossClient


class TradingAPI:
    def __init__(self, client: "TossClient"):
        self._c = client

    def _req(self, method, path, group, account=None, json=None, idempotent=False):
        return self._c.request(method, path, group=group, json=json, account=account,
                               needs_account=True, idempotent=idempotent)

    # ---- 일반 주문
    def create_order(self, body: dict, account: int | None = None):
        """body 에 clientOrderId 가 있으면 네트워크 오류·5xx·409 request-in-progress 시 같은 키로 재시도(멱등)."""
        return self._req("POST", "/api/v1/orders", "ORDER", account, body, idempotent=bool(body.get("clientOrderId")))

    def modify_order(self, order_id: str, body: dict, account: int | None = None):
        """응답의 orderId 는 원주문과 다른 새 ID."""
        return self._req("POST", f"/api/v1/orders/{order_id}/modify", "ORDER", account, body)

    def cancel_order(self, order_id: str, account: int | None = None):
        return self._req("POST", f"/api/v1/orders/{order_id}/cancel", "ORDER", account, {})

    # ---- 조건주문
    def create_conditional(self, body: dict, account: int | None = None):
        return self._req("POST", "/api/v1/conditional-orders", "CONDITIONAL_ORDER", account, body,
                         idempotent=bool(body.get("clientOrderId")))

    def modify_conditional(self, conditional_order_id: str, body: dict, account: int | None = None):
        """수정 = 기존 취소 + 새로 생성. 응답의 conditionalOrderId 가 새 ID."""
        return self._req("POST", f"/api/v1/conditional-orders/{conditional_order_id}/modify",
                         "CONDITIONAL_ORDER", account, body)

    def cancel_conditional(self, conditional_order_id: str, account: int | None = None):
        """이미 접수된(ORDERED) 주문은 함께 취소되지 않는다."""
        return self._req("DELETE", f"/api/v1/conditional-orders/{conditional_order_id}", "CONDITIONAL_ORDER", account)

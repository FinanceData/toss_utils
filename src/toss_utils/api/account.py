"""계좌·자산 카테고리 + 거래 가능 정보(매수가능금액·판매가능수량·수수료). 모두 읽기 전용.
accounts 를 제외하면 X-Tossinvest-Account 헤더가 필요하다 (account=None 이면 기본 계좌)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Sequence

if TYPE_CHECKING:
    from ..client import TossClient


class AccountAPI:
    def __init__(self, client: "TossClient"):
        self._c = client

    def _get(self, path, group, account=None, **params):
        return self._c.request("GET", path, group=group, params=params, account=account, needs_account=True)

    def accounts(self) -> list[dict]:
        """현재는 종합매매(BROKERAGE)만 반환. 초당 1회 한도 → accountSeq 는 캐시해서 쓰세요."""
        return self._c.request("GET", "/api/v1/accounts", group="ACCOUNT")

    def holdings(self, symbol: str | None = None, account: int | None = None):
        return self._get("/api/v1/holdings", "ASSET", account, symbol=symbol)

    def transactions(self, types: Sequence[str] | None = None, from_: str | None = None, to: str | None = None,
                     cursor: str | None = None, limit: int | None = None, account: int | None = None):
        """날짜 필터는 결제일 기준. 반환: {"transactions": [...], "nextCursor", "hasNext"}"""
        return self._get("/api/v1/transactions", "TRANSACTION", account, transactionTypes=types,
                         **{"from": from_}, to=to, cursor=cursor, limit=limit)

    def iter_transactions(self, types=None, from_=None, to=None, account=None, page_size: int = 100) -> Iterator[dict]:
        cursor = None
        while True:
            page = self.transactions(types, from_, to, cursor, page_size, account)
            yield from page.get("transactions", [])
            cursor = page.get("nextCursor")
            if not page.get("hasNext") or not cursor:
                return

    # ---- Order Info (읽기 전용)
    def buying_power(self, currency: str = "KRW", account: int | None = None):
        """현금 기반 매수 가능 금액. 미국 주문은 자동환전이 없으니 currency=USD 로 확인."""
        return self._get("/api/v1/buying-power", "ORDER_INFO", account, currency=currency)

    def sellable_quantity(self, symbol: str, account: int | None = None):
        return self._get("/api/v1/sellable-quantity", "ORDER_INFO", account, symbol=symbol)

    def commissions(self, account: int | None = None):
        return self._get("/api/v1/commissions", "ORDER_INFO", account)

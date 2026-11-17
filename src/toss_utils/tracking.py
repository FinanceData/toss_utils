"""주문 ID 계보(lineage) 추적.

토스증권은 정정·취소 요청마다 새 orderId 를 발급한다. 그리고 (2026-11-17 실측) 정정·취소 **전의** ID 로
`GET /orders/{id}` 를 하면 예전 상태(PENDING)가 갱신되지 않은 채 나온다.

그래서 이 라이브러리로 보낸 생성·정정·취소의 ID 연결을 ~/.config/toss/lineage.jsonl 에 기록하고,
어떤 ID 를 받든 같은 계보의 **가장 마지막 ID** 와 **목록 API 로 교차 확인한 상태**를 돌려준다.
앱·WTS 에서 한 정정·취소는 기록되지 않으므로, 그 경우엔 목록 교차 확인으로 stale 여부만 경고한다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import TossAPIError

if TYPE_CHECKING:
    from .client import TossClient

OPEN_STATUSES = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}


class Lineage:
    def __init__(self, home: Path):
        self.path = home / "lineage.jsonl"

    def record(self, event: str, new_id: str, from_id: str | None = None, **extra) -> None:
        """event: create | modify | cancel. from_id 는 정정·취소 대상(직전) ID."""
        rec = {"ts": time.time(), "event": event, "from": from_id, "to": new_id, **extra}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _edges(self) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def chain(self, order_id: str) -> list[dict]:
        """order_id 가 속한 계보를 처음(create)부터 마지막까지 [{id, event, ts}] 로 돌려준다. 기록이 없으면 []."""
        edges = self._edges()
        parent = {e["to"]: e for e in edges if e.get("from")}
        children: dict[str, list[dict]] = {}
        for e in edges:
            if e.get("from"):
                children.setdefault(e["from"], []).append(e)
        known = {e["to"] for e in edges} | set(children)
        if order_id not in known:
            return []
        root = order_id
        while root in parent:
            root = parent[root]["from"]
        created = next((e for e in edges if e["to"] == root and not e.get("from")), None)
        seq = [{"id": root, "event": created["event"] if created else "unknown", "ts": created["ts"] if created else None}]
        cur = root
        while cur in children:
            e = max(children[cur], key=lambda x: x["ts"])  # 같은 ID 에서 여러 번 갈라지면 가장 최근 것
            seq.append({"id": e["to"], "event": e["event"], "ts": e["ts"]})
            cur = e["to"]
        return seq


def track(client: "TossClient", order_id: str, account: int | None = None) -> dict:
    """어떤 ID 를 주든 최신 ID 와 신뢰할 수 있는 상태를 돌려준다.

    반환: {queriedId, latestOrderId, chain, status, order, source, warnings}
      source: detail(최신 ID 상세) | open_list | closed_list
    """
    lin = Lineage(client.home)
    chain = lin.chain(order_id)
    ids = [x["id"] for x in chain] or [order_id]
    latest = ids[-1]
    warnings: list[str] = []
    if latest != order_id:
        warnings.append(f"조회한 ID 이후 {len(ids) - ids.index(order_id) - 1}번 정정·취소되어 최신 ID 로 조회했습니다.")

    detail = client.orders.get(latest, account)
    symbol = detail.get("symbol")
    result = {"queriedId": order_id, "latestOrderId": latest, "chain": chain or None,
              "status": detail.get("status"), "order": detail, "source": "detail", "warnings": warnings}

    if detail.get("status") in OPEN_STATUSES:
        # 상세가 '진행 중'이라고 해도 예전 ID 면 갱신이 안 된 것일 수 있음 → OPEN 목록으로 교차 확인
        opens = client.orders.list("OPEN", symbol, account=account).get("orders", [])
        hit = next((o for o in opens if o["orderId"] in ids), None)
        if hit:
            result.update(status=hit["status"], order=hit, source="open_list", latestOrderId=hit["orderId"])
        else:
            result["source"] = "detail_unverified"
            try:
                closed = client.orders.list("CLOSED", symbol, limit=20, account=account).get("orders", [])
            except TossAPIError:
                closed = []
            hit = next((o for o in closed if o["orderId"] in ids), None)
            if hit:
                result.update(status=hit["status"], order=hit, source="closed_list", latestOrderId=hit["orderId"])
            else:
                result["status"] = "UNKNOWN"
                warnings.append(
                    "상세 조회는 진행 중이라고 하지만 진행 중 목록에 없습니다. 이 ID 이후 다른 곳(앱·WTS 등)에서 "
                    "정정·취소되어 상세 정보가 갱신되지 않았을 가능성이 큽니다. `toss order list --symbol "
                    f"{symbol}` 와 `--closed` 로 확인하세요.")
                result["recentClosedForSymbol"] = [
                    {"orderId": o["orderId"], "side": o.get("side"), "price": o.get("price"),
                     "quantity": o.get("quantity"), "status": o.get("status"), "canceledAt": o.get("canceledAt")}
                    for o in closed[:5]]
    return result

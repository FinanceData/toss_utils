"""안전장치를 거치는 주문 계층: 미리보기(prepare) → 확인 ID → 전송(submit).

- prepare_*: 시세·종목·잔고를 조회해 검증하고 미리보기를 만든다. **서버에 주문을 보내지 않는다.**
  모든 검사를 통과하면 pending/<confirm_id>.json 에 요청 본문을 저장하고 confirm_id 를 돌려준다.
- submit(confirm_id): 저장된 본문을 그대로 1회만 전송한다. 직전에 킬 스위치·정책 한도를 다시 검사한다.
- cancel*: 위험을 줄이는 동작이라 킬 스위치가 켜져 있어도 허용하고 확인 단계가 없다.

에이전트는 prepare 결과를 사람에게 보여주고 승인을 받은 뒤에만 submit 을 호출해야 한다.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import SafetyError, TossAPIError
from ..tracking import Lineage, track
from .policy import KST, Ledger, Policy, is_killed

if TYPE_CHECKING:
    from ..client import TossClient

HIGH_VALUE_KRW = Decimal(100_000_000)   # 1억 이상은 confirmHighValueOrder 필요
OPEN_STATUSES = {"PENDING", "PARTIAL_FILLED"}


def _dec(v, name: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} 값이 숫자가 아닙니다: {v!r}")
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} 은 0보다 커야 합니다: {v!r}")
    return d


def _decimals(d: Decimal) -> int:
    return max(0, -d.normalize().as_tuple().exponent)


def _check(checks: list, name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


class Trader:
    def __init__(self, client: "TossClient"):
        self.c = client
        self.home: Path = client.home
        self.ledger = Ledger(self.home)
        self.lineage = Lineage(self.home)
        self.pending_dir = self.home / "pending"

    @property
    def policy(self) -> Policy:
        return Policy.load(self.home)   # 매번 새로 읽어 사람이 바꾼 값이 즉시 반영되게 함

    # ------------------------------------------------------------------ 공통 조회
    def _instrument(self, symbol: str) -> dict:
        symbol = symbol.strip().upper()
        info = self.c.market.stocks([symbol])
        if not info:
            raise ValueError(f"종목을 찾을 수 없습니다: {symbol}")
        i = info[0]
        currency = i["currency"]
        last = Decimal(self.c.market.prices([symbol])[0]["lastPrice"])
        fx = Decimal(1) if currency == "KRW" else Decimal(self.c.market.exchange_rate("USD", "KRW")["rate"])
        return {"symbol": i["symbol"], "name": i.get("name"), "currency": currency,
                "country": "KR" if currency == "KRW" else "US", "status": i.get("status"),
                "kr": i.get("koreanMarketDetail") or {}, "last": last, "fx": fx}

    def _commission_rate(self, country: str, account) -> Decimal:
        try:
            for x in self.c.account.commissions(account):
                if x.get("marketCountry") == country:
                    return Decimal(x["commissionRate"])
        except TossAPIError:
            pass
        return Decimal(0)

    def _policy_checks(self, checks: list, inst: dict, policy: Policy, order_krw: Decimal,
                       daily_add_krw: Decimal, uses_market: bool, check_warnings: bool = True) -> None:
        sym = inst["symbol"]
        _check(checks, "kill_switch", not is_killed(self.home),
               "킬 스위치 켜짐 (toss policy unkill 로 해제)" if is_killed(self.home) else "off")
        allowed = policy.allowed_symbols is None or sym in [s.upper() for s in policy.allowed_symbols]
        _check(checks, "symbol_allowed", allowed and sym not in [s.upper() for s in policy.denied_symbols],
               "허용 종목 목록/차단 목록 확인")
        kr = inst["kr"]
        tradable = inst["status"] == "ACTIVE" and not kr.get("krxTradingSuspended") and not kr.get("liquidationTrading")
        _check(checks, "tradable", tradable, f"status={inst['status']}, 거래정지/정리매매 여부")
        if check_warnings:
            warns = self.c.market.warnings(sym)
            _check(checks, "no_buy_warnings", policy.allow_warning_stocks or not warns,
                   ", ".join(w.get("warningType", "?") for w in warns) or "없음")
        if uses_market:
            _check(checks, "market_order_allowed", policy.allow_market_orders,
                   "시장가 허용 정책: " + str(policy.allow_market_orders))
        _check(checks, "max_order_krw", order_krw <= policy.max_order_krw,
               f"추정 {order_krw:,.0f}원 / 한도 {policy.max_order_krw:,}원")
        used = self.ledger.today_krw()
        _check(checks, "max_daily_krw", used + daily_add_krw <= policy.max_daily_krw,
               f"오늘 사용 {used:,.0f} + 이번 {daily_add_krw:,.0f} / 한도 {policy.max_daily_krw:,}원")

    def _save_pending(self, preview: dict, record: dict) -> dict:
        policy = self.policy
        cid = record["confirm_id"]
        record["expires_at"] = time.time() + policy.confirm_ttl_sec
        record["account"] = self.c._resolve_account(record.get("account"))
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        p = self.pending_dir / f"{cid}.json"
        p.write_text(json.dumps(record, ensure_ascii=False, default=str))
        os.chmod(p, 0o600)
        preview["confirm_id"] = cid
        preview["expires_in_sec"] = policy.confirm_ttl_sec
        return preview

    @staticmethod
    def _finish(preview: dict, checks: list) -> dict:
        preview["checks"] = checks
        preview["ok"] = all(c["ok"] for c in checks)
        return preview

    # ------------------------------------------------------------------ 일반 주문
    def prepare_order(self, symbol: str, side: str, order_type: str = "LIMIT", quantity=None, price=None,
                      amount=None, time_in_force: str | None = None, account: int | None = None) -> dict:
        side, order_type = side.upper(), order_type.upper()
        tif = time_in_force.upper() if time_in_force else None
        if side not in ("BUY", "SELL"):
            raise ValueError("side 는 BUY 또는 SELL")
        if order_type not in ("LIMIT", "MARKET"):
            raise ValueError("order_type 은 LIMIT 또는 MARKET")
        if (quantity is None) == (amount is None):
            raise ValueError("quantity(수량) 와 amount(금액, 미국 시장가 전용) 중 정확히 하나를 지정하세요.")
        if order_type == "LIMIT" and price is None:
            raise ValueError("지정가(LIMIT)는 price 가 필요합니다.")
        if order_type == "MARKET" and price is not None:
            raise ValueError("시장가(MARKET)에는 price 를 넣을 수 없습니다.")
        q = _dec(quantity, "quantity") if quantity is not None else None
        amt = _dec(amount, "amount") if amount is not None else None
        px = _dec(price, "price") if price is not None else None

        inst = self._instrument(symbol)
        sym, country, cur, fx, last = inst["symbol"], inst["country"], inst["currency"], inst["fx"], inst["last"]
        if amt is not None and (country != "US" or order_type != "MARKET"):
            raise ValueError("금액 주문(amount)은 미국 주식 시장가(MARKET)만 가능합니다.")
        if q is not None and q != q.to_integral_value():
            if not (country == "US" and order_type == "MARKET" and side == "SELL"):
                raise ValueError("소수점 수량은 미국 주식 시장가 매도만 가능합니다. 소수점 매수는 amount 를 쓰세요.")
            if _decimals(q) > 6:
                raise ValueError("소수점 수량은 6자리까지입니다.")
        if tif == "CLS" and not (country == "US" and order_type == "LIMIT"):
            raise ValueError("CLS(장마감)는 미국 주식 지정가만 가능합니다.")
        if tif == "OPG" and country != "KR":
            raise ValueError("OPG(시가단일가)는 국내 주식만 가능합니다.")
        if tif and amt is not None:
            raise ValueError("금액 주문에는 time_in_force 를 지정할 수 없습니다.")

        policy = self.policy
        if amt is not None:
            est = amt
        elif order_type == "LIMIT":
            est = q * px
        else:
            est = q * last * (Decimal(1) + Decimal(str(policy.market_slippage)) if side == "BUY" else Decimal(1))
        est_krw = (est * fx).quantize(Decimal(1))
        comm = (est * self._commission_rate(country, account)).quantize(Decimal("0.01"))

        checks: list = []
        self._policy_checks(checks, inst, policy, est_krw, est_krw, order_type == "MARKET",
                            check_warnings=(side == "BUY"))
        if px is not None:
            if country == "KR":
                lim = self.c.market.price_limits(sym)
                lo, hi = lim.get("lowerLimitPrice"), lim.get("upperLimitPrice")
                ok = (px == px.to_integral_value()) and (lo is None or px >= Decimal(lo)) and (hi is None or px <= Decimal(hi))
                _check(checks, "price_in_limits", ok, f"정수·상하한가 {lo}~{hi}")
            else:
                maxd = 2 if px >= 1 else 4
                _check(checks, "price_precision", _decimals(px) <= maxd,
                       f"미국 가격은 ${'1 이상 소수 2' if maxd == 2 else '1 미만 소수 4'}자리까지 (초과분은 서버가 잘라냄)")
        if side == "BUY":
            bp = Decimal(self.c.account.buying_power(cur, account)["cashBuyingPower"])
            _check(checks, "buying_power", bp >= est + comm, f"필요 {est + comm} {cur} / 가능 {bp} {cur}")
        else:
            sq = Decimal(self.c.account.sellable_quantity(sym, account)["sellableQuantity"])
            need = q if q is not None else (amt / last)
            _check(checks, "sellable_quantity", sq >= need, f"필요 {need.normalize()} / 가능 {sq}")
        if country == "KR":
            opposite = "SELL" if side == "BUY" else "BUY"
            opens = self.c.orders.list("OPEN", sym, account=account).get("orders", [])
            clash = [o["orderId"] for o in opens if o.get("side") == opposite]
            _check(checks, "no_opposite_pending", not clash,
                   f"같은 종목 반대 방향 미체결 {len(clash)}건 (국내는 409 로 거부됨)")

        cid = str(uuid.uuid4())
        body = {"clientOrderId": cid, "symbol": sym, "side": side, "orderType": order_type}
        if amt is not None:
            body["orderAmount"] = str(amt)
        else:
            body["quantity"] = str(q.normalize()) if q != q.to_integral_value() else str(int(q))
            if tif:
                body["timeInForce"] = tif
            if px is not None:
                body["price"] = str(px)
        if est_krw >= HIGH_VALUE_KRW:
            body["confirmHighValueOrder"] = True   # 정책 한도를 통과한 경우에만 여기까지 옴

        preview = {
            "action": "order", "symbol": sym, "name": inst["name"], "country": country,
            "side": side, "orderType": order_type, "timeInForce": tif or ("DAY" if amt is None else None),
            "quantity": body.get("quantity"), "amount": body.get("orderAmount"), "price": body.get("price"),
            "lastPrice": str(last), "estimated": {"amount": str(est.quantize(Decimal('0.01'))), "currency": cur,
                                                   "krw": str(est_krw), "commission": str(comm)},
            "fxUsdKrw": str(fx) if cur == "USD" else None, "body": body,
        }
        self._finish(preview, checks)
        if preview["ok"]:
            self._save_pending(preview, {"kind": "order", "confirm_id": cid, "body": body, "symbol": sym,
                                         "side": side, "est_krw": str(est_krw), "order_krw": str(est_krw),
                                         "account": account})
        return preview

    def prepare_modify(self, order_id: str, price=None, quantity=None, order_type: str = "LIMIT",
                       account: int | None = None) -> dict:
        order_type = order_type.upper()
        if order_type == "LIMIT" and price is None:
            raise ValueError("지정가 정정은 price 가 필요합니다.")
        if order_type == "MARKET" and price is not None:
            raise ValueError("시장가 정정에는 price 를 넣을 수 없습니다.")
        t = track(self.c, order_id, account)       # 예전 ID 를 받아도 최신 ID·실제 상태로 판단
        o, order_id = t["order"], t["latestOrderId"]
        if t["status"] not in OPEN_STATUSES:
            raise ValueError(f"정정할 수 없는 상태입니다: {t['status']} (최신 ID {order_id}). " + " ".join(t["warnings"]))
        inst = self._instrument(o["symbol"])
        country, fx, last = inst["country"], inst["fx"], inst["last"]
        remaining = Decimal(o["quantity"]) - Decimal(o.get("execution", {}).get("filledQuantity") or 0)
        if country == "US" and quantity is not None:
            raise ValueError("미국 주식은 가격만 정정할 수 있습니다 (수량 정정 불가).")
        q = _dec(quantity, "quantity") if quantity is not None else remaining
        if country == "KR" and q != q.to_integral_value():
            raise ValueError("국내 정정 수량은 정수여야 합니다.")
        px = _dec(price, "price") if price is not None else None
        policy = self.policy
        new_est = q * px if px is not None else q * last * (1 + Decimal(str(policy.market_slippage)))
        old_est = remaining * (Decimal(o["price"]) if o.get("price") else last)
        new_krw = (new_est * fx).quantize(Decimal(1))
        delta_krw = (max(Decimal(0), new_est - old_est) * fx).quantize(Decimal(1))

        checks: list = []
        self._policy_checks(checks, inst, policy, new_krw, delta_krw, order_type == "MARKET", check_warnings=False)
        if px is not None and country == "KR":
            lim = self.c.market.price_limits(inst["symbol"])
            lo, hi = lim.get("lowerLimitPrice"), lim.get("upperLimitPrice")
            _check(checks, "price_in_limits", (lo is None or px >= Decimal(lo)) and (hi is None or px <= Decimal(hi)),
                   f"상하한가 {lo}~{hi}")
        body = {"orderType": order_type}
        if px is not None:
            body["price"] = str(px)
        if country == "KR":
            body["quantity"] = str(int(q))
        if new_krw >= HIGH_VALUE_KRW:
            body["confirmHighValueOrder"] = True
        preview = {"action": "modify", "orderId": order_id, "symbol": inst["symbol"], "name": inst["name"],
                   "side": o.get("side"), "before": {"price": o.get("price"), "remaining": str(remaining)},
                   "after": {"orderType": order_type, "price": body.get("price"), "quantity": str(q)},
                   "estimated": {"krw": str(new_krw), "daily_added_krw": str(delta_krw)}, "body": body,
                   "note": "정정이 반영되면 새 orderId 가 발급됩니다."}
        self._finish(preview, checks)
        if preview["ok"]:
            cid = str(uuid.uuid4())
            self._save_pending(preview, {"kind": "modify", "confirm_id": cid, "order_id": order_id, "body": body,
                                         "symbol": inst["symbol"], "est_krw": str(delta_krw),
                                         "order_krw": str(new_krw), "account": account})
        return preview

    # ------------------------------------------------------------------ 조건주문
    def prepare_conditional(self, symbol: str, type: str, quantity, first: dict, second: dict | None = None,
                            order_type: str = "LIMIT", expire_date: str | None = None,
                            account: int | None = None) -> dict:
        """first/second = {"orderSide": BUY|SELL, "triggerPrice": "...", "orderPrice": "..."(LIMIT 일 때)}"""
        type, order_type = type.upper(), order_type.upper()
        if type not in ("SINGLE", "OCO", "OTO"):
            raise ValueError("type 은 SINGLE, OCO, OTO 중 하나")
        if type == "SINGLE" and second is not None:
            raise ValueError("SINGLE 은 second 조건을 넣을 수 없습니다.")
        if type != "SINGLE" and (second is None or order_type != "LIMIT"):
            raise ValueError("OCO/OTO 는 second 조건이 필요하고 지정가(LIMIT)만 가능합니다.")
        q = _dec(quantity, "quantity")
        today = datetime.now(KST).date()
        exp = date.fromisoformat(expire_date) if expire_date else today + timedelta(days=30)
        if not (today <= exp <= today + timedelta(days=365)):
            raise ValueError("expire_date 는 오늘부터 1년 이내여야 합니다.")
        legs = [first] + ([second] if second else [])
        norm = []
        for i, leg in enumerate(legs, 1):
            side = str(leg.get("orderSide", "")).upper()
            if side not in ("BUY", "SELL"):
                raise ValueError(f"{i}번째 조건의 orderSide 는 BUY/SELL")
            trig = _dec(leg.get("triggerPrice"), "triggerPrice")
            op = leg.get("orderPrice")
            if order_type == "LIMIT" and op is None:
                raise ValueError("지정가 조건주문은 각 조건에 orderPrice 가 필요합니다.")
            if order_type == "MARKET" and op is not None:
                raise ValueError("시장가 조건주문에는 orderPrice 를 넣을 수 없습니다.")
            norm.append({"orderSide": side, "triggerPrice": trig, "orderPrice": _dec(op, "orderPrice") if op else None})

        inst = self._instrument(symbol)
        sym, country, cur, fx, last = inst["symbol"], inst["country"], inst["currency"], inst["fx"], inst["last"]
        if country == "KR" and q != q.to_integral_value():
            raise ValueError("국내 수량은 정수여야 합니다.")
        if type == "OCO":
            if not all(l["orderSide"] == "SELL" for l in norm):
                raise ValueError("OCO 는 두 조건 모두 SELL 이어야 합니다 (익절·손절).")
            if not (norm[0]["triggerPrice"] > last > norm[1]["triggerPrice"]):
                raise ValueError(f"OCO 는 first 감시가 > 현재가({last}) > second 감시가 여야 합니다.")
        if type == "OTO" and not (norm[0]["orderSide"] == "BUY" and norm[1]["orderSide"] == "SELL"):
            raise ValueError("OTO 는 first=BUY, second=SELL 이어야 합니다.")

        policy = self.policy
        slip = Decimal(str(policy.market_slippage))

        def leg_est(l):
            p = l["orderPrice"] or l["triggerPrice"] * (1 + slip if l["orderSide"] == "BUY" else 1)
            return q * p

        est = max(leg_est(l) for l in norm)
        est_krw = (est * fx).quantize(Decimal(1))
        checks: list = []
        has_buy = any(l["orderSide"] == "BUY" for l in norm)
        self._policy_checks(checks, inst, policy, est_krw, est_krw, order_type == "MARKET", check_warnings=has_buy)
        if has_buy:
            need = max(leg_est(l) for l in norm if l["orderSide"] == "BUY")
            bp = Decimal(self.c.account.buying_power(cur, account)["cashBuyingPower"])
            _check(checks, "buying_power_now", bp >= need,
                   f"현재 기준 필요 {need} / 가능 {bp} {cur} (발동 시점 잔고로 다시 판정됨)")
        if type in ("SINGLE", "OCO") and norm[0]["orderSide"] == "SELL":
            sq = Decimal(self.c.account.sellable_quantity(sym, account)["sellableQuantity"])
            _check(checks, "sellable_quantity", sq >= q, f"필요 {q} / 가능 {sq}")
        if type in ("OCO", "OTO"):
            groups = self.c.orders.conditional_list("OPEN", sym, account=account).get("conditionalOrders", [])
            dup = [g for g in groups if g.get("type") in ("OCO", "OTO")]
            _check(checks, "one_group_per_symbol", not dup, f"이 종목의 진행 중 OCO/OTO {len(dup)}건 (종목당 1개)")

        cid = str(uuid.uuid4())

        def leg_body(l):
            b = {"orderSide": l["orderSide"], "triggerPrice": str(l["triggerPrice"])}
            if l["orderPrice"] is not None:
                b["orderPrice"] = str(l["orderPrice"])
            return b

        body = {"clientOrderId": cid, "symbol": sym, "type": type, "quantity": str(q.normalize()),
                "orderType": order_type, "expireDate": exp.isoformat(), "first": leg_body(norm[0])}
        if len(norm) > 1:
            body["second"] = leg_body(norm[1])
        if est_krw >= HIGH_VALUE_KRW:
            body["confirmHighValueOrder"] = True
        session = "KRX 정규장에서만 발동" if country == "KR" else "데이마켓 포함 모든 거래 세션에서 발동"
        preview = {"action": "conditional", "symbol": sym, "name": inst["name"], "type": type,
                   "lastPrice": str(last), "quantity": body["quantity"], "orderType": order_type,
                   "expireDate": body["expireDate"], "first": body["first"], "second": body.get("second"),
                   "estimated": {"amount": str(est.quantize(Decimal('0.01'))), "currency": cur, "krw": str(est_krw)},
                   "note": f"등록 후 조건 충족 시 자동으로 실제 주문이 나갑니다 ({session}).", "body": body}
        self._finish(preview, checks)
        if preview["ok"]:
            self._save_pending(preview, {"kind": "conditional", "confirm_id": cid, "body": body, "symbol": sym,
                                         "est_krw": str(est_krw), "order_krw": str(est_krw), "account": account})
        return preview

    # ------------------------------------------------------------------ 전송
    def submit(self, confirm_id: str) -> dict:
        src = self.pending_dir / f"{confirm_id}.json"
        claimed = self.pending_dir / f"{confirm_id}.claimed"
        try:
            src.rename(claimed)   # 원자적 선점: 같은 확인 ID 로 두 번 전송되지 않게 함
        except FileNotFoundError:
            raise SafetyError(f"확인 ID 가 없거나 이미 사용·만료되었습니다: {confirm_id}")
        try:
            rec = json.loads(claimed.read_text())
            if time.time() > rec["expires_at"]:
                raise SafetyError("확인 유효 시간이 지났습니다. 다시 미리보기(prepare)를 하세요.")
            policy = self.policy
            checks: list = []
            _check(checks, "kill_switch", not is_killed(self.home), "킬 스위치")
            order_krw, add_krw = Decimal(rec["order_krw"]), Decimal(rec["est_krw"])
            _check(checks, "max_order_krw", order_krw <= policy.max_order_krw,
                   f"{order_krw:,.0f} / {policy.max_order_krw:,}원")
            used = self.ledger.today_krw()
            _check(checks, "max_daily_krw", used + add_krw <= policy.max_daily_krw,
                   f"{used:,.0f}+{add_krw:,.0f} / {policy.max_daily_krw:,}원")
            if not all(c["ok"] for c in checks):
                raise SafetyError("전송 직전 재검사에서 거부되었습니다.", checks)

            kind, body, acct = rec["kind"], rec["body"], rec["account"]
            t = self.c._trading
            try:
                if kind == "order":
                    res = t.create_order(body, acct)
                elif kind == "modify":
                    res = t.modify_order(rec["order_id"], body, acct)
                elif kind == "conditional":
                    res = t.create_conditional(body, acct)
                else:
                    raise SafetyError(f"알 수 없는 종류: {kind}")
            except TossAPIError:
                raise   # 서버가 명확히 거부 → 주문 없음
            except SafetyError:
                raise
            except Exception:
                # 네트워크 등으로 결과를 모름 → 한도 계산에는 보수적으로 포함하고, 주문 목록 확인을 안내
                self.ledger.add(kind=kind, symbol=rec.get("symbol"), est_krw=rec["est_krw"],
                                confirm_id=confirm_id, outcome="unknown")
                raise
            self.ledger.add(kind=kind, symbol=rec.get("symbol"), side=rec.get("side"), est_krw=rec["est_krw"],
                            confirm_id=confirm_id, result=res, outcome="accepted", order_id=rec.get("order_id"))
            new_id = (res or {}).get("orderId")
            if new_id and kind == "order":
                self.lineage.record("create", new_id, clientOrderId=body.get("clientOrderId"), symbol=rec.get("symbol"))
            elif new_id and kind == "modify":
                self.lineage.record("modify", new_id, from_id=rec["order_id"], symbol=rec.get("symbol"))
            self.c.audit.write(event="submit", kind=kind, symbol=rec.get("symbol"), confirm_id=confirm_id, result=res)
            out = {"submitted": kind, "symbol": rec.get("symbol"), "clientOrderId": body.get("clientOrderId"),
                   "result": res}
            if kind == "modify" and new_id:
                out["latestOrderId"] = new_id
                out["note"] = "이후 조회·정정·취소는 latestOrderId 로 하세요 (이전 ID 의 상세 조회는 갱신되지 않음)."
            return out
        finally:
            claimed.unlink(missing_ok=True)

    def pending(self) -> list[dict]:
        out = []
        if not self.pending_dir.exists():
            return out
        for p in sorted(self.pending_dir.glob("*.json")):
            r = json.loads(p.read_text())
            left = int(r["expires_at"] - time.time())
            if left <= 0:
                p.unlink(missing_ok=True)
                continue
            out.append({"confirm_id": r["confirm_id"], "kind": r["kind"], "symbol": r.get("symbol"),
                        "est_krw": r["order_krw"], "expires_in_sec": left, "body": r["body"]})
        return out

    def discard(self, confirm_id: str) -> bool:
        p = self.pending_dir / f"{confirm_id}.json"
        existed = p.exists()
        p.unlink(missing_ok=True)
        return existed

    # ------------------------------------------------------------------ 취소 (위험 감소 → 확인 없음, 킬 스위치 무관)
    def cancel(self, order_id: str, account: int | None = None) -> dict:
        chain = self.lineage.chain(order_id)
        if chain and chain[-1]["id"] != order_id:
            if chain[-1]["event"] == "cancel":
                raise ValueError(f"이미 취소 요청된 주문입니다 (최신 ID {chain[-1]['id']}). `toss order track` 으로 확인하세요.")
            order_id = chain[-1]["id"]          # 정정된 주문이면 최신 ID 로 취소
        res = self.c._trading.cancel_order(order_id, account)
        self.c.audit.write(event="cancel", orderId=order_id, result=res)
        new_id = (res or {}).get("orderId")
        if new_id:
            self.lineage.record("cancel", new_id, from_id=order_id)
        return {"canceled": order_id, "result": res, "latestOrderId": new_id,
                "note": "취소 요청에도 새 orderId 가 발급됩니다. 상태 확인은 `toss order track <아무 ID>` 로 하세요."}

    def cancel_all(self, symbol: str | None = None, account: int | None = None) -> list[dict]:
        out = []
        for o in self.c.orders.list("OPEN", symbol, account=account).get("orders", []):
            if o.get("status") not in OPEN_STATUSES:
                continue
            try:
                out.append(self.cancel(o["orderId"], account))
            except TossAPIError as e:
                out.append({"canceled": None, "orderId": o["orderId"], "error": e.to_dict()})
        return out

    def cancel_conditional(self, conditional_order_id: str, with_order: bool = False,
                           account: int | None = None) -> dict:
        detail = self.c.orders.conditional_get(conditional_order_id, account)
        self.c._trading.cancel_conditional(conditional_order_id, account)
        self.c.audit.write(event="cancel_conditional", conditionalOrderId=conditional_order_id)
        result = {"canceled": conditional_order_id, "status_before": detail.get("status"), "orders_canceled": []}
        triggered = [l.get("triggeredOrderId") for l in (detail.get("first"), detail.get("second"))
                     if l and l.get("triggeredOrderId")]
        if triggered and not with_order:
            result["warning"] = ("이미 접수된 주문은 취소되지 않았습니다: " + ", ".join(triggered) +
                                 " (with_order=True 또는 --with-order 로 함께 취소)")
        if with_order:
            for oid in triggered:
                try:
                    if self.c.orders.get(oid, account).get("status") in OPEN_STATUSES:
                        result["orders_canceled"].append(self.cancel(oid, account))
                except TossAPIError as e:
                    result["orders_canceled"].append({"orderId": oid, "error": e.to_dict()})
        return result

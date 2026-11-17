"""거래 정책(한도·허용 종목 등), 킬 스위치, 일일 장부.

정책 파일: ~/.config/toss/policy.json — 사람이 직접(또는 `toss policy set`) 바꾸는 값.
에이전트 도구에는 정책 변경 기능을 노출하지 않는다.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

KST = timezone(timedelta(hours=9))


@dataclass
class Policy:
    max_order_krw: int = 100_000          # 주문 1건 최대 추정 금액 (원화 환산)
    max_daily_krw: int = 300_000          # 하루(KST) 누적 최대 추정 금액 (매수·매도·조건주문 등록 합산)
    allow_market_orders: bool = False     # 시장가 주문 허용 여부
    market_slippage: float = 0.03         # 시장가 매수 추정 금액에 더하는 여유 (3%)
    allowed_symbols: list[str] | None = None   # None = 제한 없음, 리스트면 그 종목만 허용
    denied_symbols: list[str] = field(default_factory=list)
    allow_warning_stocks: bool = False    # 매수 유의사항(투자경고·VI·정리매매 등) 종목 허용 여부
    confirm_ttl_sec: int = 120            # 미리보기 → 전송 확인 유효 시간
    agent_can_cancel: bool = False        # MCP 등 에이전트 도구에서 취소를 허용할지

    @classmethod
    def load(cls, home: Path) -> "Policy":
        p = home / "policy.json"
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, home: Path) -> Path:
        p = home / "policy.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        return p

    def set(self, key: str, raw: str) -> None:
        f = {f.name: f for f in fields(self)}.get(key)
        if f is None:
            raise ValueError(f"알 수 없는 정책 키: {key}. 가능한 키: {[x.name for x in fields(self)]}")
        cur = getattr(self, key)
        if key in ("allowed_symbols", "denied_symbols"):
            val = None if raw.lower() in ("none", "null", "") and key == "allowed_symbols" else \
                [s.strip().upper() for s in raw.split(",") if s.strip()]
        elif isinstance(cur, bool):
            val = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(cur, int):
            val = int(raw.replace(",", "").replace("_", ""))
        elif isinstance(cur, float):
            val = float(raw)
        else:
            val = raw
        setattr(self, key, val)


def kill_switch_path(home: Path) -> Path:
    return home / "KILL"


def is_killed(home: Path) -> bool:
    """파일이 있으면 새 주문·정정·조건주문 등록을 모두 차단 (취소는 허용)."""
    return kill_switch_path(home).exists()


class Ledger:
    """이 라이브러리로 전송한 쓰기 요청의 추정 금액 장부 (일일 한도 계산용). 앱·WTS 주문은 포함되지 않는다."""

    def __init__(self, home: Path):
        self.path = home / "ledger.jsonl"

    def add(self, **rec) -> None:
        rec = {"ts": time.time(), "date": datetime.now(KST).date().isoformat(), **rec}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def today_krw(self) -> Decimal:
        today = datetime.now(KST).date().isoformat()
        total = Decimal(0)
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return total
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("date") == today:
                total += Decimal(str(r.get("est_krw", 0)))
        return total

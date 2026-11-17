"""append-only JSONL 감사 로그. 요청 본문·응답 본문은 남기지 않고 메타데이터만 기록한다."""
from __future__ import annotations

import json
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = path

    def write(self, **fields) -> None:
        if self.path is None:
            return
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

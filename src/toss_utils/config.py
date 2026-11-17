"""설정: 자격증명(.env/환경변수), 홈 디렉터리, 기본 계좌(config.json)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"


def toss_home() -> Path:
    """토큰 캐시·설정·감사 로그가 놓이는 디렉터리. TOSS_HOME 으로 바꿀 수 있다."""
    return Path(os.environ.get("TOSS_HOME", "~/.config/toss")).expanduser()


def load_dotenv(path: str | Path = ".env") -> None:
    """의존성 없는 최소 .env 로더. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls) -> "Credentials":
        cid, sec = os.environ.get("TOSS_CLIENT_ID", "").strip(), os.environ.get("TOSS_CLIENT_SECRET", "").strip()
        if not cid or not sec:
            raise ConfigError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 없습니다. 현재 폴더의 .env 또는 "
                              f"{toss_home() / '.env'} 에 넣으세요 (.env.example 참고).")
        return cls(cid, sec)


class Settings:
    """~/.config/toss/config.json 에 저장되는 사용자 설정 (현재는 기본 계좌만)."""

    def __init__(self, home: Path | None = None):
        self.path = (home or toss_home()) / "config.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key: str, default=None):
        return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def default_account(self) -> int | None:
        env = os.environ.get("TOSS_ACCOUNT", "").strip()
        if env:
            return int(env)
        v = self.get("account_seq")
        return int(v) if v is not None else None

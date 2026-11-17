"""토큰 발급·캐시.

🔴 클라이언트당 유효한 토큰은 1개 — 새로 발급하면 이전 토큰은 즉시 401 token-revoked.
그래서 모든 프로세스(CLI 여러 개, 에이전트, 데몬)가 같은 캐시 파일을 파일 락 아래에서 공유한다.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from .config import Credentials, toss_home
from .errors import from_response

REFRESH_MARGIN_SEC = 300  # 만료 5분 전이면 새로 발급


class TokenStore:
    def __init__(self, credentials: Credentials, http: httpx.Client, home: Path | None = None):
        self.credentials = credentials
        self.http = http
        self.home = home or toss_home()
        self.cache_path = self.home / "token.json"
        self.lock_path = self.home / "token.lock"

    @contextmanager
    def _locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _read_cache(self) -> dict | None:
        try:
            c = json.loads(self.cache_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        # 다른 client_id 로 발급된 캐시(키 재발급 등)는 쓰지 않는다
        if c.get("client_id") != self.credentials.client_id:
            return None
        return c

    def _issue(self) -> dict:
        r = self.http.post("/oauth2/token", data={
            "grant_type": "client_credentials",
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
        })
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise from_response(r.status_code, body, r.headers.get("x-request-id"))
        c = {"token": body["access_token"], "expires_at": time.time() + int(body["expires_in"]),
             "client_id": self.credentials.client_id, "issued_at": time.time()}
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(c))
        os.chmod(tmp, 0o600)
        tmp.replace(self.cache_path)
        return c

    def get(self) -> str:
        """유효한 캐시 토큰을 돌려주고, 없거나 만료 임박이면 락 안에서 한 번만 발급한다."""
        c = self._read_cache()
        if c and c["expires_at"] - time.time() > REFRESH_MARGIN_SEC:
            return c["token"]
        with self._locked():
            c = self._read_cache()  # 락을 기다리는 동안 다른 프로세스가 발급했을 수 있음
            if c and c["expires_at"] - time.time() > REFRESH_MARGIN_SEC:
                return c["token"]
            return self._issue()["token"]

    def recover(self, rejected_token: str) -> str:
        """401(만료·폐기) 후 호출. 다른 프로세스가 이미 새 토큰을 받았으면 그걸 쓰고, 아니면 재발급.
        (무조건 재발급하면 프로세스끼리 서로의 토큰을 계속 폐기하는 핑퐁이 생긴다.)"""
        with self._locked():
            c = self._read_cache()
            if c and c["token"] != rejected_token and c["expires_at"] - time.time() > REFRESH_MARGIN_SEC:
                return c["token"]
            return self._issue()["token"]

    def status(self) -> dict:
        c = self._read_cache()
        if not c:
            return {"cached": False}
        return {"cached": True, "expires_in_sec": int(c["expires_at"] - time.time()),
                "issued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(c["issued_at"]))}

    def clear(self) -> None:
        with self._locked():
            self.cache_path.unlink(missing_ok=True)

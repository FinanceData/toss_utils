"""httpx.MockTransport 기반 가짜 토스증권 서버. 실제 API 를 호출하지 않고 클라이언트 동작을 검증한다."""
from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs

import httpx


class FakeToss:
    def __init__(self):
        self.lock = threading.Lock()
        self.issued: list[str] = []          # 발급된 토큰 (마지막 것만 유효 — 실제 서버와 동일)
        self.requests: list[httpx.Request] = []
        self.queued: dict[str, list[httpx.Response]] = {}  # path → 우선 반환할 응답들
        self.routes: dict[str, object] = {}

    # 테스트에서 특정 path 의 다음 응답을 강제
    def queue(self, path: str, status: int, body: dict, headers: dict | None = None):
        self.queued.setdefault(path, []).append(httpx.Response(status, json=body, headers=headers or {}))

    def route(self, path: str, result):
        self.routes[path] = result

    @property
    def valid_token(self):
        return self.issued[-1] if self.issued else None

    def handler(self, req: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(req)
            path = req.url.path
            if path == "/oauth2/token":
                form = parse_qs(req.content.decode())
                if form.get("client_secret") != ["secret"]:
                    return httpx.Response(401, json={"error": "invalid_client",
                                                     "error_description": "Client authentication failed: client_secret"})
                tok = f"tok-{len(self.issued) + 1}"
                self.issued.append(tok)
                return httpx.Response(200, json={"access_token": tok, "token_type": "Bearer", "expires_in": 86400})

            auth = req.headers.get("authorization", "")
            if auth != f"Bearer {self.valid_token}":
                code = "token-revoked" if auth.removeprefix("Bearer ") in self.issued else "invalid-token"
                return httpx.Response(401, json={"error": {"requestId": "rid-401", "code": code, "message": "x"}})

            if self.queued.get(path):
                return self.queued[path].pop(0)

            if path in ("/api/v1/holdings", "/api/v1/buying-power") and "x-tossinvest-account" not in req.headers:
                return httpx.Response(400, json={"error": {"requestId": "rid-400", "code": "account-header-required",
                                                           "message": "X-Tossinvest-Account 헤더가 필요합니다."}})
            if path in self.routes:
                r = self.routes[path]
                result = r(req) if callable(r) else r
                return httpx.Response(200, json={"result": result},
                                      headers={"x-request-id": "rid-ok", "x-ratelimit-remaining": "9"})
            return httpx.Response(404, json={"error": {"requestId": "rid-404", "code": "edge-blocked",
                                                       "message": "not found"}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def last(self, path: str) -> httpx.Request:
        return [r for r in self.requests if r.url.path == path][-1]


def body_of(req: httpx.Request):
    return json.loads(req.content) if req.content else None

"""예외 계층. API 에러 envelope {"error": {requestId, code, message, data}} 를 HTTP status 별 예외로 매핑한다."""
from __future__ import annotations

from typing import Any


class TossError(Exception):
    """라이브러리의 모든 예외의 부모."""


class ConfigError(TossError):
    """자격증명·계좌 설정 누락 등 로컬 설정 문제."""


class SafetyError(TossError):
    """안전장치(정책·한도·킬스위치·확인 만료)에 의해 로컬에서 거부됨. 서버로는 아무것도 보내지 않았다."""

    def __init__(self, message: str, checks: list[dict] | None = None, preview: dict | None = None):
        self.checks = checks or []
        self.preview = preview
        super().__init__(message)

    def to_dict(self) -> dict:
        d = {"type": "safety", "message": str(self), "failed": [c for c in self.checks if not c["ok"]]}
        if self.preview:
            d["preview"] = self.preview
        return d


class TossAPIError(TossError):
    status: int = 0

    def __init__(self, status: int, code: str, message: str, data: Any = None, request_id: str | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id
        super().__init__(f"{status} {code}: {message}" + (f" (requestId={request_id})" if request_id else ""))

    def to_dict(self) -> dict:
        return {"status": self.status, "code": self.code, "message": self.message,
                "data": self.data, "requestId": self.request_id}


class InvalidRequestError(TossAPIError): ...      # 400, 414, 415
class AuthError(TossAPIError): ...                # 401 (invalid/expired/revoked token, invalid_client)
class ForbiddenError(TossAPIError): ...           # 403 (ip-not-allowed, access_denied, forbidden)
class NotFoundError(TossAPIError): ...            # 404
class ConflictError(TossAPIError): ...            # 409
class UnprocessableError(TossAPIError): ...       # 422 (주문 거부 사유 대부분)
class RateLimitError(TossAPIError): ...           # 429
class ServerError(TossAPIError): ...              # 5xx (internal-error, maintenance)


_BY_STATUS = {400: InvalidRequestError, 401: AuthError, 403: ForbiddenError, 404: NotFoundError,
              409: ConflictError, 414: InvalidRequestError, 415: InvalidRequestError,
              422: UnprocessableError, 429: RateLimitError}

# 토큰을 다시 받아 한 번 재시도할 가치가 있는 401 코드
TOKEN_RETRY_CODES = {"invalid-token", "expired-token", "token-revoked"}


def from_response(status: int, body: Any, header_request_id: str | None = None) -> TossAPIError:
    """BFF envelope 과 OAuth2 표준 형식({error, error_description}) 둘 다 처리."""
    code, message, data, rid = "unknown", "", None, header_request_id
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code") or code
            message = err.get("message") or ""
            data = err.get("data")
            rid = err.get("requestId") or rid
        elif isinstance(err, str):  # OAuth2: {"error": "invalid_client", "error_description": "..."}
            code, message = err, body.get("error_description", "")
    cls = ServerError if status >= 500 else _BY_STATUS.get(status, TossAPIError)
    return cls(status, code, message, data, rid)

"""HTTP 코어: 인증 헤더, 계좌 헤더, rate limit, envelope 파싱, 재시도, 감사 로그."""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import httpx

from .audit import AuditLog
from .auth import TokenStore
from .config import DEFAULT_BASE_URL, Credentials, Settings, toss_home
from .errors import TOKEN_RETRY_CODES, ConfigError, TossAPIError, from_response
from .ratelimit import RateLimiter

MAX_RETRIES = 3
USER_AGENT = "toss-utils-py/0.1"


def _clean_params(params: dict | None) -> dict | None:
    """None 제거, list → 콤마 구분 문자열, bool → 'true'/'false'."""
    if not params:
        return None
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple, set)):
            v = ",".join(str(x) for x in v)
        out[k] = v
    return out


class TossClient:
    """토스증권 Open API 클라이언트.

    >>> c = TossClient.from_env()
    >>> c.market.prices(["005930", "AAPL"])
    >>> c.account.holdings()          # 기본 계좌 사용 (TOSS_ACCOUNT 또는 `toss account use`)
    """

    def __init__(self, credentials: Credentials, *, base_url: str = DEFAULT_BASE_URL,
                 account_seq: int | None = None, home: Path | None = None,
                 transport: httpx.BaseTransport | None = None, audit: bool = True,
                 limiter: RateLimiter | None = None, sleep=time.sleep, timeout: float = 10.0):
        self.home = home or toss_home()
        self.http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout,
                                 headers={"User-Agent": USER_AGENT})
        self.tokens = TokenStore(credentials, self.http, self.home)
        self.settings = Settings(self.home)
        self.account_seq = account_seq if account_seq is not None else self.settings.default_account()
        self.limiter = limiter or RateLimiter()
        self.audit = AuditLog(self.home / "audit.jsonl" if audit else None)
        self._sleep = sleep

        from .api.account import AccountAPI
        from .api.market import MarketAPI
        from .api.orders import OrdersReadAPI
        from .api.trading import TradingAPI
        self.market = MarketAPI(self)
        self.account = AccountAPI(self)
        self.orders = OrdersReadAPI(self)
        self._trading = TradingAPI(self)  # 원시 쓰기 API — 안전장치 없이 직접 쓰지 말고 self.trader 를 사용
        self._trader = None

    @property
    def trader(self):
        """안전장치(정책·한도·미리보기·확인)를 거치는 주문 계층."""
        if self._trader is None:
            from .safety.trader import Trader
            self._trader = Trader(self)
        return self._trader

    @classmethod
    def from_env(cls, **kw) -> "TossClient":
        return cls(Credentials.from_env(), **kw)

    def close(self) -> None:
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ core
    def _resolve_account(self, account: int | None) -> int:
        seq = account if account is not None else self.account_seq
        if seq is None:
            raise ConfigError("계좌가 지정되지 않았습니다. `toss account list` 로 accountSeq 를 확인한 뒤 "
                              "`toss account use <seq>` 또는 TOSS_ACCOUNT 환경변수를 설정하세요.")
        return int(seq)

    def request(self, method: str, path: str, *, group: str, params: dict | None = None,
                json: Any = None, account: int | None = None, needs_account: bool = False,
                idempotent: bool | None = None) -> Any:
        """API 호출 후 envelope 의 `result` 를 돌려준다(204 면 None). 실패 시 TossAPIError 하위 예외.

        idempotent: 5xx·네트워크 오류·409 request-in-progress 에서 같은 요청을 재시도해도 안전한지.
        기본은 GET 만 True. 쓰기는 clientOrderId(멱등키)를 넣은 주문 생성에서만 True 로 호출한다."""
        headers = {}
        if needs_account:
            headers["X-Tossinvest-Account"] = str(self._resolve_account(account))
        params = _clean_params(params)
        if idempotent is None:
            idempotent = method.upper() == "GET"  # 쓰기 요청은 기본적으로 자동 재시도하지 않음

        token = self.tokens.get()
        token_retried = False
        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire(group)
            started = time.monotonic()
            try:
                r = self.http.request(method, path, params=params, json=json,
                                      headers={**headers, "Authorization": f"Bearer {token}"})
            except httpx.TransportError as e:
                if idempotent and attempt <= MAX_RETRIES:
                    self._backoff(attempt)
                    continue
                self.audit.write(method=method, path=path, group=group, error=type(e).__name__)
                raise
            self.limiter.observe(group, r.headers.get("x-ratelimit-remaining"))
            rid = r.headers.get("x-request-id")
            try:
                body = r.json()
            except ValueError:
                body = None

            if r.is_success and (body is None or (isinstance(body, dict) and "error" not in body)):
                self.audit.write(method=method, path=path, group=group, status=r.status_code,
                                 requestId=rid, ms=int((time.monotonic() - started) * 1000))
                return body.get("result") if isinstance(body, dict) else None

            err = from_response(r.status_code, body, rid)
            if r.status_code == 401 and err.code in TOKEN_RETRY_CODES and not token_retried:
                token_retried = True
                token = self.tokens.recover(token)
                continue
            if r.status_code == 429 and attempt <= MAX_RETRIES:
                self._sleep(self._retry_after(r, attempt))
                continue
            if r.status_code == 409 and err.code == "request-in-progress" and idempotent and attempt <= MAX_RETRIES:
                self._backoff(attempt)  # 같은 clientOrderId 의 첫 요청이 아직 처리 중 → 잠시 후 같은 요청 재전송
                continue
            if r.status_code >= 500 and idempotent and attempt <= MAX_RETRIES:
                self._backoff(attempt)
                continue
            self.audit.write(method=method, path=path, group=group, status=r.status_code,
                             requestId=err.request_id, code=err.code)
            raise err

    @staticmethod
    def _retry_after(r: httpx.Response, attempt: int) -> float:
        try:
            return max(float(r.headers.get("retry-after", "")), 0.1)
        except ValueError:
            return min(2 ** (attempt - 1), 8) + random.uniform(0, 0.3)

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(2 ** (attempt - 1), 8) + random.uniform(0, 0.3))


__all__ = ["TossClient", "TossAPIError"]

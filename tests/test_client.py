import json
import threading

import pytest

from toss_utils import (AuthError, ConfigError, Credentials, InvalidRequestError, RateLimitError, ServerError,
                        TossClient)
from toss_utils.cli import main as cli_main
from toss_utils.ratelimit import RateLimiter

from .fake_server import FakeToss

CREDS = Credentials("tsck_live_test", "secret")


@pytest.fixture
def fake():
    return FakeToss()


def make(fake, tmp_path, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("limiter", RateLimiter(sleep=lambda s: None))
    return TossClient(kw.pop("creds", CREDS), transport=fake.transport(), home=tmp_path, **kw)


# ---------------------------------------------------------------- 인증 / 토큰 캐시
def test_token_cached_and_shared_across_clients(fake, tmp_path):
    fake.route("/api/v1/prices", [{"symbol": "005930", "lastPrice": "70000", "currency": "KRW"}])
    a, b = make(fake, tmp_path), make(fake, tmp_path)
    a.market.prices("005930")
    b.market.prices(["005930"])
    assert fake.issued == ["tok-1"]          # 두 클라이언트(=두 프로세스 가정)가 토큰 1개를 공유
    cache = json.loads((tmp_path / "token.json").read_text())
    assert cache["token"] == "tok-1" and oct((tmp_path / "token.json").stat().st_mode)[-3:] == "600"


def test_concurrent_first_issue_happens_once(fake, tmp_path):
    clients = [make(fake, tmp_path) for _ in range(8)]
    ts = [threading.Thread(target=c.tokens.get) for c in clients]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(fake.issued) == 1             # 파일 락 덕분에 동시 발급 없음


def test_revoked_token_reuses_newer_cache_without_reissue(fake, tmp_path):
    fake.route("/api/v1/prices", [])
    a, b = make(fake, tmp_path), make(fake, tmp_path)
    a.tokens.get()                           # tok-1
    b.tokens.clear(); b.tokens.get()         # 다른 프로세스가 재발급 → tok-1 폐기, 캐시=tok-2
    # a 는 메모리상 tok-1 을 들고 요청 → 401 token-revoked → 캐시의 tok-2 를 재사용 (재발급 X)
    a_token = "tok-1"
    assert a.tokens.recover(a_token) == "tok-2"
    assert fake.issued == ["tok-1", "tok-2"]


def test_request_recovers_from_revoked_token(fake, tmp_path):
    fake.route("/api/v1/prices", [])
    c = make(fake, tmp_path)
    c.tokens.get()
    fake.issued.append("tok-external")       # 서버 측에서 외부 발급으로 tok-1 폐기, 캐시는 여전히 tok-1
    assert c.market.prices("AAPL") == []
    assert fake.valid_token == fake.issued[-1]
    assert fake.last("/api/v1/prices").headers["authorization"] == f"Bearer {fake.valid_token}"


def test_invalid_client_error(fake, tmp_path):
    c = make(fake, tmp_path, creds=Credentials("tsck_live_test", "wrong"))
    with pytest.raises(AuthError) as ei:
        c.tokens.get()
    assert ei.value.code == "invalid_client" and "client_secret" in ei.value.message


# ---------------------------------------------------------------- 공통 HTTP 동작
def test_account_header_and_default_account(fake, tmp_path):
    fake.route("/api/v1/holdings", {"items": []})
    c = make(fake, tmp_path)
    with pytest.raises(ConfigError):
        c.account.holdings()                 # 기본 계좌 없으면 서버 호출 전에 로컬에서 막음
    c2 = make(fake, tmp_path, account_seq=3)
    assert c2.account.holdings() == {"items": []}
    assert fake.last("/api/v1/holdings").headers["x-tossinvest-account"] == "3"


def test_error_envelope_mapping(fake, tmp_path):
    fake.queue("/api/v1/prices", 400, {"error": {"requestId": "r-9", "code": "invalid-request",
                                                 "message": "bad", "data": {"field": "symbols"}}})
    c = make(fake, tmp_path)
    with pytest.raises(InvalidRequestError) as ei:
        c.market.prices("005930")
    assert ei.value.request_id == "r-9" and ei.value.data == {"field": "symbols"}
    assert json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])["code"] == "invalid-request"


def test_429_retries_with_retry_after(fake, tmp_path):
    slept = []
    for _ in range(2):
        fake.queue("/api/v1/prices", 429, {"error": {"code": "rate-limit-exceeded", "message": "x"}},
                   {"retry-after": "0.5"})
    fake.route("/api/v1/prices", [{"symbol": "AAPL"}])
    c = make(fake, tmp_path, sleep=slept.append)
    assert c.market.prices("AAPL") == [{"symbol": "AAPL"}]
    assert slept == [0.5, 0.5]


def test_429_gives_up_after_max_retries(fake, tmp_path):
    for _ in range(10):
        fake.queue("/api/v1/prices", 429, {"error": {"code": "rate-limit-exceeded", "message": "x"}})
    with pytest.raises(RateLimitError):
        make(fake, tmp_path).market.prices("AAPL")


def test_5xx_retried_for_get(fake, tmp_path):
    fake.queue("/api/v1/prices", 500, {"error": {"code": "internal-error", "message": "x"}})
    fake.route("/api/v1/prices", [])
    assert make(fake, tmp_path).market.prices("AAPL") == []
    fake.queue("/api/v1/prices", 500, {"error": {"code": "maintenance", "message": "점검"}})
    fake.queue("/api/v1/prices", 500, {"error": {"code": "maintenance", "message": "점검"}})
    fake.queue("/api/v1/prices", 500, {"error": {"code": "maintenance", "message": "점검"}})
    fake.queue("/api/v1/prices", 500, {"error": {"code": "maintenance", "message": "점검"}})
    with pytest.raises(ServerError):
        make(fake, tmp_path).market.prices("AAPL")


def test_param_cleaning_and_symbol_validation(fake, tmp_path):
    fake.route("/api/v1/candles", {"candles": [], "nextBefore": None})
    c = make(fake, tmp_path)
    c.market.candles("005930", "1d", 5, before="2026-03-25T09:00:00+09:00", adjusted=False)
    q = fake.last("/api/v1/candles").url.params
    assert q["adjusted"] == "false" and q["before"] == "2026-03-25T09:00:00+09:00" and q["count"] == "5"
    with pytest.raises(ValueError):
        c.market.prices([])
    with pytest.raises(ValueError):
        c.market.prices(["X"] * 201)


def test_candle_pagination_follows_next_before(fake, tmp_path):
    pages = {None: (["c1", "c2"], "b1"), "b1": (["c3"], None)}

    def candles(req):
        cs, nb = pages[req.url.params.get("before")]
        return {"candles": [{"timestamp": x} for x in cs], "nextBefore": nb}

    fake.route("/api/v1/candles", candles)
    got = [c["timestamp"] for c in make(fake, tmp_path).market.iter_candles("005930", limit=10)]
    assert got == ["c1", "c2", "c3"]


def test_rate_limiter_blocks_beyond_group_limit():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s); now[0] += s

    rl = RateLimiter({"ACCOUNT": 1}, clock=lambda: now[0], sleep=sleep)
    rl.acquire("ACCOUNT"); rl.acquire("ACCOUNT")
    assert slept and abs(sum(slept) - 1.0) < 1e-6   # ACCOUNT 는 초당 1회 → 두 번째는 1초 대기


# ---------------------------------------------------------------- CLI
def test_cli_json_output_and_exit_codes(fake, tmp_path, capsys):
    fake.route("/api/v1/accounts", [{"accountNo": "123-45", "accountSeq": 7, "accountType": "BROKERAGE"}])
    factory = lambda: make(fake, tmp_path)
    assert cli_main(["--compact", "account", "list"], client_factory=factory) == 0
    assert json.loads(capsys.readouterr().out)[0]["accountSeq"] == 7

    assert cli_main(["account", "use", "7"], client_factory=factory) == 0
    assert json.loads((tmp_path / "config.json").read_text())["account_seq"] == 7
    capsys.readouterr()

    assert cli_main(["account", "use", "99"], client_factory=factory) == 1   # 남의 계좌 → 설정 오류
    assert "99" in capsys.readouterr().err

    fake.queue("/api/v1/prices", 404, {"error": {"code": "stock-not-found", "message": "없음"}})
    assert cli_main(["quote", "price", "ZZZZ"], client_factory=factory) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "stock-not-found"

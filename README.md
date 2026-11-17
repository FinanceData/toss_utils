# toss_utils: 토스증권 Open API 클라이언트 + CLI

토스증권 Open API를 **Python 라이브러리**와 **`toss` CLI**로 감싼 도구입니다. 사람이 터미널에서 써도 되고, **에이전트(LLM)가 도구로 호출**하기 쉽게 만들었습니다.

| 기능 | 상태 |
|---|---|
| 인증 (프로세스 간 토큰 공유) | ✅ |
| 시세·종목·시장 정보 (26개 API) | ✅ 읽기 |
| 계좌·자산·거래내역·주문 조회 | ✅ 읽기 |
| 주문·정정·취소, 조건주문(SINGLE/OCO/OTO) 등록·취소 | ✅ **안전장치 + 미리보기 → 확인 → 전송** |
| 에이전트용 MCP 서버 (`toss-mcp`) | ✅ 조회·추적·미리보기 26개 도구, **전송 도구 없음** |
| 실시간 기록과 동시 분석: `toss record` · `toss live` · `toss archive` | ✅ 원본 NDJSON.gz + SQLite(WAL) + Parquet |
| 실시간 (WebSocket): `toss stream`, `RealtimeStream` | ✅ 체결·호가·내 주문·조건주문 이벤트, 자동 재연결·재선언·PING·재동기화 |
| WebSocket 공유 데몬, 조건주문 수정 | ⏳ 아직 없음 |

> ⚠️ **토스증권 Open API에는 모의투자 환경이 없습니다.** 전송한 주문은 실제 계좌에서 바로 체결됩니다. 이 도구는 기본적으로 **미리보기만** 하고, 사람이 확인해야 전송합니다.

배경 자료 (프로젝트 밖 `../toss_lect/` 폴더): [강의 노트](../toss_lect/lecture-notes.md) · [카테고리별 가이드](../toss_lect/toss-openapi-category-guide.md) · [조사 요약](../toss_lect/toss-openapi-research.md) · `spec/` (공식 OpenAPI/AsyncAPI 스펙)

---

## 1. 설치와 초기 설정

### 1-1. 키 발급과 IP 등록 (토스증권 WTS 웹, 한 번만)
1. PC 웹 WTS에 로그인하고 **설정 > Open API**로 들어갑니다.
2. **Client Id**는 옆의 `복사` 버튼으로 복사합니다. **Client Secret은 발급(또는 재발급) 직후에만 전체 값이 보이니** 그때 바로 복사해 두세요. 나중에는 `****`로 가려집니다.
3. 같은 화면의 **허용 IP 관리 > IP 추가**에 이 컴퓨터의 공인 IP를 등록합니다. 공인 IP는 아래 명령으로 확인합니다.

```bash
curl -s https://api.ipify.org
```

4. 국내 주식을 주문하려면 앱의 투자자 거래소 설정이 **통합(SOR)**이어야 합니다.
5. 키 유효기간은 **1년**입니다. 만료일은 같은 화면에서 확인할 수 있습니다.

### 1-2. 패키지 설치

```bash
uv sync
```

```bash
cp .env.example .env
```

`.env`를 열어 두 값을 넣습니다. 이 파일은 `.gitignore`에 들어 있습니다.

```
TOSS_CLIENT_ID=tsck_live_...
TOSS_CLIENT_SECRET=tssk_live_...
```

### 1-3. 어디서나 `toss` 명령으로 쓰기 (전역 설치)

```bash
uv tool install --editable .
```

```bash
ln -s "$PWD/.env" ~/.config/toss/.env
```

- `~/.local/bin/toss`가 생깁니다. editable 설치라서 소스를 고치면 다시 설치하지 않아도 바로 반영됩니다. 의존성을 바꿨을 때만 `uv tool install --editable . --force`를 실행하세요.
- 자격증명은 `--env-file` → `./.env` → `~/.config/toss/.env` 순서로 찾습니다.
- 전역 설치 없이 이 폴더 안에서만 쓸 거라면 명령 앞에 `uv run`을 붙이면 됩니다 (예: `uv run toss ...`).
- 제거: `uv tool uninstall toss-utils` (uv는 도구 이름을 `toss-utils`로 표기합니다)

### 1-4. 연결 확인과 기본 계좌 지정

```bash
toss quote price 005930 AAPL
```

```bash
toss account list
```

```bash
toss account use 1
```

```bash
toss policy show
```

`account use` 뒤의 `1` 자리에는 `account list`가 보여준 `accountSeq` 값을 넣으세요. 이 값은 계좌번호가 아니라 작은 정수입니다.

---

## 2. 사용법

### 2-1. 공통 규칙
- **출력**: 성공하면 stdout에 JSON을 출력합니다. `--compact`를 붙이면 한 줄로 나옵니다 (에이전트나 `jq`에 넘길 때 편합니다).
- **오류**: stderr에 `{"error": {...}}`를 출력하고 아래 종료 코드로 끝납니다.

| 종료 코드 | 의미 | 예 |
|---|---|---|
| 0 | 성공 | |
| 1 | 설정 오류 | 키 없음, 기본 계좌 없음 |
| 2 | 요청·인증 오류 (4xx), 잘못된 인자 | `stock-not-found`, `invalid-request`, `ip-not-allowed` |
| 3 | **안전장치 거부** (서버로 아무것도 보내지 않음) | 한도 초과, 시장가 금지, 킬 스위치 |
| 4 | 호출 한도 초과 (429, 재시도 후에도 실패) | |
| 5 | 서버나 네트워크 오류 | `internal-error`, `maintenance` |

- 숫자(가격, 수량, 금액)는 **문자열**로 옵니다. 계산할 때는 `Decimal`로 바꾸세요.
- 계좌가 필요한 명령은 기본 계좌를 씁니다. 다른 계좌를 쓰려면 `--account N`을 붙이세요.

### 2-2. 시세·종목·시장 정보 (읽기)

| 명령 | 설명 |
|---|---|
| `toss quote price 005930 AAPL` | 현재가 (최대 200개, 국내·미국 섞어도 됨) |
| `toss quote book 005930` · `trades` · `limits` | 호가 · 최근 체결(최대 50건) · 상하한가 |
| `toss candles 005930 -i 1d -n 300` | 캔들 (최신순). `-n`이 200을 넘으면 자동으로 여러 페이지를 받음. `-i 1m`은 분봉, `--raw-price`는 수정주가 미적용 |
| `toss stock info 005930,AAPL` | 종목 기본 정보 (시장, 유형, 상장 상태, 거래정지 여부) |
| `toss stock warnings 005930` | 매수 유의사항 (VI, 투자경고, 단기과열, 정리매매 등) |
| `toss stock list KOSPI` | 시장 전체 종목 목록 (초당 1회 한도, 하루 1번 캐시 권장) |
| `toss stock trend investor-trading 005930 --count 5` | 국내 수급. KIND: `investor-trading` `program-trades` `short-selling` `credit-trades` `securities-lending` |
| `toss market calendar KR` · `US` | 전일·당일·익일 장 운영 시간 (KST) |
| `toss market fx` | 원·달러 참고 환율 (1분 갱신) |
| `toss market rankings MARKET_TRADING_AMOUNT --country KR --duration 1d --count 20` | 랭킹. TYPE: `MARKET_TRADING_AMOUNT` `MARKET_TRADING_VOLUME` `TOP_GAINERS` `TOP_LOSERS` `TOSS_SECURITIES_TRADING_*` |
| `toss market investor-rankings` | 외국인·기관·개인 순매수·순매도 상위 |
| `toss market indices` | 국내 지수 (KOSPI, KOSDAQ) |
| `toss market sectors` · `sector ID` · `sector-stocks ID` · `sector-rankings` | 섹터 |

### 2-3. 계좌·자산 (읽기)

| 명령 | 설명 |
|---|---|
| `toss account list` · `toss account use N` | 계좌 목록 · 기본 계좌 저장 |
| `toss holdings [--symbol S]` | 보유 주식. 요약 금액은 `{krw, usd}` 통화별, 종목별 금액은 해당 통화 |
| `toss cash` · `toss cash --currency USD` | 현금 기준 매수 가능 금액. **미국 주문은 자동환전이 없어** 달러 잔고만큼만 살 수 있음 |
| `toss sellable SYMBOL` | 매도 가능 수량 |
| `toss commissions` | 시장별 수수료율 (`0.00015` = 0.015%) |
| `toss tx --from 2026-11-01 --to 2026-11-30 --type BUY,SELL [--all]` | 거래내역 (**결제일 기준**) |
| `toss order list` · `--closed [--all]` · `toss order show ID` | 주문 조회 (진행 중 / 종료) |
| `toss order track ID` | **주문 추적**: 어떤 ID를 넣어도 정정·취소 이후의 최신 ID와 목록으로 교차 확인한 실제 상태를 돌려줌 |
| `toss cond list` · `--closed` · `toss cond show ID` | 조건주문 조회 |

### 2-4. 주문: 미리보기 → 확인 → 전송

**모든 주문 명령은 기본이 미리보기**입니다. 검사 결과와 `confirm_id`만 출력하고, **서버에는 아무것도 보내지 않습니다.**

```bash
toss order buy 034220 1 --limit 5230
```

결과의 `checks`를 확인하고, 모두 통과(`"ok": true`)했으면 `confirm_id`로 전송합니다.

```bash
toss order submit <confirm_id>
```

- `confirm_id`는 **120초 동안 유효하고 한 번만** 쓸 수 있습니다. 시간이 지나면 미리보기를 다시 하세요.
- 미리보기 명령에 `--yes`를 붙이면 미리보기와 전송을 한 번에 합니다. 사람이 직접 실행할 때만 쓰세요.
- 대기 중인 확인 목록은 `toss order pending`, 필요 없는 확인은 `toss order discard <confirm_id>`로 지웁니다.

| 주문 형태 | 명령 |
|---|---|
| 지정가 매수·매도 | `toss order buy 005930 1 --limit 190000` / `toss order sell ...` |
| 시장가 (정책에서 허용해야 함) | `toss order buy 005930 1 --market` |
| 미국 소수점 매수 (금액 주문, 시장가) | `toss order buy AAPL --market --amount 10` |
| 미국 장마감 지정가 (LOC) | `toss order buy AAPL 1 --limit 350 --tif CLS` |
| 국내 시가단일가 | `toss order buy 005930 1 --limit 190000 --tif OPG` |
| 정정 (국내) | `toss order modify <orderId> --limit 5200 [--qty 1]` → `toss order submit <confirm_id>` |
| 정정 (미국, 가격만) | `toss order modify <orderId> --limit 349.5` |
| 취소 (확인 없이 바로 실행) | `toss order cancel <orderId>` |
| 전체 취소 | `toss order cancel-all [--symbol 005930]` (대상만 보여줌) → 같은 명령에 `--yes`를 붙이면 실행 |

> 정정이나 취소를 하면 **새 orderId**가 발급됩니다. 이후 조회는 응답으로 받은 새 ID로 하세요.
>
> **실측 (2026-11-17, 국내):** 정정이나 취소 **전의 ID로 `order show`를 하면 예전 상태(PENDING)가 그대로 나옵니다.** 갱신되지 않습니다.
> - 그래서 상태 확인은 **`toss order track <아무 ID>`** 로 하세요. 이 도구로 보낸 생성·정정·취소의 ID 연결을 `~/.config/toss/lineage.jsonl`에 기록해 두고, 최신 ID로 조회한 뒤 진행 중·종료 목록과 교차 확인합니다.
> - 앱이나 WTS에서 정정·취소한 주문은 연결 기록이 없습니다. 이 경우 `track`은 `status: "UNKNOWN"`과 경고, 그리고 같은 종목의 최근 종료 주문을 돌려줍니다.
> - `order modify`와 `order cancel`에 예전 ID를 넣어도 자동으로 최신 ID를 대상으로 합니다. 이미 취소된 주문은 다시 취소하지 않습니다.
> - 예: 접수 ID(5,230) → 정정 ID(5,200) → 취소 ID 순서로 받았을 때, CLOSED 목록에는 정정 ID로 `CANCELED`가 나옵니다. 취소 ID로 `show`하면 `CANCELED`가 나오지만, 접수 ID와 정정 ID로 `show`하면 여전히 `PENDING`이 나옵니다.

### 2-5. 조건주문 (SINGLE / OCO / OTO)

조건주문은 등록해 두면 **조건이 충족될 때 서버가 자동으로 실제 주문을 냅니다.** 국내는 KRX 정규장에서만 실행되고, 미국은 데이마켓을 포함한 모든 세션에서 실행됩니다. 등록도 미리보기 → `toss order submit`(또는 `--yes`) 순서입니다.

```bash
toss cond single 034220 1 --side BUY --trigger 5300 --price 5300 --expire 2026-12-31
```

```bash
toss cond oco 108860 1 --tp-trigger 8000 --tp-price 8000 --sl-trigger 6500 --sl-price 6490
```

```bash
toss cond oto 034220 1 --buy-trigger 5300 --buy-price 5300 --sell-trigger 6500 --sell-price 6500
```

```bash
toss cond cancel <conditionalOrderId> --with-order
```

- **OCO**(익절+손절): 두 조건 모두 매도이고, `익절 감시가 > 현재가 > 손절 감시가`여야 합니다. 지정가만 가능하고 종목당 1개입니다.
- **OTO**(매수 후 매도): 첫 조건은 매수, 두 번째는 매도입니다. 지정가만 가능하고 종목당 1개입니다.
- `--expire`를 생략하면 30일 뒤에 만료됩니다 (최대 1년).
- 조건주문을 취소해도 **이미 접수된 주문은 그대로 남습니다.** `--with-order`를 붙이면 그 주문도 함께 취소합니다.

### 2-6. 안전장치와 정책

정책 파일은 `~/.config/toss/policy.json`입니다. `toss policy show`로 보고, `toss policy set KEY VALUE`로 바꿉니다.

| 키 | 기본값 | 의미 |
|---|---|---|
| `max_order_krw` | 100000 | 주문 1건 최대 추정 금액 (원화 환산) |
| `max_daily_krw` | 300000 | 하루(KST) 누적 최대 금액. **이 도구로 보낸 주문만** 셈 (앱 주문은 제외) |
| `allow_market_orders` | false | 시장가 주문 허용 |
| `market_slippage` | 0.03 | 시장가 매수 금액을 추정할 때 더하는 여유분 |
| `allowed_symbols` | null | `null`이면 제한 없음. 예: `toss policy set allowed_symbols 005930,AAPL` |
| `denied_symbols` | [] | 거래를 막을 종목 |
| `allow_warning_stocks` | false | 매수 유의 종목(VI, 투자경고 등)을 살 수 있게 할지 |
| `confirm_ttl_sec` | 120 | 미리보기 확인 ID의 유효 시간 |
| `agent_can_cancel` | false | MCP 도구로 에이전트가 취소할 수 있게 할지 |

미리보기에서 하는 검사는 다음과 같습니다.
- 킬 스위치, 허용·차단 종목, 거래정지·정리매매 여부, 매수 유의사항
- 시장가 허용 여부, 1건 한도, 하루 한도
- 국내 상하한가 범위, 미국 가격 자릿수
- 매수 가능 금액, 매도 가능 수량
- 국내 같은 종목 반대 방향 미체결 주문
- OCO/OTO 규칙, 종목당 OCO/OTO 1개 제한

**전송 직전에는 킬 스위치와 한도를 한 번 더 검사합니다.**

긴급 정지:

```bash
toss policy kill
```

이 명령을 실행하면 새 주문, 정정, 조건주문 등록이 모두 막힙니다. 취소는 계속 할 수 있습니다. 해제는 `toss policy unkill`입니다.

### 2-7. 실시간 (WebSocket)
`websockets` 패키지가 필요합니다. `.[mcp]`로 설치했다면 이미 들어 있고, CLI만 쓴다면 아래처럼 설치합니다.

```bash
uv tool install --editable ".[ws]" --force
```

```bash
toss stream trade:kr:005930 orderbook:kr:005930 order --seconds 30
```

| 토픽 | 의미 |
|---|---|
| `trade:kr:005930` · `trade:us:AAPL` | 실시간 체결 (체결가, 수량, 시각) |
| `orderbook:kr:005930` · `orderbook:us:AAPL` | 실시간 호가 (매번 전체 스냅샷) |
| `order` | 내 주문 이벤트 (접수, 체결, 정정, 취소, 거부). 기본 계좌 사용 |
| `cond` | 내 조건주문 이벤트 (감시, 발동, 완료, 만료, 취소, 수정) |

- 출력은 **NDJSON**(한 줄에 JSON 하나)입니다. `type`이 `status`(연결 상태), `subscriptions`(구독 결과), `resync`(주문 채널 연결 직후 REST 스냅샷), `message`(실시간 데이터), `error` 중 하나입니다.
- 종료: `--seconds N`, `--count N`(데이터 N건), 또는 Ctrl-C. `--log FILE`로 이벤트를 파일에 함께 기록합니다.
- 자동 처리: 60초마다 `PING`, 끊기면 지수 백오프로 재연결하고 구독을 다시 선언, `auth-revoked`면 새 토큰으로 재연결, 거부된 토픽은 다음 선언에서 제외
- ⚠️ **계정당 동시 연결은 2개**입니다. `toss stream`을 여러 개 띄우거나 MCP `sample_realtime`과 함께 쓰면 가장 오래된 연결이 끊깁니다 (자동 재연결이 서로를 밀어낼 수 있음).
- 시세는 유실될 수 있고(LOSSY), 주문 이벤트는 연결 안에서만 유실이 없습니다. 끊겼다 다시 붙으면 `resync` 스냅샷으로 상태를 맞추세요.
- 주문 이벤트에는 정정 전 ID와 연결하는 필드가 없어서, 주문 ID 추적(`order track`)의 연결 기록을 실시간 이벤트로 채울 수는 없습니다.

라이브러리:

```python
import asyncio
from toss_utils.stream import RealtimeStream, collect

async def main():
    agen = RealtimeStream(c, ["trade:kr:005930", "order"]).events()
    try:
        async for ev in agen:
            print(ev)
    finally:
        await agen.aclose()

asyncio.run(main())
snapshot = collect(c, ["trade:kr:005930"], seconds=5)     # 동기 코드에서 5초간 모으기
```

### 2-8. 실시간 기록과 동시 분석 (`toss record` · `toss live` · `toss archive`)
**기록 프로세스 하나**가 WebSocket 연결 1개로 받아서 저장하고, 분석은 다른 프로세스에서 같은 DB를 **동시에** 읽습니다. 분석 프로그램을 여러 개 띄워도 WebSocket 연결은 1개로 유지됩니다.

```text
toss record ─▶ ① 원본  ~/.local/share/toss/raw/date=YYYY-MM-DD/HH.ndjson.gz  (받은 그대로 + recv_ns)
            ─▶ ② 실시간 DB ~/.local/share/toss/live.db  (SQLite WAL, 0.25초마다 묶어서 기록)
                  trades · bars_1s · bars_1m(OHLCV·거래대금·VWAP) · book_top(최우선 호가) · gaps · order_events · orders_latest
toss live   ─▶ ② 를 읽기 전용으로 조회 (키·연결 불필요)
toss archive ─▶ ③ 하루치를 ~/.local/share/toss/parquet/<표>/date=YYYY-MM-DD/ 로 (DuckDB 로 조회)
```

설치 (Parquet 보관에 DuckDB 필요):

```bash
uv tool install --editable ".[mcp,analytics]" --force
```

기록 시작 (터미널 하나에 띄워 둡니다. Ctrl-C로 종료):

```bash
toss record trade:kr:005930 orderbook:kr:005930 trade:kr:000660 orderbook:kr:000660 order
```

다른 터미널이나 노트북에서 동시에 조회:

```bash
toss live status
```

```bash
toss live bars 005930 --interval 1m --minutes 30
```

```bash
toss live book 005930
```

장 마감 후 Parquet으로 보관하고, 7일보다 오래된 실시간 DB 행 정리:

```bash
toss archive --prune-days 7
```

| 표 (SQLite) | 내용 |
|---|---|
| `trades` | 체결 전부 (`ts_ns` 서버 시각, `recv_ns` 받은 시각, `price_e4`, `volume_e6`) |
| `bars_1s`, `bars_1m` | 봉: 시가·고가·저가·종가, 거래량, 체결 수, 거래대금(`amt_e4`). VWAP = 거래대금 ÷ 거래량 |
| `book_top` | 최우선 호가가 바뀔 때(또는 `--book-interval`초마다) 1행. 상위 5단계 JSON, 스프레드, 상태 |
| `gaps` | 재연결 구간 = 데이터 공백 (시세에는 sequence가 없어 공백은 이것으로만 알 수 있음) |
| `order_events`, `orders_latest`, `cond_latest` | 내 주문·조건주문 이벤트와 최신 상태. 재연결 후 스냅샷에 없는 진행 중 주문은 `CHECK_REST`로 표시 |

- **가격과 수량은 정수로 스케일**합니다: `price_e4` = 가격×10⁴, `volume_e6` = 수량×10⁶. SQLite에는 decimal이 없어서 float 대신 이렇게 저장합니다. `toss live`와 Parquet에서는 정확한 decimal로 바꿔 보여줍니다.
- **호가 상태** `crossed`: 0 정상, 1 매도1 = 매수1(locked), 2 매도1 < 매수1(crossed). 국내는 KRX+NXT 통합 호가라서 1과 2가 흔합니다 (실측: 삼성전자 프레임의 약 40%). 스프레드가 0이나 음수일 수 있으니 분석할 때 걸러내세요.
- Parquet의 시각 열(`ts`)은 **UTC 기준 시각**(시간대 정보 없음)입니다. 한국 시간으로 보려면 `(ts AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Seoul'`로 변환하세요.
- 실측 (2026-11-17, 2종목 체결과 호가, 60초): 실시간 데이터 4,561건 → 체결 1,269행, 호가 프레임 3,292개 중 121행만 저장. 원본 NDJSON.gz는 약 200KB, DB는 약 250KB.
- ⚠️ 받은 데이터는 본인 매매 목적으로만 쓸 수 있습니다. 데이터 폴더를 공유 폴더나 공개 저장소에 두지 마세요.

DuckDB로 Parquet 조회 예:

```sql
SELECT symbol, (ts AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Seoul' AS t, close, volume, vwap
FROM '~/.local/share/toss/parquet/bars_1m/*/*.parquet' ORDER BY symbol, t;
```

### 2-9. 토큰 관리

| 명령 | 설명 |
|---|---|
| `toss auth status` | 토큰 남은 시간, 현재 공인 IP, 기본 계좌 |
| `toss auth refresh` | 강제 재발급. **다른 프로세스가 쓰던 토큰은 무효가 됩니다** |
| `toss auth logout` | 토큰 캐시 삭제 (WTS에서 키를 재발급했을 때 실행) |

---

## 3. 라이브러리로 사용

```python
from toss_utils import TossClient, SafetyError, TossAPIError, load_dotenv

load_dotenv()                                     # ./.env
with TossClient.from_env() as c:                  # 기본 계좌는 `toss account use` 로 저장한 값
    print(c.market.prices(["005930", "AAPL"]))
    print(c.account.holdings())
    candles = list(c.market.iter_candles("005930", "1d", limit=500))

    # 주문: 미리보기 → (사람 확인) → 전송
    pv = c.trader.prepare_order("034220", "BUY", "LIMIT", quantity="1", price="5230")
    if pv["ok"]:
        print(pv["estimated"], [x for x in pv["checks"]])
        # res = c.trader.submit(pv["confirm_id"])   # 사람이 승인한 뒤에만
```

| 모듈 | 내용 |
|---|---|
| `c.market` | 시세·종목·시장·랭킹·지수·섹터 (`prices`, `candles`, `iter_candles`, `stocks`, `warnings`, `trend`, `calendar`, `rankings`, …) |
| `c.account` | `accounts`, `holdings`, `transactions`, `iter_transactions`, `buying_power`, `sellable_quantity`, `commissions` |
| `c.orders` | 조회 전용: `list`, `iter_closed`, `get`, `conditional_list`, `conditional_get` |
| `toss_utils.tracking.track(c, id)` | 최신 ID와 교차 확인한 상태 (`latestOrderId`, `status`, `chain`, `source`, `warnings`) |
| `c.trader` | 안전장치를 거치는 쓰기: `prepare_order`, `prepare_modify`, `prepare_conditional`, `submit`, `pending`, `discard`, `cancel`, `cancel_all`, `cancel_conditional` |
| `c._trading` | 안전장치 **없는** 원시 쓰기 API입니다. 직접 쓰지 마세요. |

예외 종류:
- `SafetyError`: 로컬 안전장치가 거부했습니다. `.checks`와 `.preview`에 이유가 있습니다.
- `TossAPIError` 계열: `AuthError`, `ForbiddenError`, `NotFoundError`, `ConflictError`, `UnprocessableError`, `RateLimitError`, `ServerError`. `.code`, `.message`, `.data`, `.request_id`에 서버가 알려준 내용이 있습니다.
- `ConfigError`: 키나 계좌 설정이 없습니다.

---

## 4. 에이전트에서 활용하기

### 4-1. 원칙
1. **읽기는 자유롭게**: 시세, 종목, 계좌, 주문 조회는 에이전트가 승인 없이 써도 됩니다.
2. **쓰기는 미리보기까지만**: 에이전트는 `toss order buy ...`(미리보기)나 `prepare_*`까지만 실행하고, 결과(`checks`, `estimated`, `body`)를 사람에게 보여줍니다.
3. **전송은 사람이**: `toss order submit <confirm_id>`와 `--yes`는 사람이 직접 실행하거나 명시적으로 승인합니다.
4. **한도는 코드가 지킵니다**: 프롬프트 인젝션을 당해도 정책 한도, 허용 종목, 킬 스위치는 라이브러리 안에서 강제되므로 우회할 수 없습니다. 에이전트에게는 `toss policy set`이나 `unkill`을 맡기지 마세요.
5. 미국 주식은 데이마켓 때문에 **거의 24시간 체결**될 수 있습니다. 무인으로 운영한다면 `allowed_symbols`와 낮은 한도를 먼저 설정하세요.

### 4-2. 셸 도구가 있는 에이전트 (예: Claude Code)
CLI가 JSON과 종료 코드를 돌려주므로 셸에서 바로 쓸 수 있습니다. 프로젝트의 `.claude/settings.json`에 **읽기 명령만 자동 허용**하고 위험한 명령은 막아 두세요. 목록에 없는 명령(주문 미리보기 등)은 실행할 때마다 승인을 묻습니다.

```json
{
  "permissions": {
    "allow": [
      "Bash(toss quote:*)", "Bash(toss candles:*)", "Bash(toss stock:*)", "Bash(toss market:*)",
      "Bash(toss holdings:*)", "Bash(toss cash:*)", "Bash(toss sellable:*)", "Bash(toss commissions:*)",
      "Bash(toss tx:*)", "Bash(toss account list:*)", "Bash(toss order list:*)", "Bash(toss order show:*)", "Bash(toss order track:*)",
      "Bash(toss order pending:*)", "Bash(toss cond list:*)", "Bash(toss cond show:*)",
      "Bash(toss policy show:*)", "Bash(toss auth status:*)",
      "Bash(toss --compact quote:*)", "Bash(toss --compact holdings:*)", "Bash(toss --compact order list:*)"
    ],
    "deny": [
      "Bash(toss order submit:*)", "Bash(toss policy set:*)", "Bash(toss policy unkill:*)",
      "Bash(toss auth refresh:*)", "Bash(toss auth logout:*)"
    ]
  }
}
```

> ⚠️ 이 권한 규칙은 **명령 앞부분이 같은지만** 비교합니다. 그래서 `toss order buy ... --yes`처럼 중간에 `--yes`가 붙은 명령을 규칙으로 따로 막을 수 없습니다. `order buy`, `order sell`, `order modify`, `cond ...`는 **allow에 넣지 말고**, 실행할 때마다 승인 창에서 전체 명령(특히 `--yes`가 있는지)을 확인하세요.

에이전트에게 줄 지시문 예시입니다 (`CLAUDE.md`나 시스템 프롬프트에 넣으세요).

```markdown
## 토스증권 도구(toss) 사용 규칙
- 조회는 `toss --compact ...` 를 써서 JSON 으로 받는다. 종료 코드 3 은 안전장치 거부이므로 이유(`failed`)를 사용자에게 그대로 전한다.
- 주문은 반드시 미리보기만 실행한다: `toss --compact order buy|sell SYMBOL QTY --limit PRICE`.
  `--yes` 를 붙이거나 `toss order submit` 을 실행하지 않는다.
- 미리보기 결과에서 종목명, 방향, 수량, 가격, 추정 금액(원화), 실패한 검사, confirm_id 를 표로 보여주고
  "전송하려면 `toss order submit <confirm_id>` 를 직접 실행하세요" 라고 안내한다.
- 시장가, 한도 상향, 정책 변경, 킬 스위치 해제는 제안하지 않는다.
- 주문 상태는 `toss --compact order show` 가 아니라 `toss --compact order track <orderId>` 로 확인한다
  (정정·취소 전의 ID 로 show 하면 예전 상태가 나온다). status 가 UNKNOWN 이면 warnings 를 사용자에게 전한다.
```

대화 흐름 예시:

```text
사용자: LG디스플레이 1주 5,230원에 사줘
에이전트: (toss --compact order buy 034220 1 --limit 5230 실행 → 검사 9개 통과, confirm_id=c9d3…)
          | 종목 | LG디스플레이(034220) | 매수 1주 | 지정가 5,230원 | 추정 5,231원 |
          전송하려면 터미널에서 실행하세요: toss order submit c9d3…  (120초 유효)
사용자: (직접 submit 실행) 보냈어
에이전트: (toss --compact order list 실행) 접수 확인: orderId=… PENDING
```

### 4-3. MCP 서버로 붙이기 (권장)
CLI는 그대로 두고, 같은 라이브러리와 같은 `~/.config/toss/` 설정(토큰, 정책, 킬 스위치, 장부, ID 연결 기록)을 공유하는 **MCP 서버**를 함께 제공합니다. 도구 정의는 `src/toss_utils/tools.py` 한 곳에 있습니다.

설치 (MCP 의존성 포함):

```bash
uv tool install --editable ".[mcp]" --force
```

Claude Code에 등록:

```bash
claude mcp add toss-utils -- toss-mcp
```

| 종류 | 도구 | 동작 |
|---|---|---|
| 조회 (20) | `get_live_bars` `get_live_top_of_book` `get_live_status` `sample_realtime` `get_quotes` `get_orderbook` `get_candles` `get_stock_info` `get_stock_warnings` `get_market_hours` `get_exchange_rate` `get_rankings` `get_portfolio` `get_buying_power` `get_sellable_quantity` `list_orders` `track_order` `list_conditional_orders` `get_trading_policy` `list_pending_confirmations` | 읽기 전용 (`readOnlyHint`) |
| 미리보기 (4) | `prepare_order` `prepare_modify` `prepare_conditional` `discard_confirmation` | 검사와 미리보기만 하고 서버에 주문을 보내지 않음. 결과의 `next_step_for_user`에 사람이 실행할 `toss order submit <id>`가 들어 있음 |
| 취소 (2) | `cancel_order` `cancel_conditional_order` | 정책 `agent_can_cancel`이 true일 때만 동작 (기본 false). 켜려면 사람이 `toss policy set agent_can_cancel true` |
| **없음** | 주문 전송, 정책 변경, 킬 스위치 해제, 토큰 재발급 | 에이전트가 할 수 없음. 전송은 사람이 터미널에서 |

- 모든 도구는 예외 대신 `{"ok": false, "error": {"type": "safety"\|"api"\|"config"\|"invalid-argument", ...}}`를 돌려줍니다. 안전장치 거부면 `failed`에 실패한 검사 목록이 들어 있습니다.
- 서버는 연결할 때 사용 규칙(`instructions`)을 에이전트에게 전달합니다: 미리보기만 할 것, 상태는 `track_order`로 볼 것, 한도 상향을 제안하지 말 것 등.
- 키가 없어도 서버는 뜹니다. 그 경우 도구를 호출하면 `config` 오류를 돌려줍니다.
- stdio 방식이라 stdout은 MCP 프로토콜 전용입니다 (테스트로 확인).

사용 흐름:

```text
사용자: LG디스플레이 1주 5,230원 매수 준비해줘
에이전트: prepare_order(symbol="034220", side="BUY", quantity="1", price="5230")
        → 미리보기 표 + "터미널에서 toss order submit 631b… 를 실행하세요 (120초)"
사용자: (터미널) toss order submit 631b…
에이전트: track_order(<orderId>) → PENDING
```

### 4-4. 함수 호출(function calling)로 직접 붙이기
LLM SDK의 도구 정의에 아래처럼 **읽기 도구와 prepare 도구만** 노출하고, 전송은 애플리케이션 코드에서 사람이 승인할 때만 실행합니다.

```python
from toss_utils import TossClient, SafetyError, TossAPIError, load_dotenv
from toss_utils.tracking import track

load_dotenv()
c = TossClient.from_env()

# 에이전트에게 노출할 도구: 이름 → (설명, 실행 함수)
TOOLS = {
    "get_quotes":      ("현재가 조회. symbols: 종목코드/티커 목록", lambda symbols: c.market.prices(symbols)),
    "get_candles":     ("캔들(최신순). interval 1d|1m", lambda symbol, interval="1d", count=60:
                        c.market.candles(symbol, interval, count)),
    "get_stock_info":  ("종목 기본 정보", lambda symbols: c.market.stocks(symbols)),
    "get_warnings":    ("매수 유의사항", lambda symbol: c.market.warnings(symbol)),
    "get_portfolio":   ("보유 주식·평가손익", lambda: c.account.holdings()),
    "get_cash":        ("매수 가능 금액. currency KRW|USD", lambda currency="KRW": c.account.buying_power(currency)),
    "get_open_orders": ("진행 중 주문", lambda: c.orders.list("OPEN")),
    "track_order":     ("주문 최신 상태(정정·취소 후에도 정확)", lambda order_id: track(c, order_id)),
    "prepare_order":   ("주문 미리보기(전송 안 함). side BUY|SELL, price 지정가",
                        lambda symbol, side, quantity, price: c.trader.prepare_order(symbol, side, "LIMIT", quantity, price)),
    "prepare_bracket": ("익절·손절 OCO 미리보기(전송 안 함)",
                        lambda symbol, quantity, tp, sl: c.trader.prepare_conditional(
                            symbol, "OCO", quantity,
                            {"orderSide": "SELL", "triggerPrice": tp, "orderPrice": tp},
                            {"orderSide": "SELL", "triggerPrice": sl, "orderPrice": sl})),
    # submit 은 노출하지 않음
}

def run_tool(name: str, args: dict) -> dict:
    try:
        return {"ok": True, "result": TOOLS[name][1](**args)}
    except SafetyError as e:
        return {"ok": False, "safety": e.to_dict()}
    except (TossAPIError, ValueError) as e:
        return {"ok": False, "error": str(e)}

def human_approve_and_submit(confirm_id: str) -> dict:
    """UI에서 사람이 승인 버튼을 눌렀을 때만 호출."""
    return c.trader.submit(confirm_id)
```

- 도구 결과는 그대로 JSON 직렬화해서 모델에 돌려주면 됩니다 (숫자는 이미 문자열).
- 캔들처럼 큰 응답은 최근 N개나 요약 통계로 줄여서 넘기면 컨텍스트를 아낄 수 있습니다.
- 에이전트 프로세스가 여러 개여도 토큰은 `~/.config/toss/token.json` 하나를 함께 씁니다. 각자 `auth refresh`를 하면 서로의 토큰을 무효로 만드니 하지 마세요.

> 💡 `toss_utils.tools.build_tools(lambda: c)`를 쓰면 MCP 서버와 같은 도구 목록(이름, 설명, 함수, 종류)을 그대로 가져올 수 있습니다.

### 4-5. 아직 없는 것 (에이전트 연동 관련)
- **셸 경로에서 사람만 전송할 수 있게 강제하는 장치**: MCP 경로에는 전송 도구가 없지만, 셸 도구가 있는 에이전트가 `toss order submit`이나 `--yes`를 실행하면 막을 방법이 권한 설정뿐입니다. 터미널(TTY)에서 직접 입력해야만 전송되게 하는 옵션을 추가할 예정입니다.
- WebSocket 공유 데몬(연결 1개를 여러 프로세스가 나눠 쓰기), 체결 대기(`wait_for_fill`) 도구

---

## 5. 파일 위치 (`~/.config/toss/`, `TOSS_HOME`으로 변경 가능)

| 파일 | 내용 |
|---|---|
| `.env` | 자격증명 (프로젝트 `.env`로 링크) |
| `token.json` (권한 600), `token.lock` | 공유 토큰 캐시와 파일 락 |
| `config.json` | 기본 계좌 (`account_seq`) |
| `policy.json` | 안전 정책 |
| `KILL` | 이 파일이 있으면 킬 스위치가 켜진 상태 |
| `pending/` | 미리보기 확인 대기 (120초 뒤 만료) |
| `ledger.jsonl` | 이 도구로 전송한 주문의 추정 금액 (하루 한도 계산용) |
| `lineage.jsonl` | 주문 ID 연결 기록 (생성 → 정정 → 취소), `order track`에서 사용 |
| `~/.local/share/toss/` (`TOSS_DATA`로 변경) | 실시간 기록: `raw/`(원본 NDJSON.gz), `live.db`(SQLite WAL), `parquet/`(보관) |
| (선택) `toss stream --log` 파일 | 받은 실시간 이벤트 기록 |
| `audit.jsonl` | 모든 API 호출 메타데이터 (경로, 상태, requestId, 오류 코드). 요청·응답 본문은 기록하지 않음 |

## 6. 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| `TOSS_CLIENT_ID ... 가 없습니다` | `.env`가 없거나 비어 있음 | 1-2 참고, 전역 설치라면 1-3의 링크 확인 |
| `invalid_client` | 키 값이 틀림 (메시지에 id와 secret 중 어느 쪽인지 나옴) | 복사 버튼으로 다시 붙여넣기. secret은 재발급 직후 복사 |
| `access_denied`, `ip-not-allowed` | 허용 IP에 없음 (공유기 재시작 등으로 IP가 바뀜) | `curl -s https://api.ipify.org`로 확인 후 WTS에 등록 |
| 키 재발급 후 401 | 예전 토큰 캐시가 남아 있음 | `toss auth logout` |
| `계좌가 지정되지 않았습니다` | 기본 계좌 없음 | `toss account use N` |
| 종료 코드 3 | 안전장치 거부 | 출력의 `failed`를 보고 수량·가격을 조정하거나 정책 확인 |
| `investor-exchange-not-integrated` | 국내 거래소 설정이 통합(SOR)이 아님 | 앱에서 설정 변경 |
| `opposite-pending-order-exists` (409) | 같은 종목에 반대 방향 미체결 주문 있음 (국내) | 기존 주문 취소 후 다시 |
| `insufficient-buying-power` (미국) | 달러 부족 (자동환전 없음) | 앱에서 환전 |
| 조건주문 `condition-already-met` | 감시가가 이미 충족된 가격 | 감시가 조정 |

에러를 고객센터에 문의할 때는 출력의 `requestId`를 함께 보내세요.

## 7. 설계 메모
- **토큰 공유**: 클라이언트당 토큰은 1개만 유효합니다. 그래서 파일 락 아래에서 모든 프로세스가 캐시 하나를 함께 씁니다. 401 `token-revoked`를 받으면 먼저 캐시를 다시 읽고, 다른 프로세스가 받은 새 토큰이 있으면 재발급하지 않고 그 토큰을 씁니다.
- **호출 한도**: API 그룹별로 공식 수치에 맞춘 token bucket을 쓰고(`ratelimit.py`), 429를 받으면 `Retry-After`만큼 기다렸다 다시 시도합니다. 여러 프로세스의 호출을 합쳐서 제어하지는 않습니다.
- **재시도**: GET은 5xx와 네트워크 오류에서 다시 시도합니다. 주문과 조건주문 생성은 `clientOrderId`(= confirm_id) 멱등키가 있을 때만 같은 키로 다시 보냅니다. 정정과 취소는 자동으로 다시 보내지 않습니다.
- **전송 결과를 알 수 없을 때**(네트워크가 끊긴 경우)는 장부에 `outcome: "unknown"`으로 기록하고 한도 계산에 포함합니다. 이때는 `toss order list`로 실제 접수 여부를 확인하세요.

## 8. 테스트

```bash
uv run pytest -q
```

실제 API를 호출하지 않고 `tests/fake_server.py`(httpx MockTransport)로 테스트합니다 (59개). 검증하는 내용은 다음과 같습니다.
- 토큰 공유, 동시 발급, 무효가 된 토큰 복구
- 오류 응답 매핑, 429·5xx 재시도, 페이지네이션
- 주문 미리보기는 전송하지 않는지, 확인 ID가 한 번만 쓰이는지
- 한도, 시장가 금지, 킬 스위치, 확인 만료
- 국내·미국 주문 규칙과 OCO 규칙
- 네트워크 오류 시 같은 `clientOrderId`로 재시도하는지
- 정정·취소 후 예전 ID로 추적·정정·취소해도 최신 ID로 처리되는지, 연결 기록이 없을 때 stale 경고를 내는지
- 실시간 기록: 봉·VWAP 집계, flush를 나눠도 봉 합계가 맞는지, 고가 종목 대량 체결 오버플로(실측 회귀), 호가 상태(정상·같음·역전), 재연결 공백, 재동기화 후 `CHECK_REST`, **기록 중 다른 연결에서 동시 읽기(WAL)**, 원본 NDJSON.gz, Parquet decimal 정확도, 키 없이 `toss live`, MCP 읽기 도구
- 실시간: 로컬 WebSocket 가짜 서버로 선언 형식, PING, auth-revoked·server-shutdown 재연결과 재선언, 거부 토픽 제외, 핸드셰이크 401·403, 느린 소비자에서 주문 이벤트 무손실, CLI NDJSON, MCP 도구
- MCP: 전송 도구가 없는지, 미리보기 도구가 주문을 보내지 않는지, 취소가 정책으로 막히는지, 스키마와 힌트, 실제 프로세스의 stdout이 JSON-RPC만 내보내는지
- CLI 종료 코드

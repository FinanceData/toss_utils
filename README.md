# toss_utils

토스증권 웹 API를 호출해 JSON(`dict` / `list`)으로 반환하는 유틸리티.
인증 없이 공개 API만 사용한다. 모든 함수는 네트워크 요청이 필요하다.

## 설치

### uv tool (권장)

```bash
# 로컬 설치 → 전역 `toss` 명령
uv tool install .

# --output df 용 pandas 포함
uv tool install ".[df]"

# 설치 없이 일회 실행
uv tool run --from . toss ticks 005930

# git 저장소에서 (remote URL 기준)
uv tool install git+https://github.com/<user>/toss_utils.git
```

### pip

```bash
pip install httpx
# 또는 (CLI 포함)
pip install -e .
```

`uv tool install` 또는 `pip install -e .` 후 `toss` 명령을 사용한다. 미설치 시 `python main.py` 또는 `uv tool run --from . toss`로 실행한다.

## CLI

```bash
toss ticks 005930
toss orderbook 000660
toss news hot

# 출력 형식 지정 (기본: json)
toss ticks 005930 --count 50 --output md
toss --output df orderbook 000660
toss news hot -o json
```

### `--output` / `-o`


| 값      | 설명                                       |
| ------ | ---------------------------------------- |
| `json` | JSON (기본)                                |
| `md`   | 마크다운 테이블                                 |
| `df`   | pandas 텍스트 테이블 (`uv tool install ".[df]"` 또는 `pip install pandas`) |


### 명령어


| 명령 | 예시 |
|------|------|
| `search` | `toss search 삼성전자` |
| `info` | `toss info 005930` |
| `detail` | `toss detail 000660` |
| `prices` | `toss prices 005930 000660` |
| `chart` | `toss chart 005930 --freq day:1 --count 100` |
| `ticks` | `toss ticks 005930 --count 100` |
| `orderbook` | `toss orderbook 000660` |
| `trend` | `toss trend 005930 --date 2026-07-01` |
| `realtime-rankings` | `toss realtime-rankings` |
| `rankings` | `toss rankings --id heavy_soar --duration 20d` |
| `exchange-rates` | `toss exchange-rates` |
| `events` | `toss events` |
| `hours` | `toss hours` |
| `ai-signals` | `toss ai-signals 005930 000660` |
| `news` | `toss news hot` |


`news` 타입 별칭: `all`/`highlight` → 주요 뉴스, `hot` → 최신, `soaring` → 급상승 종목

## Python API

```python
import toss_utils as toss

# 종목 검색 → 코드 확인
stocks = toss.search("삼성전자")
code = stocks[0]["stockCode"]  # 'A005930'

# 현재가
toss.prices(["005930", "000660"])

# 일봉 100개
toss.chart("005930", freq="day:1", count=100)

# 체결 틱 / 호가
toss.ticks("000660", count=50)
toss.orderbook("000660")
```



### 종목 코드

- 국내 주식 6자리(`005930`)는 자동으로 `A` 접두사가 붙는다.
- 미국 주식은 `US...` 형식 그대로 사용한다.
- 반환값은 `list[dict]` 또는 `dict`. 결과 없으면 `[]` / `{}`.

---



## 종목 조회



### `search(query)`

종목명·티커로 검색.

```python
toss.search("하이닉스")
# keys: stockName, stockCode, companyName, ...
```



### `info(code)`

종목 메타데이터 (이름, 시장, 통화, 상장일 등).

```python
toss.info("000660")
```



### `detail(code)`

종목 상세 UI 정보 (배지, 공지, NXT 지원 여부 등).

```python
toss.detail("005930")
```



### `prices(codes)`

여러 종목 현재가 스냅샷.

```python
toss.prices(["005930", "000660", "US20100211003"])
# keys: productCode, close, base, volume, currency, ...
```

---



## 시세 / 차트



### `chart(code, freq="min:10", s_type="kr-s", count=450, from_dt=None)`

캔들 차트. `count`가 450을 넘으면 자동 페이지네이션.

```python
# 10분봉 최근 450개 (기본)
toss.chart("005930")

# 일봉 1년
toss.chart("005930", freq="day:1", count=250)

# 미국 주식
toss.chart("US20100211003", s_type="us-s", freq="day:1")

# 특정 시각 이전 캔들 (과거 방향)
toss.chart("005930", freq="min:1", count=100,
        from_dt="2026-06-10T15:00:00+09:00")
```


| `freq`                                                            | 설명  |
| ----------------------------------------------------------------- | --- |
| `min:1`, `min:3`, `min:5`, `min:10`, `min:15`, `min:30`, `min:60` | 분봉  |
| `day:1`                                                           | 일봉  |
| `week:1`                                                          | 주봉  |
| `month:1`                                                         | 월봉  |



| `s_type` | 설명         |
| -------- | ---------- |
| `kr-s`   | 국내 주식 (기본) |
| `us-s`   | 미국 주식      |


반환 키: `dt`, `open`, `high`, `low`, `close`, `volume`, `amount`, `base`

### `ticks(code, count=450)`

체결(틱) 내역. 최대 450개.

```python
toss.ticks("000660", count=100)
# keys: time, price, volume, tradeType(BUY/SELL), cumulativeVolume, ...
```



### `orderbook(code)`

호가 10단계. level 1이 최우선 호가.

```python
toss.orderbook("000660")
# keys: level, offerPrice, offerVolume, bidPrice, bidVolume
```



### `trend(code, date=None)`

투자자별 순매수 추이 (개인·외국인·기관). `date` 미지정 시 오늘.

```python
toss.trend("005930")
toss.trend("005930", date="2026-07-01")
# keys: baseDate, netIndividualsBuyVolume, netForeignerBuyVolume, netInstitutionBuyVolume
```

---



## 시장 / 랭킹



### `realtime_rankings()`

실시간 인기 종목 랭킹.

```python
toss.realtime_rankings()
```



### `rankings(ranking_id, duration, tag, filters)`

거래대금·거래량·급등·급락 랭킹.

```python
# 거래대금 상위 (기본)
toss.rankings()

# 급상승, 국내, 1개월
toss.rankings("heavy_soar", duration="20d", tag="kr")

# 해외 거래량
toss.rankings("biggest_market_volume", tag="us")
```


| `ranking_id`            | 설명        |
| ----------------------- | --------- |
| `biggest_market_amount` | 시장 거래대금   |
| `biggest_market_volume` | 시장 거래량    |
| `biggest_total_amount`  | 토스증권 거래대금 |
| `biggest_total_volume`  | 토스증권 거래량  |
| `heavy_soar`            | 급상승       |
| `heavy_descent`         | 급하락       |



| `duration`                         | 설명    |
| ---------------------------------- | ----- |
| `realtime`                         | 실시간   |
| `5d`, `20d`, `60d`, `120d`, `240d` | 1주~1년 |



| `tag` | 설명      |
| ----- | ------- |
| `kr`  | 국내 (기본) |
| `us`  | 해외      |
| `all` | 전체      |


---



## 기타



### `exchange_rates()`

환율·달러 인덱스.

```python
toss.exchange_rates()
```



### `events()`

경제 캘린더 일정.

```python
toss.events()
```



### `hours()`

국내·미국·ATS 통합 거래 시간 상태.

```python
toss.hours()
```



### `ai_signals(code)`

종목 AI 시그널. 단일 코드 또는 리스트.

```python
toss.ai_signals("005930")
toss.ai_signals(["005930", "000660"])
```



### `news(news_type="ALL_HIGHLIGHT")`

뉴스 피드.

```python
toss.news()                        # 주요 뉴스
toss.news("HOT")                   # 최신
toss.news("SOARING_STOCK")         # 급상승 종목
```


| `news_type`         | 설명           |
| ------------------- | ------------ |
| `ALL_HIGHLIGHT`     | 주요 뉴스 (기본)   |
| `HOT`               | 최신 뉴스        |
| `SOARING_STOCK`     | 급상승 종목 뉴스    |
| `PERSONALIZE_HOLD`  | 보유주식 (인증 필요) |
| `PERSONALIZE_WATCH` | 관심주식 (인증 필요) |


---



## 테스트

실제 API를 호출하는 통합 테스트. 주요 결과를 화면에 출력한다.

```bash
pytest -s test_toss_utils.py
pytest -s test_toss_utils.py::test_orderbook   # 개별
python test_toss_utils.py
```



## 참고

- 원본 API 엔드포인트 정리: `[toss-invest.http](toss-invest.http)`
- 비공식 API이므로 엔드포인트·응답 형식이 변경될 수 있다.


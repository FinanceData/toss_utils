import httpx
from datetime import date as _date
from typing import List, Dict, Any, Optional, Union

JsonValue = Union[Dict[str, Any], List[Any]]

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def _headers(referer: Optional[str] = None) -> Dict[str, str]:
    """
    브라우저 요청을 흉내 내어 403 Forbidden 응답을 피하기 위한 표준 요청 헤더를 반환합니다.
    """
    headers_dict = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if referer:
        headers_dict["Referer"] = referer
    return headers_dict

def _normalize_code(code: str) -> str:
    """
    국내 주식 6자리 코드(예: 005930)에 'A' 접두사가 없는 경우 자동으로 붙여줍니다.
    """
    if len(code) == 6 and code.isdigit():
        return f"A{code}"
    return code

def _coerce_codes(code: str | List[str]) -> List[str]:
    if isinstance(code, str):
        return [_normalize_code(code)]
    return [_normalize_code(c) for c in code]

def search(query: str) -> List[Dict[str, Any]]:
    """
    검색어와 일치하는 종목을 검색합니다.
    """
    url = "https://wts-info-api.tossinvest.com/api/v2/search/stocks"
    headers_dict = _headers(referer="https://www.tossinvest.com/")
    payload = {"query": query}

    with httpx.Client(timeout=10.0) as client:
        response = client.post(url, json=payload, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("stocks", [])

def info(code: str) -> Dict[str, Any]:
    """
    종목 메타데이터(이름, 시장, 통화 등)를 조회합니다.
    """
    code = _normalize_code(code)
    url = f"https://wts-info-api.tossinvest.com/api/v2/stock-infos/{code}"
    headers_dict = _headers(referer=f"https://www.tossinvest.com/stocks/{code}")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {})

def detail(code: str) -> Dict[str, Any]:
    """
    종목의 공통 UI 배지, 공지사항 및 상세 정보를 조회합니다.
    """
    code = _normalize_code(code)
    url = f"https://wts-info-api.tossinvest.com/api/v1/stock-detail/ui/{code}/common"
    headers_dict = _headers(referer=f"https://www.tossinvest.com/stocks/{code}")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {})

def prices(codes: List[str]) -> List[Dict[str, Any]]:
    """
    여러 종목 코드에 대한 현재가 스냅샷을 조회합니다.
    """
    normalized_codes = [_normalize_code(c) for c in codes]
    codes_str = ",".join(normalized_codes)
    url = "https://wts-info-api.tossinvest.com/api/v1/product/stock-prices"
    params = {"meta": "true", "productCodes": codes_str}
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", [])

def chart(
    code: str,
    freq: str = "min:10",
    s_type: str = "kr-s",
    count: int = 450,
    from_dt: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    종목의 캔들 차트 데이터를 조회합니다.

    인자:
        code: 종목 코드 (예: 'A005930' 또는 미국 주식 코드)
        freq: 차트 주기, 예: 'min:1', 'min:5', 'min:10', 'min:60', 'day:1', 'week:1', 'month:1'
        s_type: 주식 타입, 예: 한국 주식 'kr-s', 미국 주식 'us-s'
        count: 가져올 데이터 포인트 개수 (450 초과 시 nextDateTime으로 자동 페이지네이션)
        from_dt: 기준 시각 (ISO 8601, 예: '2026-06-10T09:00:00+09:00').
                 지정 시 해당 시각 이전(포함) 캔들을 과거 방향으로 조회합니다.
                 미지정 시 최신 데이터부터 조회합니다.
    """
    code = _normalize_code(code)
    url = f"https://wts-info-api.tossinvest.com/api/v1/c-chart/{s_type}/{code}/{freq}"
    headers_dict = _headers(referer=f"https://www.tossinvest.com/stocks/{code}")

    candles: List[Dict[str, Any]] = []
    cursor = from_dt
    max_per_request = 450

    with httpx.Client(timeout=30.0) as client:
        while len(candles) < count:
            batch_size = min(max_per_request, count - len(candles))
            params: Dict[str, Any] = {
                "count": batch_size,
                "useAdjustedRate": "true",
            }
            if cursor:
                params["from"] = cursor
            else:
                params["investMode"] = "integrated"

            response = client.get(url, params=params, headers=headers_dict)
            response.raise_for_status()
            result = response.json().get("result", {})
            page = result.get("candles", [])
            if not page:
                break
            candles.extend(page)
            cursor = result.get("nextDateTime")
            if not cursor:
                break

    return candles

def ticks(code: str, count: int = 450) -> List[Dict[str, Any]]:
    """
    종목의 체결(틱) 내역을 조회합니다.

    인자:
        code: 종목 코드 (예: '000660' 또는 'A000660')
        count: 가져올 체결 개수 (1 ~ 450)
    """
    code = _normalize_code(code)
    url = f"https://wts-info-api.tossinvest.com/api/v2/stock-prices/{code}/ticks"
    params = {"count": count}
    headers_dict = _headers(referer=f"https://www.tossinvest.com/stocks/{code}")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", [])

def orderbook(code: str) -> List[Dict[str, Any]]:
    """
    종목의 호가(orderbook)를 조회합니다.

    매도(offer)·매수(bid) 각 10호가를 level별로 정렬한 리스트를 반환합니다.
    level 1이 최우선 호가입니다.

    인자:
        code: 종목 코드 (예: '000660' 또는 'A000660')
    """
    code = _normalize_code(code)
    url = f"https://wts-info-api.tossinvest.com/api/v3/stock-prices/{code}/quotes"
    headers_dict = _headers(referer=f"https://www.tossinvest.com/stocks/{code}")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        result = data.get("result", {})
        if not result:
            return []

        # offer 배열은 원거리→최우선(내림차순), bid 배열은 최우선→원거리 순서
        offer_prices = list(reversed(result.get("offerPrices", [])))
        offer_volumes = list(reversed(result.get("offerVolumes", [])))
        bid_prices = result.get("bidPrices", [])
        bid_volumes = result.get("bidVolumes", [])
        depth = max(len(offer_prices), len(bid_prices))

        rows = []
        for i in range(depth):
            rows.append({
                "level": i + 1,
                "offerPrice": offer_prices[i] if i < len(offer_prices) else None,
                "offerVolume": offer_volumes[i] if i < len(offer_volumes) else None,
                "bidPrice": bid_prices[i] if i < len(bid_prices) else None,
                "bidVolume": bid_volumes[i] if i < len(bid_volumes) else None,
            })
        return rows

def trend(code: str, date: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    종목의 투자자별 순매수 추이를 조회합니다.

    기본적으로 개인·외국인·기관의 순매수(net buy volume)를 반환합니다.

    인자:
        code: 종목 코드 (예: '005930' 또는 'A005930')
        date: 기준일 (YYYY-MM-DD). 미지정 시 오늘 날짜
    """
    code = _normalize_code(code)
    key = date or _date.today().isoformat()
    url = "https://wts-info-api.tossinvest.com/api/v1/stock-infos/trade/trend/trading-trend"
    params = {
        "productCode": code,
        "size": 60,
        "number": 3,
        "key": key,
    }
    headers_dict = _headers(referer=f"https://www.tossinvest.com/stocks/{code}")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, params=params, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        body = data.get("result", {}).get("body", [])
        if not body:
            return []

        cols = {"baseDate", "netIndividualsBuyVolume", "netForeignerBuyVolume", "netInstitutionBuyVolume"}
        return [{k: row[k] for k in cols if k in row} for row in body]

def realtime_rankings() -> List[Dict[str, Any]]:
    """
    실시간 인기 종목 랭킹을 조회합니다.
    """
    url = "https://wts-info-api.tossinvest.com/api/v1/rankings/realtime/stock"
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("data", [])

def rankings(
    ranking_id: str = "biggest_market_amount",
    duration: str = "realtime",
    tag: str = "kr",
    filters: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    거래량, 거래대금, 급상승, 급하락 등 고급 시장 랭킹을 조회합니다.

    인자:
        ranking_id:
            - `biggest_total_amount`: 토스증권 거래대금
            - `biggest_total_volume`: 토스증권 거래량
            - `biggest_market_amount`: 시장 거래대금
            - `biggest_market_volume`: 시장 거래량
            - `heavy_soar`: 실시간 급상승
            - `heavy_descent`: 실시간 급하락
        duration:
            - `realtime` (실시간)
            - `5d` (1주일), `20d` (1개월), `60d` (3개월), `120d` (6개월), `240d` (1년)
        tag:
            - `all` (전체), `kr` (국내), `us` (해외)
        filters: 필터 목록. None일 경우 기본값 지정:
                 ["KRX_MANAGEMENT_STOCK",
                  "MARKET_CAP_GREATER_THAN_50M",
                  "STOCKS_PRICE_GREATER_THAN_ONE_DOLLAR"]
    """
    url = "https://wts-cert-api.tossinvest.com/api/v2/dashboard/wts/overview/ranking"
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    if filters is None:
        filters = [
            "KRX_MANAGEMENT_STOCK",
            "MARKET_CAP_GREATER_THAN_50M",
            "STOCKS_PRICE_GREATER_THAN_ONE_DOLLAR"
        ]

    payload = {
        "id": ranking_id,
        "filters": filters,
        "duration": duration,
        "tag": tag
    }

    with httpx.Client(timeout=10.0) as client:
        response = client.post(url, json=payload, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("products", [])

def exchange_rates() -> List[Dict[str, Any]]:
    """
    환율 및 달러 인덱스를 조회합니다.
    """
    url = "https://wts-info-api.tossinvest.com/api/v1/dashboard/wts/overview/exchange-rates"
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("exchangeRates", [])

def events() -> List[Dict[str, Any]]:
    """
    경제 캘린더 일정을 조회합니다.
    """
    url = "https://wts-info-api.tossinvest.com/api/v2/dashboard/wts/overview/calendar/economic-events"
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("events", [])

def hours() -> Dict[str, Any]:
    """
    통합 시장 거래 시간 상태를 조회합니다.
    """
    url = "https://wts-api.tossinvest.com/api/v2/system/trading-hours/integrated"
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    with httpx.Client(timeout=10.0) as client:
        response = client.get(url, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {})

def ai_signals(code: str | List[str]) -> List[Dict[str, Any]]:
    """
    종목 AI 시그널 정보를 조회합니다.

    code에 단일 종목 코드(str) 또는 종목 코드 리스트를 전달할 수 있습니다.
    productCodes JSON 배열로 한 번의 POST 요청으로 조회합니다.
    """
    product_codes = _coerce_codes(code)
    if not product_codes:
        return []

    url = "https://wts-info-api.tossinvest.com/api/v1/dashboard/wts/overview/ai-signals"
    payload: Dict[str, Any] = {"productCodes": product_codes}
    headers_dict = _headers(referer="https://www.tossinvest.com/")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(url, json=payload, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("signals", [])

def news(news_type: str = "ALL_HIGHLIGHT") -> List[Dict[str, Any]]:
    """
    뉴스 기사를 카테고리별로 조회합니다.

    인자:
        news_type:
            - `ALL_HIGHLIGHT`: 주요 뉴스
            - `HOT`: 최신 뉴스
            - `SOARING_STOCK`: 급상승 종목 뉴스
            - `PERSONALIZE_HOLD`: 보유주식 뉴스 (실제 인증 필요)
            - `PERSONALIZE_WATCH`: 관심주식 뉴스 (실제 인증 필요)
    """
    url = "https://wts-info-api.tossinvest.com/api/v1/dashboard/wts/news"
    headers_dict = _headers(referer="https://www.tossinvest.com/")
    payload = {"type": news_type}

    with httpx.Client(timeout=10.0) as client:
        response = client.post(url, json=payload, headers=headers_dict)
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("news", [])

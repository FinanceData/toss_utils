"""시세·종목 정보 카테고리 (토큰만 필요, 26개 엔드포인트). 응답은 서버 JSON 그대로(dict/list, 숫자는 문자열)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Sequence

if TYPE_CHECKING:
    from ..client import TossClient

TREND_KINDS = {  # GET /api/v1/stocks/{symbol}/<kind>  (국내 종목 전용)
    "investor-trading", "program-trades", "short-selling", "credit-trades", "securities-lending",
}


def _symbols(symbols: str | Sequence[str]) -> str:
    if isinstance(symbols, str):
        symbols = [s for s in symbols.replace(" ", "").split(",") if s]
    if not symbols or len(symbols) > 200:
        raise ValueError("symbols 는 1~200개여야 합니다.")
    return ",".join(symbols)


class MarketAPI:
    def __init__(self, client: "TossClient"):
        self._c = client

    def _get(self, path, group, **params):
        return self._c.request("GET", path, group=group, params=params)

    # ---- Market Data
    def prices(self, symbols: str | Sequence[str]):
        return self._get("/api/v1/prices", "MARKET_DATA", symbols=_symbols(symbols))

    def orderbook(self, symbol: str):
        return self._get("/api/v1/orderbook", "MARKET_DATA", symbol=symbol)

    def trades(self, symbol: str, count: int | None = None):
        return self._get("/api/v1/trades", "MARKET_DATA", symbol=symbol, count=count)

    def price_limits(self, symbol: str):
        return self._get("/api/v1/price-limits", "MARKET_DATA", symbol=symbol)

    def candles(self, symbol: str, interval: str = "1d", count: int | None = None,
                before: str | None = None, adjusted: bool | None = None):
        """최신순 정렬. 반환: {"candles": [...], "nextBefore": str|None}"""
        if interval not in ("1m", "1d"):
            raise ValueError("interval 은 '1m' 또는 '1d'")
        return self._get("/api/v1/candles", "MARKET_DATA_CHART", symbol=symbol, interval=interval,
                         count=count, before=before, adjusted=adjusted)

    def iter_candles(self, symbol: str, interval: str = "1d", limit: int = 1000,
                     adjusted: bool | None = None) -> Iterator[dict]:
        """nextBefore 를 따라가며 최신→과거 순으로 최대 limit 개 봉을 돌려준다."""
        before, n = None, 0
        while n < limit:
            page = self.candles(symbol, interval, min(200, limit - n), before, adjusted)
            for c in page.get("candles", []):
                yield c
                n += 1
                if n >= limit:
                    return
            before = page.get("nextBefore")
            if not before or not page.get("candles"):
                return

    # ---- Stock Info
    def stocks(self, symbols: str | Sequence[str]):
        return self._get("/api/v1/stocks", "STOCK", symbols=_symbols(symbols))

    def stocks_all(self, market: str, status: str | None = None, security_type: str | None = None,
                   common_share: bool | None = None):
        """초당 1회 한도 + 일 배치 데이터 → 하루 1회 받아 캐시해서 쓰세요."""
        return self._get("/api/v1/stocks/all", "STOCK_ALL", market=market, status=status,
                         securityType=security_type, commonShare=common_share)

    def warnings(self, symbol: str):
        return self._get(f"/api/v1/stocks/{symbol}/warnings", "STOCK")

    def trend(self, kind: str, symbol: str, count: int | None = None, until: str | None = None):
        """국내 종목 일별 수급. kind ∈ investor-trading|program-trades|short-selling|credit-trades|securities-lending"""
        if kind not in TREND_KINDS:
            raise ValueError(f"kind 는 {sorted(TREND_KINDS)} 중 하나")
        return self._get(f"/api/v1/stocks/{symbol}/{kind}", "STOCK_TRADING_TREND", count=count, until=until)

    # ---- Market Info
    def exchange_rate(self, base: str = "USD", quote: str = "KRW", date_time: str | None = None):
        return self._get("/api/v1/exchange-rate", "MARKET_INFO", baseCurrency=base, quoteCurrency=quote,
                         dateTime=date_time)

    def calendar(self, market: str = "KR", date: str | None = None):
        market = market.upper()
        if market not in ("KR", "US"):
            raise ValueError("market 은 KR 또는 US")
        return self._get(f"/api/v1/market-calendar/{market}", "MARKET_INFO", date=date)

    # ---- Ranking
    def rankings(self, type: str, market_country: str = "KR", duration: str = "1d",
                 exclude_investment_caution: bool | None = None, count: int | None = None):
        """type: MARKET_TRADING_AMOUNT|MARKET_TRADING_VOLUME|TOP_GAINERS|TOP_LOSERS|TOSS_SECURITIES_TRADING_AMOUNT|
        TOSS_SECURITIES_TRADING_VOLUME. duration: realtime|1d|1w|1mo|3mo|6mo|1y (TOP_GAINERS/LOSERS 는 realtime 불가)"""
        return self._get("/api/v1/rankings", "RANKING", type=type, marketCountry=market_country,
                         duration=duration, excludeInvestmentCaution=exclude_investment_caution, count=count)

    def investor_rankings(self, count: int | None = None):
        """국내 외국인·기관·개인 순매수/순매도 상위."""
        return self._get("/api/v1/rankings/investor-trading", "RANKING", count=count)

    # ---- Market Indicators
    def indicator_prices(self, symbols: str | Sequence[str]):
        """국내 지수·국채 현재가."""
        return self._get("/api/v1/market-indicators/prices", "MARKET_INDICATOR_PRICE", symbols=_symbols(symbols))

    def indicator_candles(self, symbol: str, interval: str = "1d", count: int | None = None,
                          before: str | None = None):
        return self._get(f"/api/v1/market-indicators/{symbol}/candles", "MARKET_INDICATOR_CHART",
                         interval=interval, count=count, before=before)

    def indicator_investor_trading(self, symbol: str = "KOSPI", interval: str = "1d",
                                   count: int | None = None, until: str | None = None):
        """symbol: KOSPI|KOSDAQ, interval: 1d|1w|1mo|1y"""
        return self._get(f"/api/v1/market-indicators/{symbol}/investor-trading", "MARKET_INDICATOR",
                         interval=interval, count=count, until=until)

    # ---- Sector
    def sectors(self):
        """전체 섹터 계층 (depth 0~2). 하루 1회 받아 캐시 권장."""
        return self._get("/api/v1/sectors", "SECTOR")

    def sector(self, sector_id: str, market_country: str = "KR"):
        return self._get(f"/api/v1/sectors/{sector_id}", "SECTOR", marketCountry=market_country)

    def sector_stocks(self, sector_id: str, market_country: str = "KR"):
        return self._get(f"/api/v1/sectors/{sector_id}/stocks", "SECTOR", marketCountry=market_country)

    def sector_etfs(self, sector_id: str, market_country: str = "KR"):
        return self._get(f"/api/v1/sectors/{sector_id}/etfs", "SECTOR", marketCountry=market_country)

    def sector_rankings(self, type: str = "TOP_GAINERS", market_country: str = "KR", duration: str = "1d"):
        """type: TOP_GAINERS|MARKET_TRADING_AMOUNT, duration: 1d|1w|1mo|3mo|6mo|1y"""
        return self._get("/api/v1/sectors/rankings", "SECTOR_RANKING", type=type,
                         marketCountry=market_country, duration=duration)

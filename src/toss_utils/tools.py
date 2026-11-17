"""에이전트 도구 정의 (MCP 서버와 function calling 이 함께 쓰는 단일 출처).

원칙
- read    : 조회 전용. 자유롭게 호출.
- prepare : 미리보기만 만든다(서버에 주문 안 보냄). 전송은 사람이 터미널에서 `toss order submit <id>`.
- cancel  : 정책 `agent_can_cancel` 이 true 일 때만 동작 (기본 false).
- **전송(submit) 도구는 의도적으로 없다.** 정책 변경·킬 스위치 해제·토큰 재발급 도구도 없다.

모든 도구는 예외를 던지지 않고 {"ok": False, "error": {...}} 를 돌려준다 (에이전트가 이유를 읽을 수 있게).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal

from .errors import ConfigError, SafetyError, TossAPIError, TossError
from .safety.policy import Ledger, Policy, is_killed
from .tracking import track

if TYPE_CHECKING:
    from .client import TossClient

Kind = Literal["read", "prepare", "cancel"]

INSTRUCTIONS = """토스증권 계좌 도구입니다. 실제 계좌에 연결되어 있고 모의투자 환경이 없습니다.
규칙:
1. 조회 도구(get_*, list_*, track_order)는 자유롭게 사용합니다. 숫자는 문자열(decimal)입니다.
2. 주문·조건주문은 prepare_* 로 미리보기만 만들 수 있습니다. 이 서버에는 전송 도구가 없습니다.
   미리보기의 종목명·방향·수량·가격·추정 원화 금액·실패한 검사를 사용자에게 보여주고,
   전송은 사용자가 터미널에서 `toss order submit <confirm_id>` 를 직접 실행하도록 안내하세요 (기본 120초 유효).
3. ok=false 이고 error.type=safety 이면 failed 항목(한도·시장가 금지 등)을 그대로 사용자에게 전하세요.
   한도 상향·시장가 허용·정책 변경·킬 스위치 해제를 제안하지 마세요.
4. 주문 상태는 track_order 로 확인하세요. 정정·취소 전의 orderId 는 상세 조회가 갱신되지 않습니다.
5. 미국 주식은 자동환전이 없습니다(달러 매수 가능 금액 확인). 국내 조건주문은 KRX 정규장에서만 발동합니다."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    description: str
    kind: Kind


def _wrap(fn):
    """예외를 에이전트가 읽을 수 있는 dict 로 바꾼다. 시그니처·타입 힌트는 유지(MCP 스키마 생성용)."""
    @functools.wraps(fn)
    def inner(*args, **kwargs):
        try:
            return {"ok": True, "result": fn(*args, **kwargs)}
        except SafetyError as e:
            return {"ok": False, "error": e.to_dict()}
        except TossAPIError as e:
            return {"ok": False, "error": {"type": "api", **e.to_dict()}}
        except ConfigError as e:
            return {"ok": False, "error": {"type": "config", "message": str(e)}}
        except ValueError as e:
            return {"ok": False, "error": {"type": "invalid-argument", "message": str(e)}}
        except TossError as e:
            return {"ok": False, "error": {"type": "error", "message": str(e)}}
        except FileNotFoundError as e:
            return {"ok": False, "error": {"type": "not-found", "message": str(e)}}
    return inner


def _prepared(pv: dict) -> dict:
    """미리보기 결과에 사람용 다음 단계를 붙이고, 실패면 SafetyError 형태로 돌려준다."""
    if not pv.get("ok"):
        raise SafetyError("안전장치 검사를 통과하지 못해 미리보기를 확정하지 않았습니다.", pv.get("checks"), pv)
    pv = dict(pv)
    pv["next_step_for_user"] = (f"사용자가 터미널에서 직접 실행: toss order submit {pv['confirm_id']} "
                                f"({pv.get('expires_in_sec')}초 안에). 에이전트는 전송할 수 없습니다.")
    return pv


def build_tools(get_client: Callable[[], "TossClient"]) -> list[ToolSpec]:
    """get_client 는 호출 시점에 TossClient 를 돌려주는 함수 (지연 생성: 키가 없어도 서버는 뜬다)."""
    C = get_client
    specs: list[ToolSpec] = []

    def tool(kind: Kind, description: str):
        def deco(fn):
            specs.append(ToolSpec(fn.__name__, _wrap(fn), description, kind))
            return fn
        return deco

    # ------------------------------------------------------------------ 시세·종목·시장 (read)
    @tool("read", "현재가 조회. 국내 6자리 코드와 미국 티커를 섞어 최대 200개.")
    def get_quotes(symbols: list[str]) -> list:
        return C().market.prices(symbols)

    @tool("read", "호가(매도·매수 잔량) 조회. 국내는 KRX+NXT 통합 호가.")
    def get_orderbook(symbol: str) -> dict:
        return C().market.orderbook(symbol)

    @tool("read", "캔들(OHLCV) 조회. 최신순. interval 1d(일봉) 또는 1m(분봉), count 최대 200.")
    def get_candles(symbol: str, interval: Literal["1d", "1m"] = "1d", count: int = 60) -> dict:
        return C().market.candles(symbol, interval, max(1, min(count, 200)))

    @tool("read", "종목 기본 정보(이름, 시장, 유형, 상장 상태, 거래정지 여부). 최대 200개.")
    def get_stock_info(symbols: list[str]) -> list:
        return C().market.stocks(symbols)

    @tool("read", "매수 유의사항(VI, 투자경고·위험, 단기과열, 정리매매 등). 없으면 빈 목록.")
    def get_stock_warnings(symbol: str) -> list:
        return C().market.warnings(symbol)

    @tool("read", "장 운영 시간(전일·당일·익일, KST). 국내 integrated 가 null 이면 휴장.")
    def get_market_hours(market: Literal["KR", "US"] = "KR") -> dict:
        return C().market.calendar(market)

    @tool("read", "원·달러 참고 환율(1분 갱신, 실제 체결 환율과 다를 수 있음).")
    def get_exchange_rate() -> dict:
        return C().market.exchange_rate("USD", "KRW")

    @tool("read", "주식 랭킹. type 예: MARKET_TRADING_AMOUNT, MARKET_TRADING_VOLUME, TOP_GAINERS, TOP_LOSERS. "
                  "duration: realtime,1d,1w,1mo,3mo,6mo,1y (상승·하락은 realtime 불가).")
    def get_rankings(type: str = "MARKET_TRADING_AMOUNT", market_country: Literal["KR", "US"] = "KR",
                     duration: str = "1d", count: int = 20) -> Any:
        return C().market.rankings(type, market_country, duration, None, max(1, min(count, 100)))

    # ------------------------------------------------------------------ 계좌 (read)
    @tool("read", "보유 주식과 평가손익. 요약 금액은 {krw, usd} 통화별, 종목별 금액은 해당 통화.")
    def get_portfolio(symbol: str | None = None) -> dict:
        return C().account.holdings(symbol)

    @tool("read", "현금 기준 매수 가능 금액. 미국 주식은 자동환전이 없으므로 currency=USD 로 확인.")
    def get_buying_power(currency: Literal["KRW", "USD"] = "KRW") -> dict:
        return C().account.buying_power(currency)

    @tool("read", "종목의 매도 가능 수량.")
    def get_sellable_quantity(symbol: str) -> dict:
        return C().account.sellable_quantity(symbol)

    @tool("read", "주문 목록. OPEN=진행 중(전량), CLOSED=종료(최근 limit 건).")
    def list_orders(status: Literal["OPEN", "CLOSED"] = "OPEN", symbol: str | None = None, limit: int = 20) -> dict:
        return C().orders.list(status, symbol, limit=None if status == "OPEN" else max(1, min(limit, 100)))

    @tool("read", "주문의 최신 ID 와 실제 상태. 정정·취소 전의 ID 를 넣어도 최신 상태를 돌려준다. "
                  "status 가 UNKNOWN 이면 warnings 를 사용자에게 전할 것.")
    def track_order(order_id: str) -> dict:
        return track(C(), order_id)

    @tool("read", "조건주문 목록. OPEN=감시 중·주문 진행 중, CLOSED=완료·만료.")
    def list_conditional_orders(status: Literal["OPEN", "CLOSED"] = "OPEN", symbol: str | None = None) -> dict:
        return C().orders.conditional_list(status, symbol)

    @tool("read", "현재 안전 정책(1건·일일 한도, 시장가 허용 등), 킬 스위치, 오늘 사용 금액. 변경은 사람만 가능.")
    def get_trading_policy() -> dict:
        from dataclasses import asdict
        home = C().home
        return {"policy": asdict(Policy.load(home)), "kill_switch": is_killed(home),
                "today_used_krw": str(Ledger(home).today_krw())}

    @tool("read", "전송을 기다리는 미리보기(confirm_id) 목록.")
    def list_pending_confirmations() -> list:
        return C().trader.pending()

    @tool("read", "실시간 데이터를 몇 초간 받아 모아서 돌려준다(WebSocket). topics 예: trade:kr:005930, "
                  "orderbook:us:AAPL, order(내 주문 이벤트), cond(내 조건주문 이벤트). seconds 최대 30, "
                  "max_messages 최대 200. 계정당 동시 연결이 2개뿐이라 다른 실시간 연결이 끊길 수 있음.")
    def sample_realtime(topics: list[str], seconds: float = 5, max_messages: int = 50) -> dict:
        from .stream import collect, ensure_available
        ensure_available()
        return collect(C(), topics, max(1.0, min(float(seconds), 30.0)), max(1, min(max_messages, 200)))

    @tool("read", "기록 중인 실시간 DB(`toss record`)에서 최근 봉을 조회. WebSocket 연결을 쓰지 않음. "
                  "interval 1s|1m, minutes 최대 390. 기록이 없으면 not-found 오류.")
    def get_live_bars(symbol: str, minutes: float = 30, interval: Literal["1s", "1m"] = "1m") -> list:
        from .recorder import default_db, query_bars
        return query_bars(default_db(), symbol.upper(), max(1.0, min(float(minutes), 390.0)), interval)

    @tool("read", "기록 중인 실시간 DB 에서 종목의 최신 최우선 호가(상위 5단계 포함), 스프레드, 교차 여부, 몇 초 전 값인지.")
    def get_live_top_of_book(symbol: str) -> Any:
        from .recorder import default_db, query_book
        return query_book(default_db(), symbol.upper())

    @tool("read", "실시간 기록 상태: 테이블별 행 수, 기록 중인 종목, 마지막 기록 후 경과 초, 최근 데이터 공백(재연결 구간).")
    def get_live_status() -> dict:
        from .recorder import default_db, query_status
        return query_status(default_db())

    # ------------------------------------------------------------------ 미리보기 (prepare)
    @tool("prepare", "주문 미리보기(서버에 보내지 않음). 지정가는 price 필수. 시장가(order_type=MARKET)는 정책에서 "
                     "허용돼야 함. 미국 소수점 매수는 amount(USD)+MARKET. 결과의 confirm_id 로 사용자가 직접 전송.")
    def prepare_order(symbol: str, side: Literal["BUY", "SELL"], quantity: str | None = None,
                      price: str | None = None, order_type: Literal["LIMIT", "MARKET"] = "LIMIT",
                      amount: str | None = None,
                      time_in_force: Literal["DAY", "CLS", "OPG"] | None = None) -> dict:
        return _prepared(C().trader.prepare_order(symbol, side, order_type, quantity, price, amount, time_in_force))

    @tool("prepare", "정정 미리보기(서버에 보내지 않음). 국내는 quantity 생략 시 남은 수량, 미국은 가격만 정정 가능. "
                     "정정되면 새 orderId 가 발급됨.")
    def prepare_modify(order_id: str, price: str | None = None, quantity: str | None = None,
                       order_type: Literal["LIMIT", "MARKET"] = "LIMIT") -> dict:
        return _prepared(C().trader.prepare_modify(order_id, price, quantity, order_type))

    @tool("prepare", "조건주문 미리보기(서버에 보내지 않음). type: SINGLE(조건 1개) | OCO(익절 SELL + 손절 SELL, "
                     "first 감시가 > 현재가 > second 감시가) | OTO(first BUY 체결 후 second SELL). OCO/OTO 는 지정가만. "
                     "지정가면 각 조건의 *_price 필수. expire_date 는 YYYY-MM-DD(기본 30일, 최대 1년).")
    def prepare_conditional(symbol: str, type: Literal["SINGLE", "OCO", "OTO"], quantity: str,
                            first_side: Literal["BUY", "SELL"], first_trigger: str, first_price: str | None = None,
                            second_side: Literal["BUY", "SELL"] | None = None, second_trigger: str | None = None,
                            second_price: str | None = None, order_type: Literal["LIMIT", "MARKET"] = "LIMIT",
                            expire_date: str | None = None) -> dict:
        def leg(side, trig, price):
            d = {"orderSide": side, "triggerPrice": trig}
            if price is not None:
                d["orderPrice"] = price
            return d
        second = leg(second_side, second_trigger, second_price) if second_side else None
        return _prepared(C().trader.prepare_conditional(symbol, type, quantity, leg(first_side, first_trigger, first_price),
                                                         second, order_type, expire_date))

    @tool("prepare", "전송하지 않을 미리보기(confirm_id)를 폐기.")
    def discard_confirmation(confirm_id: str) -> dict:
        return {"discarded": C().trader.discard(confirm_id)}

    # ------------------------------------------------------------------ 취소 (cancel, 정책으로 허용 시)
    def _cancel_allowed():
        if not Policy.load(C().home).agent_can_cancel:
            raise SafetyError("에이전트의 취소가 정책에서 꺼져 있습니다(agent_can_cancel=false). 사용자가 직접 "
                              "`toss order cancel <ID>` 를 실행하거나, 사람이 정책을 바꿔야 합니다.")

    @tool("cancel", "주문 취소(정책 agent_can_cancel=true 일 때만). 예전 ID 를 넣어도 최신 ID 로 취소. 새 orderId 발급.")
    def cancel_order(order_id: str) -> dict:
        _cancel_allowed()
        return C().trader.cancel(order_id)

    @tool("cancel", "조건주문 취소(정책 agent_can_cancel=true 일 때만). with_order=true 면 이미 접수된 주문도 함께 취소.")
    def cancel_conditional_order(conditional_order_id: str, with_order: bool = False) -> dict:
        _cancel_allowed()
        return C().trader.cancel_conditional(conditional_order_id, with_order)

    return specs

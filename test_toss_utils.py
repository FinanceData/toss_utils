"""
toss_utils 통합 단위 테스트.

실제 토스증권 API를 호출하므로 네트워크 연결이 필요합니다.
주요 내용을 화면에 출력하려면 -s 옵션과 함께 실행하세요.

    pytest -s test_toss_utils.py
    pytest -s test_toss_utils.py::test_orderbook
"""
import json
from typing import Any

import pytest

import toss_utils as toss

SAMPLE_CODE = "000660"  # SK하이닉스


def _show(title: str, data: Any, n: int = 5) -> None:
    """JSON 결과의 길이/키와 상위 n개 항목을 출력합니다."""
    if isinstance(data, list):
        print(f"\n{'=' * 70}\n[{title}] len={len(data)}")
        preview = data[:n]
    elif isinstance(data, dict):
        print(f"\n{'=' * 70}\n[{title}] keys={list(data.keys())}")
        preview = {k: data[k] for k in list(data.keys())[:n]}
    else:
        print(f"\n{'=' * 70}\n[{title}] type={type(data).__name__}")
        preview = data
    print(json.dumps(preview, ensure_ascii=False, indent=2))


def _assert_nonempty_list(data: list) -> None:
    assert isinstance(data, list)
    assert len(data) > 0


def _assert_nonempty_dict(data: dict) -> None:
    assert isinstance(data, dict)
    assert len(data) > 0


def test_search():
    data = toss.search("삼성전자")
    _show("search('삼성전자')", data)
    _assert_nonempty_list(data)


def test_info():
    data = toss.info(SAMPLE_CODE)
    _show(f"info('{SAMPLE_CODE}')", data)
    _assert_nonempty_dict(data)


def test_detail():
    data = toss.detail(SAMPLE_CODE)
    _show(f"detail('{SAMPLE_CODE}')", data)
    _assert_nonempty_dict(data)


def test_prices():
    data = toss.prices(["005930", "000660"])
    _show("prices(['005930', '000660'])", data)
    _assert_nonempty_list(data)
    assert len(data) == 2


def test_chart():
    data = toss.chart(SAMPLE_CODE, freq="day:1", count=10)
    _show(f"chart('{SAMPLE_CODE}', 'day:1', count=10)", data)
    _assert_nonempty_list(data)
    assert len(data) <= 10


def test_chart_pagination():
    data = toss.chart(SAMPLE_CODE, freq="min:1", count=500)
    _show(f"chart('{SAMPLE_CODE}', 'min:1', count=500)", data)
    _assert_nonempty_list(data)
    assert len(data) > 450


def test_ticks():
    data = toss.ticks(SAMPLE_CODE, count=10)
    _show(f"ticks('{SAMPLE_CODE}', count=10)", data)
    _assert_nonempty_list(data)
    assert len(data) <= 10
    for key in ("price", "volume", "tradeType"):
        assert key in data[0]


def test_orderbook():
    data = toss.orderbook(SAMPLE_CODE)
    _show(f"orderbook('{SAMPLE_CODE}')", data, n=10)
    _assert_nonempty_list(data)
    assert list(data[0].keys()) == ["level", "offerPrice", "offerVolume", "bidPrice", "bidVolume"]
    assert data[0]["offerPrice"] > data[0]["bidPrice"]


def test_trend():
    data = toss.trend(SAMPLE_CODE)
    _show(f"trend('{SAMPLE_CODE}')", data)
    assert isinstance(data, list)


def test_realtime_rankings():
    data = toss.realtime_rankings()
    _show("realtime_rankings()", data)
    _assert_nonempty_list(data)


def test_rankings():
    data = toss.rankings()
    _show("rankings()", data)
    _assert_nonempty_list(data)


def test_exchange_rates():
    data = toss.exchange_rates()
    _show("exchange_rates()", data)
    _assert_nonempty_list(data)


def test_events():
    data = toss.events()
    _show("events()", data)
    assert isinstance(data, list)


def test_hours():
    data = toss.hours()
    _show("hours()", data)
    _assert_nonempty_dict(data)


def test_ai_signals():
    data = toss.ai_signals(["005930", "000660"])
    _show("ai_signals(['005930', '000660'])", data)
    assert isinstance(data, list)


def test_news():
    data = toss.news("ALL_HIGHLIGHT")
    _show("news('ALL_HIGHLIGHT')", data)
    _assert_nonempty_list(data)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s", "-v"]))

#!/usr/bin/env python3
"""toss CLI — toss_utils 래퍼."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, List

import toss_utils as api

NEWS_ALIASES = {
    "all": "ALL_HIGHLIGHT",
    "highlight": "ALL_HIGHLIGHT",
    "all_highlight": "ALL_HIGHLIGHT",
    "hot": "HOT",
    "soaring": "SOARING_STOCK",
    "soaring_stock": "SOARING_STOCK",
    "hold": "PERSONALIZE_HOLD",
    "watch": "PERSONALIZE_WATCH",
}


def _resolve_news_type(value: str) -> str:
    key = value.strip().lower()
    if key in NEWS_ALIASES:
        return NEWS_ALIASES[key]
    return value.upper()


def _to_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        if not data:
            return []
        if all(not isinstance(v, (dict, list)) for v in data.values()):
            return [data]
        return [data]
    return [{"value": data}]


def format_output(data: Any, output: str) -> str:
    if output == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)

    rows = _to_rows(data)

    if output == "md":
        if not rows:
            return "(empty)"
        keys = list(dict.fromkeys(k for row in rows for k in row))
        header = "| " + " | ".join(keys) + " |"
        sep = "| " + " | ".join("---" for _ in keys) + " |"
        body = [
            "| " + " | ".join(_cell(row.get(k)) for k in keys) + " |"
            for row in rows
        ]
        return "\n".join([header, sep, *body])

    if output == "df":
        try:
            import pandas as pd
        except ImportError as exc:
            raise SystemExit(
                "--output df 는 pandas 가 필요합니다: pip install pandas"
            ) from exc

        if not rows:
            return "(empty)"
        if len(rows) == 1 and isinstance(data, dict) and len(data) <= 20:
            frame = pd.DataFrame(list(data.items()), columns=["key", "value"])
        else:
            frame = pd.json_normalize(rows)
        with pd.option_context("display.max_columns", None, "display.width", 200):
            return frame.to_string(index=False)

    raise ValueError(f"unsupported output: {output}")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", " ")


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "-o",
        choices=["json", "md", "df"],
        default="json",
        help="출력 형식 (기본: json)",
    )


def _run(handler: Callable[..., Any], args: argparse.Namespace) -> int:
    try:
        data = handler(args)
        print(format_output(data, args.output))
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI top-level error surface
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    output_parent = argparse.ArgumentParser(add_help=False)
    _add_output(output_parent)

    parser = argparse.ArgumentParser(prog="toss", description="토스증권 API CLI")
    _add_output(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[output_parent], **kwargs)

    p = add("search", help="종목 검색")
    p.add_argument("query", help="검색어")
    p.set_defaults(handler=lambda a: api.search(a.query))

    p = add("info", help="종목 메타데이터")
    p.add_argument("code", help="종목 코드")
    p.set_defaults(handler=lambda a: api.info(a.code))

    p = add("detail", help="종목 상세 UI 정보")
    p.add_argument("code", help="종목 코드")
    p.set_defaults(handler=lambda a: api.detail(a.code))

    p = add("prices", help="현재가 스냅샷")
    p.add_argument("codes", nargs="+", help="종목 코드 (복수 가능)")
    p.set_defaults(handler=lambda a: api.prices(a.codes))

    p = add("chart", help="캔들 차트")
    p.add_argument("code", help="종목 코드")
    p.add_argument("--freq", default="min:10", help="차트 주기 (예: min:1, day:1)")
    p.add_argument("--type", dest="s_type", default="kr-s", help="kr-s | us-s")
    p.add_argument("--count", type=int, default=450, help="캔들 개수")
    p.add_argument("--from", dest="from_dt", default=None, help="기준 시각 ISO 8601")
    p.set_defaults(
        handler=lambda a: api.chart(
            a.code, freq=a.freq, s_type=a.s_type, count=a.count, from_dt=a.from_dt
        )
    )

    p = add("ticks", help="체결(틱) 내역")
    p.add_argument("code", help="종목 코드")
    p.add_argument("--count", type=int, default=450, help="체결 개수 (1~450)")
    p.set_defaults(handler=lambda a: api.ticks(a.code, count=a.count))

    p = add("orderbook", help="호가")
    p.add_argument("code", help="종목 코드")
    p.set_defaults(handler=lambda a: api.orderbook(a.code))

    p = add("trend", help="투자자별 순매수 추이")
    p.add_argument("code", help="종목 코드")
    p.add_argument("--date", default=None, help="기준일 YYYY-MM-DD")
    p.set_defaults(handler=lambda a: api.trend(a.code, date=a.date))

    p = add("realtime-rankings", help="실시간 인기 종목")
    p.set_defaults(handler=lambda a: api.realtime_rankings())

    p = add("rankings", help="시장 랭킹")
    p.add_argument("--id", dest="ranking_id", default="biggest_market_amount")
    p.add_argument("--duration", default="realtime")
    p.add_argument("--tag", default="kr")
    p.set_defaults(
        handler=lambda a: api.rankings(
            ranking_id=a.ranking_id, duration=a.duration, tag=a.tag
        )
    )

    p = add("exchange-rates", help="환율")
    p.set_defaults(handler=lambda a: api.exchange_rates())

    p = add("events", help="경제 캘린더")
    p.set_defaults(handler=lambda a: api.events())

    p = add("hours", help="거래 시간")
    p.set_defaults(handler=lambda a: api.hours())

    p = add("ai-signals", help="AI 시그널")
    p.add_argument("codes", nargs="+", help="종목 코드 (복수 가능)")
    p.set_defaults(handler=lambda a: api.ai_signals(a.codes))

    p = add("news", help="뉴스")
    p.add_argument(
        "type",
        nargs="?",
        default="all",
        help="all | hot | soaring (기본: all)",
    )
    p.set_defaults(handler=lambda a: api.news(_resolve_news_type(a.type)))

    return parser


def main(argv: List[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _run(args.handler, args)


if __name__ == "__main__":
    raise SystemExit(main())

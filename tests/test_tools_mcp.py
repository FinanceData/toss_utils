"""에이전트 도구(tools.py)와 MCP 서버 테스트 — 가짜 서버만 사용, 실제 주문 없음."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from toss_utils.safety import Policy
from toss_utils.tools import build_tools

from .test_trader import env  # noqa: F401  (가짜 서버 fixture 재사용)


def _tools(c):
    return {t.name: t for t in build_tools(lambda: c)}


def test_no_submit_or_policy_tools_exposed(env):  # noqa: F811
    f, st, c, home = env
    names = set(_tools(c))
    assert not any(n.startswith("submit") or "policy_set" in n or "unkill" in n or "refresh" in n for n in names)
    kinds = {t.kind for t in _tools(c).values()}
    assert kinds == {"read", "prepare", "cancel"}


def test_prepare_tool_never_sends_and_tells_user_to_submit(env):  # noqa: F811
    f, st, c, home = env
    out = _tools(c)["prepare_order"].fn(symbol="005930", side="BUY", quantity="1", price="60000")
    assert out["ok"] and st["posted"] == []
    assert out["result"]["confirm_id"] in out["result"]["next_step_for_user"]
    assert "toss order submit" in out["result"]["next_step_for_user"]


def test_prepare_tool_returns_safety_failure_as_data(env):  # noqa: F811
    f, st, c, home = env
    out = _tools(c)["prepare_order"].fn(symbol="005930", side="BUY", quantity="5", price="60000")  # 30만 > 10만
    assert not out["ok"] and out["error"]["type"] == "safety"
    assert {x["name"] for x in out["error"]["failed"]} == {"max_order_krw"}
    out = _tools(c)["prepare_order"].fn(symbol="005930", side="BUY", quantity="1")                 # 가격 없음
    assert not out["ok"] and out["error"]["type"] == "invalid-argument"


def test_cancel_tools_blocked_until_policy_allows(env):  # noqa: F811
    f, st, c, home = env
    out = _tools(c)["cancel_order"].fn(order_id="o-9")
    assert not out["ok"] and "agent_can_cancel" in out["error"]["message"]
    Policy(agent_can_cancel=True).save(home)
    out = _tools(c)["cancel_order"].fn(order_id="o-9")
    assert out["ok"] and out["result"]["latestOrderId"] == "o-11"


def test_conditional_prepare_tool(env):  # noqa: F811
    f, st, c, home = env
    out = _tools(c)["prepare_conditional"].fn(symbol="005930", type="OCO", quantity="1",
                                               first_side="SELL", first_trigger="80000", first_price="80000",
                                               second_side="SELL", second_trigger="65000", second_price="64900")
    assert out["ok"], out
    assert out["result"]["body"]["second"] == {"orderSide": "SELL", "triggerPrice": "65000", "orderPrice": "64900"}
    assert st["posted"] == []


# ---------------------------------------------------------------- MCP 서버 (mcp 패키지가 있을 때만)
needs_mcp = pytest.mark.skipif(__import__("importlib").util.find_spec("mcp") is None, reason="mcp 미설치")


@needs_mcp
def test_mcp_server_lists_tools_with_schemas_and_hints(env):  # noqa: F811
    from toss_utils.mcp_server import build_server
    f, st, c, home = env
    s = build_server(lambda: c)
    tools = {t.name: t for t in asyncio.run(s.list_tools())}
    assert "prepare_order" in tools and "track_order" in tools and len(tools) == len(_tools(c))
    assert tools["get_quotes"].annotations.read_only_hint is True
    assert tools["cancel_order"].annotations.destructive_hint is True
    schema = tools["prepare_order"].input_schema
    assert schema["properties"]["side"]["enum"] == ["BUY", "SELL"] and set(schema["required"]) == {"symbol", "side"}


@needs_mcp
def test_mcp_call_tool_roundtrip(env):  # noqa: F811
    from toss_utils.mcp_server import build_server
    f, st, c, home = env
    s = build_server(lambda: c)
    r = asyncio.run(s.call_tool("get_quotes", {"symbols": ["005930"]}))
    data = json.loads(r.content[0].text)
    assert data["ok"] and data["result"][0]["lastPrice"] == "70000"


@needs_mcp
def test_stdio_server_stdout_is_pure_jsonrpc(tmp_path):
    """실제 프로세스로 띄워 initialize·tools/list 를 보내고, stdout 의 모든 줄이 JSON-RPC 인지 확인."""
    envv = {**os.environ, "TOSS_HOME": str(tmp_path), "TOSS_CLIENT_ID": "x", "TOSS_CLIENT_SECRET": "y"}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    p = subprocess.Popen([sys.executable, "-m", "toss_utils.mcp_server"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=envv, cwd=tmp_path, text=True)
    try:
        for m in msgs:
            p.stdin.write(json.dumps(m) + "\n")
            p.stdin.flush()
        lines = []
        while len([x for x in lines if x.get("id") == 2]) == 0:
            line = p.stdout.readline()
            assert line, p.stderr.read()
            lines.append(json.loads(line))       # JSON 이 아니면 여기서 실패 → stdout 오염
    finally:
        p.kill()
    listed = next(x for x in lines if x.get("id") == 2)["result"]["tools"]
    names = {t["name"] for t in listed}
    assert "prepare_order" in names and not any(n.startswith("submit") for n in names)

"""`toss-mcp`: toss_utils 도구를 MCP(stdio) 서버로 노출한다.

    claude mcp add toss_utils -- toss-mcp

- 도구 정의는 tools.py 하나에서 온다 (CLI 는 그대로, 같은 라이브러리·~/.config/toss 설정을 공유).
- 주문 전송 도구는 없다. 에이전트는 prepare_* 까지만, 전송은 사람이 `toss order submit <id>`.
- stdio 방식이라 stdout 은 MCP 프로토콜 전용이다. 이 패키지는 stdout 에 아무것도 출력하지 않는다.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

from . import __version__
from .client import TossClient
from .config import load_dotenv, toss_home
from .tools import INSTRUCTIONS, build_tools


def _lazy_client(factory: Callable[[], TossClient] | None) -> Callable[[], TossClient]:
    """첫 도구 호출 때 클라이언트를 만든다 (키가 없어도 서버는 뜨고, 호출 시 config 오류를 돌려줌)."""
    holder: dict = {}
    lock = threading.Lock()

    def get() -> TossClient:
        with lock:
            if "c" not in holder:
                holder["c"] = (factory or TossClient.from_env)()
            return holder["c"]
    return get


def build_server(client_factory: Callable[[], TossClient] | None = None):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(name="toss_utils", title="토스증권", version=__version__, instructions=INSTRUCTIONS)
    hints = {
        "read": ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
        "prepare": ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                   open_world_hint=False),
        "cancel": ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    }
    for spec in build_tools(_lazy_client(client_factory)):
        server.add_tool(spec.fn, name=spec.name, description=spec.description, annotations=hints[spec.kind])
    return server


def main() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("MCP 패키지가 없습니다. 설치: uv tool install --editable '.[mcp]' --force", file=sys.stderr)
        sys.exit(1)
    load_dotenv(".env")
    load_dotenv(toss_home() / ".env")
    server = build_server()
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)   # 요청마다 찍히는 INFO 로그(stderr) 억제
    server.run("stdio")


if __name__ == "__main__":
    main()

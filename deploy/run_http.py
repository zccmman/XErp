"""容器内 HTTP MCP 服务入口（compose mcp 服务使用）。

挂载端点: http://<host>:8000/mcp  （fastmcp streamable HTTP）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp-server"))

from ledgeros_mcp.server import build_server  # noqa: E402


def main() -> None:
    url = os.environ.get("LEDGEROS_DB")
    if not url:
        raise SystemExit("LEDGEROS_DB 未配置（应为 postgresql+psycopg://…）")
    server = build_server(url)
    server.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()

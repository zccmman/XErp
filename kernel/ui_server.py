"""A2UI 演示服务器（P1-05 阶段 2）：webapp + dist2（React+A2UI 构建）挂载。

用法: python -m kernel.ui_server   （默认 http://127.0.0.1:8002/ui/）
背景：kernel/webapp.py 被外部进程锁定期间，用独立启动器先行挂载新构建。
待锁释放后可将挂载合并回 build_app。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from kernel.webapp import build_app

_REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    app = build_app(os.environ.get("XERP_DB") or os.environ.get("LEDGEROS_DB"))
    dist = _REPO_ROOT / "web" / "dist2"
    if not dist.exists():
        raise SystemExit("web/dist2 不存在——先执行 npm run build -- --outDir dist2")
    app.mount("/ui", StaticFiles(directory=str(dist), html=True), name="ui")

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8002")))


if __name__ == "__main__":
    main()

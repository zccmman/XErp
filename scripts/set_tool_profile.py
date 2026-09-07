"""把工具分层写进 WorkBuddy 的 mcp.json（改配置，不改代码）。

    python scripts/set_tool_profile.py minimal          # 切到极简档
    python scripts/set_tool_profile.py standard --dry-run
    python scripts/set_tool_profile.py pro --mcp-json ./mcp.json
    python scripts/set_tool_profile.py --list           # 看三档各含多少工具

为什么是写 mcp.json 而不是改内核：分层是**暴露面**问题，不是能力问题。
内核保持一份，客户要哪一档只是配置不同，升级互不干扰。

安全约束：
- 只动目标 server 的 disabledTools 字段，其余 server 与字段原样保留
- 写前备份 mcp.json.bak-<时间戳>
- 原子写（临时文件 + os.replace），中断不留下半截文件
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from xerp_mcp.profiles import PROFILES, disabled_for, enabled_for, known_tools  # noqa: E402

DEFAULT_SERVER = "xerp"


def default_target() -> Path:
    """默认目标：~/.workbuddy/mcp.json（非 ~/.workbuddy/.mcp.json）。"""
    return Path.home() / ".workbuddy" / "mcp.json"


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"[x] 找不到 {path}；可用 --mcp-json 指定")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"[x] {path} 不是合法 JSON：{e}")


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    dst.write_bytes(path.read_bytes())
    return dst


def atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def pick_server(cfg: dict, name: str | None) -> str:
    servers = (cfg.get("mcpServers") or {}) | (cfg.get("servers") or {})
    if not servers:
        raise SystemExit("[x] mcp.json 里没有任何 server，请先接入 XErp 连接器")
    if name:
        if name not in servers:
            raise SystemExit(
                f"[x] 找不到 server {name!r}；已有：" + "、".join(sorted(servers))
            )
        return name
    if DEFAULT_SERVER in servers:
        return DEFAULT_SERVER
    if len(servers) == 1:
        return next(iter(servers))
    raise SystemExit(
        "[x] 有多个 server，请用 --server 指定；已有：" + "、".join(sorted(servers))
    )


def runtime_tools() -> list[str]:
    """内省真实注册的工具有哪些——静态清单可能与代码漂移，以运行时为准。"""
    from xerp_mcp.server import build_server

    async def inner():
        return [t.name for t in await build_server().list_tools()]

    return asyncio.run(inner())


def main() -> int:
    ap = argparse.ArgumentParser(description="设置 XErp 的 MCP 工具档位")
    ap.add_argument("profile", nargs="?", choices=sorted(PROFILES), help="档位名")
    ap.add_argument("--mcp-json", type=Path, default=None, help="目标 mcp.json 路径")
    ap.add_argument("--server", default=None, help="server 名（默认 xerp）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容")
    ap.add_argument("--list", action="store_true", help="列出三档工具清单")
    args = ap.parse_args()

    if args.list or not args.profile:
        print("档位一览：")
        for k in ("minimal", "standard", "pro"):
            print(f"  {k:9s} 启用 {len(enabled_for(k)):2d} 个 —— {PROFILES[k]}")
        if not args.profile:
            print("\n用 python scripts/set_tool_profile.py <minimal|standard|pro> 切换")
        else:
            for k in ("minimal", "standard", "pro"):
                print(f"\n--- {k} 启用清单 ---")
                print("  " + " ".join(enabled_for(k)))
        return 0

    target = args.mcp_json or default_target()
    cfg = load_config(target)
    srv = pick_server(cfg, args.server)

    try:
        all_tools = runtime_tools()
    except Exception as e:  # 内省失败不阻塞：退化为静态清单
        print(f"[!] 无法内省运行时工具（{e}），改用静态清单")
        all_tools = list(known_tools())

    disabled = disabled_for(args.profile, all_tools)
    enabled = [t for t in all_tools if t not in set(disabled)]

    print(f"目标文件：{target}")
    print(f"server   ：{srv}")
    print(f"档位     ：{args.profile}（{PROFILES[args.profile]}）")
    print(f"启用 {len(enabled)} / 禁用 {len(disabled)} / 全集 {len(all_tools)}")

    if args.dry_run:
        print("\n[dry-run] disabledTools =")
        print(json.dumps(disabled, ensure_ascii=False, indent=2))
        return 0

    bak = backup(target)
    cfg.setdefault("mcpServers", {}).setdefault(srv, {})
    # pro 档不裁剪 → 移除该字段而不是留空数组，避免旧版本客户端解析歧义
    if disabled:
        cfg["mcpServers"][srv]["disabledTools"] = disabled
    else:
        cfg["mcpServers"][srv].pop("disabledTools", None)
    atomic_write(target, cfg)

    print(f"\n✅ 已写入{'（备份 ' + bak.name + '）' if bak else ''}")
    print("   重开会话后生效：AI 只会看到启用清单里的工具")
    print("   改回全量：python scripts/set_tool_profile.py pro")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

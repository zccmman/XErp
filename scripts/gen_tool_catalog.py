#!/usr/bin/env python3
"""校验 / 修正 / 同步 XErp 工具清单（tool-catalog.md）与 profiles.py 的一致性。

为什么存在：
    tool-catalog.md 是手工维护的「工具地图」，历史上曾滞后内核（漏 8 个工具、
    计数错写），导致对外交付物与 108 工具内核不一致。本脚本把 profiles.py
    作为唯一真源，自动发现并修正漂移，并作为 CI 门禁拦住回归。

用法：
    python scripts/gen_tool_catalog.py check            # 校验（CI 门禁；漂移则 exit 1）
    python scripts/gen_tool_catalog.py fix              # 原地修正计数（仅数字，不动描述）
    python scripts/gen_tool_catalog.py sync             # 把仓库 catalog 同步到已装技能 + 客户包

退出码：check 发现漂移 → 1；否则 0。fix / sync 始终 0（同步是幂等的）。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from xerp_mcp.profiles import (  # noqa: E402
    MINIMAL,
    PRO_ONLY,
    STANDARD_EXTRA,
    enabled_for,
    known_tools,
)

# 唯一真源计数
N_MIN = len(MINIMAL)
N_STD_EXTRA = len(STANDARD_EXTRA)
N_STD = len(enabled_for("standard"))
N_PRO_ONLY = len(PRO_ONLY)
N_PRO = len(enabled_for("pro"))
N_TOTAL = len(known_tools())

DEFAULT_CATALOG = ROOT / "skills" / "references" / "tool-catalog.md"
INSTALLED_CATALOG = (
    Path.home() / ".workbuddy" / "skills" / "xerp" / "references" / "tool-catalog.md"
)
CUSTOMER_CATALOG = (
    ROOT.parent
    / "xerp-customer-pack"
    / "config"
    / "skills"
    / "xerp"
    / "references"
    / "tool-catalog.md"
)


# ---------------------------------------------------------------- 解析


def _sections(text: str) -> dict[str, str]:
    """按 `## ` 切分，返回 档位关键词 → 该段正文。"""
    out: dict[str, str] = {}
    parts = re.split(r"(?m)^## ", text)
    for seg in parts[1:]:  # 跳过 preamble
        head = seg.splitlines()[0]
        if "MINIMAL" in head:
            out["minimal"] = seg
        elif "STANDARD_EXTRA" in head:
            out["standard_extra"] = seg
        elif "PRO_ONLY" in head:
            out["pro_only"] = seg
    return out


def _tool_tokens(text: str) -> set[str]:
    """全文里所有形如 `aaa_bbb` 的小写工具名 token。"""
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", text))


# ---------------------------------------------------------------- 校验


def check(catalog: Path) -> int:
    if not catalog.exists():
        print(f"[x] 找不到 catalog：{catalog}")
        return 1

    text = catalog.read_text(encoding="utf-8")
    errors: list[str] = []
    warns: list[str] = []

    # 1) 头部总数
    m = re.search(r"# XErp MCP 工具清单（(\d+) 个", text)
    if m:
        if int(m.group(1)) != N_TOTAL:
            errors.append(f"头部总数 {m.group(1)} ≠ profiles 全集 {N_TOTAL}")
    else:
        errors.append("头部未匹配到『工具清单（N 个』计数")

    # 2) 摘要表三档计数
    for tier_cn, want in (("minimal", N_MIN), ("standard", N_STD), ("pro", N_PRO)):
        m = re.search(rf"^\|\s*`{tier_cn}`\s*\S+\s*\|\s*(\d+)", text, re.M)
        if m:
            if int(m.group(1)) != want:
                errors.append(f"摘要表 `{tier_cn}` 计数 {m.group(1)} ≠ {want}")
        else:
            errors.append(f"摘要表未找到 `{tier_cn}` 计数行")

    # 3) 三段章节计数
    sec_want = {
        "minimal": (r"MINIMAL 极简（(\d+)）", N_MIN),
        "standard_extra": (r"STANDARD_EXTRA 标准（(\d+)，", N_STD_EXTRA),
        "pro_only": (r"PRO_ONLY 专业（(\d+)，", N_PRO_ONLY),
    }
    for key, (pat, want) in sec_want.items():
        m = re.search(pat, text)
        if m:
            if int(m.group(1)) != want:
                errors.append(f"章节计数 {key} {m.group(1)} ≠ {want}")
        else:
            errors.append(f"章节标题未匹配到 {key} 计数")

    # 4) 第 6 行说明里的 STANDARD_EXTRA 计数（曾错写为 57）
    m = re.search(r"MINIMAL (\d+) / STANDARD_EXTRA (\d+) / PRO_ONLY (\d+)", text)
    if m:
        if int(m.group(2)) != N_STD_EXTRA:
            errors.append(
                f"说明文字 STANDARD_EXTRA {m.group(2)} ≠ {N_STD_EXTRA}"
            )
        if int(m.group(3)) != N_PRO_ONLY:
            errors.append(f"说明文字 PRO_ONLY {m.group(3)} ≠ {N_PRO_ONLY}")
    else:
        warns.append("未匹配到『MINIMAL x / STANDARD_EXTRA y / PRO_ONLY z』说明文字")

    # 5) 成员覆盖：每个已知工具至少在全文档出现一次 backtick
    doc_tokens = _tool_tokens(text)
    missing = sorted(set(known_tools()) - doc_tokens)
    if missing:
        errors.append(f"{len(missing)} 个已知工具未出现在 catalog：{', '.join(missing)}")

    # 6) 文档里出现但不在已知集合的 token（警告，可能是别名/函数名/笔误）
    extras = sorted(doc_tokens - set(known_tools()))
    if extras:
        warns.append(f"文档出现 {len(extras)} 个非工具 token（忽略）：{', '.join(extras)}")

    # 输出
    print(f"catalog：{catalog}")
    print(f"profiles 真源：minimal={N_MIN} standard={N_STD}(+{N_STD_EXTRA}) "
          f"pro={N_PRO}(+{N_PRO_ONLY}) 全集={N_TOTAL}")
    if warns:
        for w in warns:
            print(f"  ⚠ {w}")
    if errors:
        print("── 漂移 ──")
        for e in errors:
            print(f"  [x] {e}")
        print(f"\n❌ 发现 {len(errors)} 处漂移。运行 `fix` 自动修正计数。")
        return 1
    print("\n✅ catalog 与 profiles.py 完全一致，无漂移。")
    return 0


# ---------------------------------------------------------------- 修正


def fix(catalog: Path) -> int:
    if not catalog.exists():
        print(f"[x] 找不到 catalog：{catalog}")
        return 1
    text = catalog.read_text(encoding="utf-8")

    # 头部总数
    text = re.sub(
        r"(# XErp MCP 工具清单（)\d+( 个 · 三档分层）)",
        lambda m: f"# XErp MCP 工具清单（{N_TOTAL} 个 · 三档分层）",
        text,
        count=1,
    )
    # 摘要表三档计数
    for tier_cn, want in (("minimal", N_MIN), ("standard", N_STD), ("pro", N_PRO)):
        text = re.sub(
            rf"(^\|\s*`{tier_cn}`\s*\S+\s*\|\s*)\d+",
            lambda m, w=want: f"{m.group(1)}{w}",
            text,
            count=1,
            flags=re.M,
        )
    # 三段章节计数
    text = re.sub(
        r"(MINIMAL 极简（)\d+(）)",
        lambda m: f"MINIMAL 极简（{N_MIN}）",
        text,
        count=1,
    )
    text = re.sub(
        r"(STANDARD_EXTRA 标准（)\d+(，叠加在极简之上）)",
        lambda m: f"STANDARD_EXTRA 标准（{N_STD_EXTRA}，叠加在极简之上）",
        text,
        count=1,
    )
    text = re.sub(
        r"(PRO_ONLY 专业（)\d+(，仅专业档）)",
        lambda m: f"PRO_ONLY 专业（{N_PRO_ONLY}，仅专业档）",
        text,
        count=1,
    )
    # 说明文字
    text = re.sub(
        r"MINIMAL \d+ / STANDARD_EXTRA \d+ / PRO_ONLY \d+",
        f"MINIMAL {N_MIN} / STANDARD_EXTRA {N_STD_EXTRA} / PRO_ONLY {N_PRO_ONLY}",
        text,
        count=1,
    )

    catalog.write_text(text, encoding="utf-8")
    print(f"✅ 已原地修正计数：{catalog}")
    print("   重跑 `check` 复核；随后 `sync` 推送到已装技能 + 客户包。")
    return 0


# ---------------------------------------------------------------- 同步


def sync() -> int:
    if not DEFAULT_CATALOG.exists():
        print(f"[x] 仓库 catalog 不存在：{DEFAULT_CATALOG}")
        return 1
    targets = [INSTALLED_CATALOG, CUSTOMER_CATALOG]
    for t in targets:
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DEFAULT_CATALOG, t)
            print(f"  → {t}")
        except Exception as e:  # noqa: BLE001
            print(f"  [!] 同步失败 {t}：{e}")
    print("\n⚠ 客户包 config 已更新；若已打包 exe，须重建 exe 才能把新 catalog 嵌进去：")
    print("   cd xerp-customer-pack && python -m PyInstaller xerp-installer.spec --noconfirm --clean")
    return 0


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="XErp 工具清单 校验/修正/同步")
    ap.add_argument(
        "action",
        nargs="?",
        choices=("check", "fix", "sync"),
        default="check",
        help="check=校验(默认) / fix=修正计数 / sync=同步到已装+客户包",
    )
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="catalog 路径")
    args = ap.parse_args()

    if args.action == "check":
        return check(args.catalog)
    if args.action == "fix":
        return fix(args.catalog)
    return sync()


if __name__ == "__main__":
    raise SystemExit(main())

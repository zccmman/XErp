#!/usr/bin/env python3
"""部署配置静态校验 —— 在『docker compose up 之前』拦住纸面缺陷。

背景：XErp 曾出现三类只在真实构建/启动时才暴露、而本地 pytest 完全测不到的缺陷：
  1. compose 缩进错误 → healthcheck/volumes 挂错服务，depends_on: service_healthy 直接启动失败
  2. Dockerfile COPY 漏目录 → 容器里没有入口脚本
  3. pip install 漏依赖 → import 直接 ModuleNotFoundError

本脚本把这三类做成静态断言，接入提交门槛（E2E + 密钥扫描 + gitignore + 本脚本）。

用法：
    python scripts/check_deploy.py          # 校验，失败 exit 1
    python scripts/check_deploy.py -v       # 打印详细通过项
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
PYPROJECT = ROOT / "pyproject.toml"

# 镜像内会运行的代码根目录
CODE_ROOTS = ["kernel", "mcp-server", "scripts", "deploy"]
# 项目自身模块（非第三方）
OWN_MODULES = {"kernel", "xerp_mcp", "migrations", "scripts", "deploy"}
# 仅本地/CI 运行、不会进镜像的脚本 → 其依赖不计入容器依赖校验
DEV_ONLY_FILES = {"check_deploy.py", "acceptance_regression.py"}
# import 名 -> pip 包名
IMPORT_TO_PKG = {
    "psycopg2": "psycopg",
    "lark_oapi": "lark-oapi",
    "casbin_sqlalchemy_adapter": "casbin-sqlalchemy-adapter",
}

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

failures: list[str] = []
checked = 0


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"  ❌ {msg}")


def ok(msg: str) -> None:
    global checked
    checked += 1
    if VERBOSE:
        print(f"  ✅ {msg}")


def section(title: str) -> None:
    print(f"\n【{title}】")


def require(cond: bool, msg_ok: str, msg_fail: str) -> bool:
    if cond:
        ok(msg_ok)
    else:
        fail(msg_fail)
    return cond


# ── 基础设施 ────────────────────────────────────────────────
def load_yaml(path: Path):
    try:
        import yaml
    except ImportError:
        print("  ⚠️  未安装 PyYAML，跳过 compose 校验（pip install pyyaml）")
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def parse_dockerignore() -> list[str]:
    if not DOCKERIGNORE.exists():
        return []
    return [
        ln.strip()
        for ln in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def is_build_excluded(rel: str, patterns: list[str]) -> bool:
    rel = rel.replace("\\", "/")
    for p in patterns:
        p = p.rstrip("/")
        if rel == p or rel.startswith(p + "/") or fnmatch.fnmatch(rel, p):
            return True
    return False


# ════════════════════════════════════════════════════════════
# 1. compose 依赖链校验
# ════════════════════════════════════════════════════════════
def check_compose() -> None:
    section("1. compose 依赖链与持久化")

    if not require(COMPOSE.exists(), "compose 文件存在", f"缺失 {COMPOSE}"):
        return
    doc = load_yaml(COMPOSE)
    if doc is None:
        return

    services = doc.get("services") or {}
    if not require(bool(services), "services 非空", "compose 无 services"):
        return

    # 1a. service_healthy 依赖方必须有 healthcheck
    for name, svc in services.items():
        for dep, cond in (svc.get("depends_on") or {}).items():
            if isinstance(cond, dict) and cond.get("condition") == "service_healthy":
                require(
                    bool((services.get(dep) or {}).get("healthcheck")),
                    f"{name} → {dep}(healthy) 依赖方有 healthcheck",
                    f"{name} 依赖 {dep} 的 service_healthy，但 {dep} 未定义 healthcheck → compose up 必然失败",
                )

    # 1b. 数据库类服务必须挂卷（防数据丢失）
    db_image_re = re.compile(r"(postgres|mysql|mariadb|mongo)", re.I)
    for name, svc in services.items():
        image = str(svc.get("image", ""))
        if db_image_re.search(image):
            require(
                bool(svc.get("volumes")),
                f"{name}({image.split(':')[0]}) 已挂载数据卷",
                f"{name}({image.split(':')[0]}) 未挂数据卷 → docker compose down 后数据全丢",
            )

    # 1c. 具名卷必须声明
    declared_vols = set((doc.get("volumes") or {}).keys())
    for name, svc in services.items():
        for v in svc.get("volumes") or []:
            if ":" not in str(v):
                require(
                    v in declared_vols,
                    f"{name} 使用的具名卷 {v} 已声明",
                    f"{name} 使用未声明的具名卷 {v}",
                )
                continue
            src = str(v).split(":")[0]
            # 具名卷（非相对/绝对路径）必须声明
            if src and not src.startswith((".", "/", "~")) and "/" not in src:
                require(
                    src in declared_vols,
                    f"{name} 使用的具名卷 {src} 已声明",
                    f"{name} 使用未声明的具名卷 {src}",
                )


# ════════════════════════════════════════════════════════════
# 2. 构建上下文完整性
# ════════════════════════════════════════════════════════════
def parse_copy_srcs(dockerfile: str) -> set[str]:
    srcs = set()
    for m in re.finditer(r"^\s*COPY\s+(\S+)\s+(\S+)", dockerfile, re.M):
        src = m.group(1)
        srcs.add("." if src in (".", "..") or src.startswith("..") else src.rstrip("/"))
    return srcs


def check_build_context() -> None:
    section("2. 构建上下文完整性")

    if not require(DOCKERFILE.exists(), "Dockerfile 存在", f"缺失 {DOCKERFILE}"):
        return

    df = DOCKERFILE.read_text(encoding="utf-8")
    copied = parse_copy_srcs(df)
    ignored = parse_dockerignore()

    doc = load_yaml(COMPOSE)
    services = (doc or {}).get("services") or {}

    # 2a. 每个服务 command 引用的 .py 必须既存在于仓库、又被 COPY 覆盖、且未被 .dockerignore 排除
    seen = False
    for name, svc in services.items():
        for tok in svc.get("command") or []:
            if not str(tok).endswith(".py"):
                continue
            seen = True
            covered = any(tok == c or tok.startswith(c + "/") or c == "." for c in copied)
            exists = (ROOT / tok).exists()
            excluded = is_build_excluded(tok, ignored)
            require(
                covered and exists and not excluded,
                f"{name} 入口 {tok} 在镜像内可见",
                f"{name} 的入口 {tok} "
                f"(仓库存在={exists}, COPY覆盖={covered}, 被.dockerignore排除={excluded}) "
                f"→ 容器内将 No such file",
            )
    if not seen and VERBOSE:
        print("  （无服务 command 引用 .py，跳过）")

    # 2b. 含本机绝对路径的运行时产物不得进镜像
    for prod in ["deploy/demo_state.json"]:
        if (ROOT / prod).exists() or True:
            require(
                is_build_excluded(prod, ignored),
                f"{prod} 已被 .dockerignore 排除",
                f"{prod} 含本机绝对路径，必须加入 .dockerignore 否则泄漏进镜像",
            )


# ════════════════════════════════════════════════════════════
# 3. 依赖完整性与两处一致性
# ════════════════════════════════════════════════════════════
def collect_third_party_imports() -> set[str]:
    std = set(sys.stdlib_module_names)
    found: set[str] = set()
    for root in CODE_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in ("__pycache__", ".venv", "node_modules")]
            for fn in fns:
                if not fn.endswith(".py") or fn in DEV_ONLY_FILES:
                    continue
                try:
                    src = (Path(dp) / fn).read_text(encoding="utf-8")
                except Exception:
                    continue
                for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_]\w*)", src, re.M):
                    top = m.group(1)
                    if top in std or top in OWN_MODULES:
                        continue
                    found.add(top)
    return found


def base_name(spec: str) -> str:
    return re.split(r"[<>=!\[; ]", spec)[0].strip().lower().replace("_", "-")


def check_dependencies() -> None:
    section("3. 依赖完整性与两处一致性")

    if not require(PYPROJECT.exists(), "pyproject.toml 存在", f"缺失 {PYPROJECT}"):
        return

    pj = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    deps = (pj.get("project") or {}).get("dependencies") or []
    if not require(bool(deps), "pyproject 声明了 dependencies", "pyproject 无 dependencies，依赖无真源"):
        return

    declared = {base_name(d) for d in deps}

    # 3a. 代码 import 的第三方包必须已声明
    for imp in sorted(collect_third_party_imports()):
        pkg = IMPORT_TO_PKG.get(imp, imp.lower().replace("_", "-"))
        require(
            pkg in declared or imp.lower() in declared,
            f"依赖已声明：{imp}",
            f"代码 import '{imp}'，但 pyproject 未声明（pip 包名 {pkg}）→ 容器 ModuleNotFoundError",
        )

    # 3b. Dockerfile 与 pyproject 必须一致
    if DOCKERFILE.exists():
        df = DOCKERFILE.read_text(encoding="utf-8")
        df_pkgs = {base_name(m.group(1)) for m in re.finditer(r'"([a-zA-Z0-9_\-\[\]]+)>=', df)}
        only_pj = declared - df_pkgs
        only_df = df_pkgs - declared
        require(
            not only_pj,
            "pyproject 依赖已全部同步到 Dockerfile",
            f"pyproject 有但 Dockerfile 未装：{sorted(only_pj)}",
        )
        require(
            not only_df,
            "Dockerfile 未多装未声明的依赖",
            f"Dockerfile 有但 pyproject 未声明：{sorted(only_df)}",
        )


# ════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 62)
    print("XErp 部署配置静态校验（防『纸面交付』回归）")
    print("=" * 62)

    check_compose()
    check_build_context()
    check_dependencies()

    print("\n" + "=" * 62)
    if failures:
        print(f"❌ 校验失败：{len(failures)} 项（通过 {checked} 项）")
        print("=" * 62)
        return 1
    print(f"✅ 全部通过（{checked} 项断言）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""适配器规则注册表（P2-01）。

内置规则以 JSON 放在 ``kernel/data/adapters/``，启动时自动加载；
第三方模块可在运行时用 :func:`register` 注册自己的规则——**零核心改动**的落点就在这里。

规则主键为 ``(adapter, event_type)``，重复注册同名键即覆盖（便于热更新规则版本）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kernel.adapters.spec import RuleError, validate_rule

_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "data" / "adapters"

_REGISTRY: dict[tuple[str, str], dict] = {}
_LOADED = False


def register(rule: dict) -> dict:
    """校验并注册一条规则；非法规则直接抛 :class:`RuleError`。"""
    validate_rule(rule)
    key = (rule["adapter"], rule["event_type"])
    _REGISTRY[key] = rule
    return rule


def load_builtin_rules(force: bool = False) -> int:
    """加载 ``kernel/data/adapters/*.json``，返回加载条数。"""
    global _LOADED
    if _LOADED and not force:
        return len(_REGISTRY)
    if not _BUILTIN_DIR.exists():
        _LOADED = True
        return 0
    for path in sorted(_BUILTIN_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            register(json.load(f))
    _LOADED = True
    return len(_REGISTRY)


def get_rule(adapter: str, event_type: str) -> dict | None:
    load_builtin_rules()
    return _REGISTRY.get((adapter, event_type))


def list_rules() -> list[dict[str, Any]]:
    load_builtin_rules()
    return [
        {
            "adapter": r["adapter"],
            "event_type": r["event_type"],
            "version": r["version"],
            "target_status": r.get("target_status", "PUSHED"),
            "summary": r.get("summary", ""),
            "lines": len(r["lines"]),
        }
        for r in _REGISTRY.values()
    ]


def clear() -> None:
    """清空注册表（测试用）。"""
    _REGISTRY.clear()
    global _LOADED
    _LOADED = False


class RuleNotFoundError(RuleError):
    def __init__(self, adapter: str, event_type: str):
        super().__init__(
            "RULE_NOT_FOUND",
            f"未注册的适配器规则：{adapter}/{event_type}",
            {"adapter": adapter, "event_type": event_type},
        )

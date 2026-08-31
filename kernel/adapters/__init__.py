"""事件适配器包（P2-01）：第三方业务事件 → 凭证，声明式规则、零核心改动。"""

from kernel.adapters.engine import AdapterError, ingest_event, preview
from kernel.adapters.registry import (
    RuleNotFoundError,
    clear,
    get_rule,
    list_rules,
    load_builtin_rules,
    register,
)
from kernel.adapters.spec import EventFieldError, RuleError, validate_rule

__all__ = [
    "AdapterError",
    "EventFieldError",
    "RuleError",
    "RuleNotFoundError",
    "clear",
    "get_rule",
    "ingest_event",
    "list_rules",
    "load_builtin_rules",
    "preview",
    "register",
    "validate_rule",
]

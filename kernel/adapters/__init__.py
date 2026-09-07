"""事件适配器包（P2-01）：第三方业务事件 → 凭证，声明式规则、零核心改动。

Integration ↔ Ledger Core 边界契约（D8：防腐层缺失，按债务建议「保持零核心改动
断言即可，暂不动」——不建完整 ACL，只把边界落成机器可验证断言，见
``tests/test_integration_acl.py``）：

1. **依赖方向**：外围依赖核心、核心不依赖外围。本包（Integration）只被外围模块
   消费（如 ``kernel/ocr`` 发票管线）；Ledger Core（``kernel/posting``、
   ``kernel/state``、``kernel/voucher_wizard``、``kernel/db``、``kernel/events``、
   ``kernel/seed``、``kernel/carryforward``、``kernel/reconcile``、
   ``kernel/ledgerbook``、``kernel/classic``、``kernel/ledger`` 等）**不得** import
   本包。零核心改动（P2-01）的落点正是：第三方扩展只注册声明式规则，绝不改核心。
2. **不绕过投影**：适配器**不得**调用核心内部投影累加器（``_accumulate_balances``
   等下划线前缀内部符号），也**不得**直写 ``Balance`` 投影。余额投影只由
   ``post_voucher`` 经 ``validate_voucher`` 触发，是事件流的可重建投影（ADR-002）。
3. **状态只经状态机**：适配器驱动凭证状态**只**经 ``kernel.state.transition`` 公开
   路径（``DRAFT → PUSHED → APPROVED``），不得裸写 ``voucher.status=``。
4. **已知边界泄漏（暂不修，待 P4 防腐层）**：适配器自管 ``记-`` 凭证号前缀
   （``engine._next_voucher_no`` 重复核心内部符号），接第三方时此处会脆；届时由
   防腐层委托核心 ``next_voucher_no``。该泄漏属 ACL 范畴，不在 D8 断言范围内。
"""

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

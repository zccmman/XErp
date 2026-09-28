"""F 边界纪律：把寄生架构红线落成机器可验证断言。

钉死三条此前未被单独覆盖的红线（其余见 ``test_integration_acl.py``）：
- R3 AI 只产草稿：只读智能体（audit_trail / simulation / healing / copilot）
  绝不写 Voucher / Balance、绝不走 transition / post_voucher / approve_voucher 等终态入口；
- R2 单一真源（ADR-002）：审计追踪复用 ``verify_chain``，绝不自行重算事件哈希；
- R4 制单 ≠ 审批：Web ``_apply_transition`` 二次校验「制单人不能审批自己的凭证」。

R1（核心层不反向依赖 adapters）与 R2 的适配器投影契约由 ``test_integration_acl.py`` 锁死。
"""

from __future__ import annotations

from pathlib import Path

# 只读智能体模块清单（R3 钉死对象）
_READONLY_AGENT_MODULES = (
    "kernel/reporting/audit_trail.py",
    "kernel/simulation.py",
    "kernel/healing.py",
    "kernel/copilot.py",
)

# 任一出现即视为「尝试写终态」（R3 禁止）
_FORBIDDEN_WRITE_SYMBOLS = (
    "Voucher(",
    "Balance(",
    "transition(",
    "post_voucher",
    "create_voucher",
    "approve_voucher",
    "reject_voucher",
    "cancel_post_voucher",
    "withdraw_voucher",
    "sign_voucher",
    "push_voucher",
)


def test_readonly_agents_never_mutate_state():
    """R3：审计 / 模拟 / 自愈 / Copilot 只读智能体不得出现任何终态写入入口。"""
    violations = []
    for rel in _READONLY_AGENT_MODULES:
        p = Path(rel)
        assert p.exists(), f"被测只读模块缺失：{rel}"
        text = p.read_text(encoding="utf-8")
        for sym in _FORBIDDEN_WRITE_SYMBOLS:
            if sym in text:
                violations.append(f"{rel}: 含禁止符号 {sym!r}")
    assert violations == [], (
        "只读智能体出现终态写入入口（违反 R3 AI 只产草稿）：\n"
        + "\n".join(violations)
    )


def test_audit_reuses_chain():
    """R2：审计追踪复用 kernel.ledger.chain.verify_chain，绝不自行重算哈希。"""
    text = Path("kernel/reporting/audit_trail.py").read_text(encoding="utf-8")
    assert "from kernel.ledger.chain import verify_chain" in text, (
        "audit_trail 未复用 verify_chain（违反 ADR-002 单一真源：不得另算一份哈希）"
    )
    # 反向证明：模块内不得出现自实现的哈希累加（compute_event_hash 的"自实现"迹象）
    assert "def compute_event_hash" not in text, (
        "audit_trail 自行实现了事件哈希（应与内核口径同源，违反 ADR-002）"
    )


def test_webapp_enforces_maker_not_approver():
    """R4：Web _apply_transition 必须二次校验「制单人不能审批自己的凭证」。"""
    text = Path("kernel/webapp.py").read_text(encoding="utf-8")
    assert "制单人不能审批自己的凭证" in text, (
        "webapp 缺少制单≠审批的内核守卫（违反 R4）"
    )
    # 守卫必须落在 _apply_transition 附近（函数体里 is_maker 判定 + 返回拒绝）
    assert "require_maker is False and is_maker" in text, (
        "webapp 未对「制单人尝试审批」做分支拒绝（违反 R4 二次校验）"
    )

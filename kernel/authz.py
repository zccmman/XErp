"""鉴权模块（P1-03）：Casbin RBAC（账套为 domain）+ Agent 自治额度。

角色（domain = ledger_set_id，"*" 表示全局）：
- admin       账套管理员：全部动作
- accountant  会计：记账/提交/过账/报表
- reviewer    审批人：审批/报表（审批换人由状态机另行保证）
- auditor     只读：查询/报表
- agent       智能体：记账/提交（受自治额度约束，见 check_agent_quota）

动作：ledger:manage / voucher:create / voucher:push / voucher:approve /
voucher:post / voucher:cancel / ledger:close / ledger:read
"""

from __future__ import annotations

from decimal import Decimal

import casbin
import casbin_sqlalchemy_adapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Event, Subject, utcnow
from kernel.events import E

MODEL_TEXT = """
[request_definition]
r = sub, dom, act

[policy_definition]
p = sub, dom, act

[role_definition]
g = _, _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = (r.sub == p.sub || g(r.sub, p.sub, r.dom)) \
    && (r.dom == p.dom || p.dom == "*") \
    && (r.act == p.act || p.act == "*")
"""

ROLES = {
    "admin": ["*"],
    "accountant": [
        "ledger:read", "ledger:close",
        "voucher:create", "voucher:push", "voucher:post",
    ],
    "reviewer": ["ledger:read", "voucher:approve"],
    "auditor": ["ledger:read"],
    "agent": ["ledger:read", "voucher:create", "voucher:push"],
}

# 动作继承：admin 通配；accountant 可读等由 ROLES 展开成具体策略
ROLE_POLICIES = {
    role: acts for role, acts in ROLES.items()
}


class AuthzError(PermissionError):
    """权限或额度拒绝（message 直接可展示）。"""


def get_enforcer(session: Session) -> casbin.Enforcer:
    """基于当前 session 绑定的连接创建 enforcer（casbin_rule 表自动建）。"""
    bind = session.get_bind()
    adapter = casbin_sqlalchemy_adapter.Adapter(bind)
    model = casbin.Model()
    model.load_model_from_text(MODEL_TEXT)
    return casbin.Enforcer(model, adapter)


def grant_ledger_role(session: Session, *, ledger_set_id: str, subject_id: str,
                      role: str) -> None:
    """把 subject 授予某账套下的角色（幂等）。"""
    if role not in ROLES:
        raise AuthzError(f"未知角色: {role}")
    e = get_enforcer(session)
    for act in ROLES[role]:
        e.add_policy(subject_id, ledger_set_id, act)
    session.commit()


def revoke_ledger_role(session: Session, *, ledger_set_id: str, subject_id: str,
                       role: str) -> None:
    e = get_enforcer(session)
    for act in ROLES[role]:
        e.remove_policy(subject_id, ledger_set_id, act)
    session.commit()


def enforce(session: Session, *, actor_id: str, ledger_set_id: str, action: str) -> None:
    """鉴权入口：不通过直接抛 AuthzError。"""
    e = get_enforcer(session)
    if not e.enforce(actor_id, ledger_set_id, action):
        raise AuthzError(
            f"权限不足：主体 {actor_id[:8]}… 在账套 {ledger_set_id[:8]}… 无 {action} 权限"
        )


def check_agent_quota(session: Session, *, actor_id: str,
                      voucher_amount: Decimal) -> None:
    """Agent 自治额度：type=agent 且配置了 daily_voucher_limit 时，
    校验当日（UTC，与 occurred_at 存储口径一致）该主体创建的凭证金额
    合计 + 本次金额 ≤ 上限。
    """
    subject = session.scalars(
        select(Subject).where(Subject.id == actor_id)
    ).first()
    if subject is None or subject.type != "agent":
        return
    limit = subject.daily_voucher_limit
    if limit is None:
        return
    today = utcnow().date()
    used = Decimal("0")
    for e in session.scalars(
        select(Event).where(Event.event_type == E.VOUCHER_CREATED)
    ):
        if (e.actor or {}).get("id") != actor_id:
            continue
        occurred = e.occurred_at.date() if e.occurred_at else None
        if occurred != today:
            continue
        total = (e.payload or {}).get("total_debit")
        if total:
            used += Decimal(str(total))
    if used + voucher_amount > Decimal(str(limit)):
        raise AuthzError(
            f"自治额度不足：当日已用 {used}，本次 {voucher_amount}，"
            f"上限 {limit}（需人工审批或调整额度）"
        )

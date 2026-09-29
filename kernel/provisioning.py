"""应用模板 · 一键开通个人账套（C · 寄生 WB 平台，内核层）。

零 WorkBuddy 依赖：``external_ref`` / ``display_name`` 由调用方传入
（WB 云会话的 wb_uid / 本地 Web 会话的登录主体 / CLI 的 --owner-ref）。
内核只做确定性建账，复用既有内核原语（COA 导入 / Period / Subject / 授权 / 外部键绑定），
不复制任何配平或取数逻辑（ADR-002 单一真源）。

幂等：同一 ``external_ref``（一个外部身份）再次调用直接返回既有绑定，绝不重复建账套。

起步模板（产品化核心）：不同个人场景选不同「开账档案」，复用同一套小企业准则科目，
但给出贴合的命名、准则与开账后引导清单（个体户 / 小微企业 / 一人公司 / 非营利）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.approval import bind_subject_external_ref
from kernel.authz import get_enforcer, grant_ledger_role, list_role_members
from kernel.coa import CoaImportError, import_chart_of_accounts, load_template_rows
from kernel.db.models import LedgerSet, Period, Subject

# 个人账套名称前缀（工作区隔离：仅展示「我的账套 · <姓名>」与公开演示账套）
PERSONAL_LEDGER_PREFIX = "我的账套 · "
REVIEWER_NAME = "审批人"


@dataclass(frozen=True)
class TemplateProfile:
    """起步模板：决定开账档案的命名 / 准则 / 引导清单（科目表当前统一复用小企业准则）。"""

    key: str
    label: str
    accounting_standard: str
    description: str
    onboarding: tuple[str, ...]


TEMPLATES: dict[str, TemplateProfile] = {
    "small_business": TemplateProfile(
        key="small_business",
        label="小微企业（会计准则）",
        accounting_standard="small_business",
        description="适用有限责任公司 / 小微企业，内置小企业会计准则 144 科目，支持存货、固定资产、成本核算。",
        onboarding=(
            "录入期初余额（借贷必须平衡，期初是存量不是发生额）",
            "确认审批人（默认已建「审批人」身份，制单≠审批）",
            "日常记账：一句话制单 → 推送审批 → 过账",
            "月末结账：四道闸门全绿才放行",
        ),
    ),
    "individual": TemplateProfile(
        key="individual",
        label="个体工商户（简易账）",
        accounting_standard="small_business",
        description="适用个体户 / 个人工作室，复用同一套科目但走更轻的开账与申报节奏。",
        onboarding=(
            "若开业首月无期初，可直接跳过录期初",
            "按收支记流水（收入 / 费用为主）",
            "关注季度经营情况，按需导出对账单",
        ),
    ),
    "sole_proprietor_ltd": TemplateProfile(
        key="sole_proprietor_ltd",
        label="一人有限公司",
        accounting_standard="small_business",
        description="适用一人有限责任公司，强调公私账分离与独立核算。",
        onboarding=(
            "录入期初，区分股东投入与经营资产",
            "严格公私账分离，报销走制单审批",
            "设审批人，月末结账",
        ),
    ),
    "nonprofit": TemplateProfile(
        key="nonprofit",
        label="非营利 / 社团",
        accounting_standard="small_business",
        description="适用社团 / 非营利组织，强调限定用途与理事审批。",
        onboarding=(
            "录入期初（限定性资产单独核算）",
            "理事审批（默认已建「审批人」身份）",
            "按项目归集收支",
        ),
    ),
}

DEFAULT_TEMPLATE = "small_business"


def list_templates() -> list[dict]:
    """返回所有起步模板（供 CLI / Web / Skill 展示选择）。"""
    return [
        {
            "key": t.key,
            "label": t.label,
            "accounting_standard": t.accounting_standard,
            "description": t.description,
            "onboarding": list(t.onboarding),
        }
        for t in TEMPLATES.values()
    ]


def get_template(key: Optional[str]) -> TemplateProfile:
    """按 key 取模板，未知/空回退默认；绝不抛错（产品化要兜底）。"""
    return TEMPLATES.get((key or "").strip(), TEMPLATES[DEFAULT_TEMPLATE])


def owned_ledger_for_subject(session: Session, subject_id: str) -> Optional[str]:
    """查该主体作为 admin 拥有的账套（多租户隔离：一个外部身份一个主账套）。

    直接读 Casbin 策略：admin 角色授权为 ``(subject, ledger_set_id, "*")``，
    命中即该主体拥有此账套。
    """
    enforcer = get_enforcer(session)
    for _sub, dom, act in enforcer.get_filtered_policy(0, subject_id):
        if act == "*":  # admin 通配
            return dom
    return None


def reviewer_for_ledger(session: Session, ledger_set_id: str) -> Optional[str]:
    """查该账套的 reviewer 角色主体（缺省审批人回退）。"""
    members = list_role_members(session, ledger_set_id=ledger_set_id, role="reviewer")
    return members[0] if members else None


def provision_personal_ledger(
    session: Session,
    *,
    display_name: str,
    external_ref: Optional[str] = None,
    owner_subject_id: Optional[str] = None,
    ledger_name: Optional[str] = None,
    template: Optional[str] = None,
    reviewer_name: str = REVIEWER_NAME,
) -> dict:
    """开通个人账套（C · 应用模板）。返回建账结果。

    两种调用语义（单一真源，ADR-002，不复制配平/取数）：

    1. 身份联邦首次接入（``external_ref`` 给定且 ``owner_subject_id`` 为空）：
       幂等——同一 ``external_ref`` 已开通直接返回既有（is_new=False）。
    2. 已登录用户主动新建账套（``owner_subject_id`` 给定，如 Web /init 向导）：
       不复用既有账套，始终为该主体新建一套（一个主体可有多个账套），
       并把 admin 授予该现有主体。

    共同：创建个人账套（起步模板命名的 COA）+ 当期 OPEN 期间 + 默认审批人（双身份）
    + Casbin 角色（admin / accountant / reviewer），结构性保证制单≠审批。
    """
    if not external_ref and not owner_subject_id:
        raise ValueError("external_ref 与 owner_subject_id 至少提供一个")
    display_name = (display_name or "").strip() or "老板"
    tpl = get_template(template)

    # —— 语义①幂等：同一外部身份已开通 → 返回既有 ——
    if external_ref and not owner_subject_id:
        owner = session.scalars(
            select(Subject).where(Subject.external_ref == external_ref)
        ).first()
        if owner is not None:
            owned = owned_ledger_for_subject(session, owner.id)
            if owned is not None:
                return {
                    "subject_id": owner.id,
                    "ledger_set_id": owned,
                    "reviewer_subject_id": reviewer_for_ledger(session, owned),
                    "is_new": False,
                    "template": tpl.key,
                    "accounting_standard": tpl.accounting_standard,
                    "accounts_created": 0,
                    "external_ref": external_ref,
                }

    # —— 解析账套所有者主体 ——
    if owner_subject_id:
        owner = session.get(Subject, owner_subject_id)
        if owner is None:
            raise ValueError(f"owner_subject_id 不存在：{owner_subject_id}")
    elif external_ref:
        owner = session.scalars(
            select(Subject).where(Subject.external_ref == external_ref)
        ).first()
        if owner is None:
            owner = Subject(
                type="user",
                display_name=display_name,
                autonomy_level=3,
                external_ref=external_ref,
            )
            session.add(owner)
            session.flush()

    # —— 创建个人账套（起步模板 → 小企业准则 COA，空账套）——
    name = ledger_name or f"{PERSONAL_LEDGER_PREFIX}{display_name}"
    if session.scalars(select(LedgerSet).where(LedgerSet.name == name)).first():
        suffix = (external_ref or owner_subject_id or "")[:6]
        name = f"{name} ({suffix})"
    ls = LedgerSet(name=name, accounting_standard=tpl.accounting_standard)
    session.add(ls)
    session.flush()
    try:
        coa_stats = import_chart_of_accounts(session, ls.id, load_template_rows())
    except CoaImportError as e:  # pragma: no cover - 模板数据受控
        session.rollback()
        raise RuntimeError(f"科目导入失败：{e}") from e
    today = date.today()
    session.add(
        Period(ledger_set_id=ls.id, year=today.year, month=today.month, status="OPEN")
    )

    # —— 默认审批人（双身份，结构性保证 maker≠approver）——
    reviewer = session.scalars(
        select(Subject).where(Subject.display_name == reviewer_name, Subject.type == "user")
    ).first()
    if reviewer is None:
        reviewer = Subject(type="user", display_name=reviewer_name, autonomy_level=3)
        session.add(reviewer)
        session.flush()

    # SQLite：先落盘主体再授权（防 casbin 自锁）
    session.commit()
    grant_ledger_role(session, ledger_set_id=ls.id, subject_id=owner.id, role="admin")
    grant_ledger_role(session, ledger_set_id=ls.id, subject_id=reviewer.id, role="accountant")
    grant_ledger_role(session, ledger_set_id=ls.id, subject_id=reviewer.id, role="reviewer")
    return {
        "subject_id": owner.id,
        "ledger_set_id": ls.id,
        "reviewer_subject_id": reviewer.id,
        "is_new": True,
        "template": tpl.key,
        "accounting_standard": tpl.accounting_standard,
        "accounts_created": coa_stats.get("created", 0),
        "external_ref": external_ref,
    }


def bind_reviewer_external_ref(
    session: Session, *, ledger_set_id: str, external_ref: str
) -> None:
    """把外部身份键（WB user id / 飞书 open_id 等）补绑到该账套的默认审批人。

    仅作解析键（见 Subject.external_ref 注释），不参与授权判定。
    """
    if not external_ref:
        return
    rid = reviewer_for_ledger(session, ledger_set_id)
    if rid:
        bind_subject_external_ref(session, subject_id=rid, external_ref=external_ref)

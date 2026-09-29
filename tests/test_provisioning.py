"""钉死「一键开通个人账套」产品化能力（P2 后续 · C 应用模板）。

覆盖：
- 幂等开通（同一 external_ref 重复调用不重复建账套）
- 多租户隔离（不同 external_ref → 不同账套）
- 双身份 + HITL 红线（owner=admin，reviewer 独立，制单≠审批）
- 起步模板（4 模板 + 未知 key 兜底默认）
- list_role_members 按动作集合反查 p 策略（修复 get_users_for_role_in_domain 恒空 bug）
- 零 WorkBuddy 依赖（L0 逃生舱红线）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.authz import list_role_members
from kernel.db.base import Base
from kernel.migrate import ensure_schema_current
from kernel.provisioning import (
    DEFAULT_TEMPLATE,
    PERSONAL_LEDGER_PREFIX,
    TEMPLATES,
    get_template,
    list_templates,
    owned_ledger_for_subject,
    provision_personal_ledger,
    reviewer_for_ledger,
)

_MODULE = Path(__file__).resolve().parents[1] / "kernel" / "provisioning.py"


@pytest.fixture()
def engine(tmp_path: Path):
    # 用临时文件库：ensure_schema_current 与 SQLAlchemy Session 必须共享同一物理库
    # （内存库 ``sqlite://`` 每连接独立，迁移建表后引擎看不到表）。
    db_file = tmp_path / "ledger.db"
    url = "sqlite:///" + str(db_file).replace("\\", "/")
    ensure_schema_current(url)
    return create_engine(url)


def test_provision_creates_coa_period_dual_identity(engine) -> None:
    with Session(engine) as s:
        r = provision_personal_ledger(
            s, external_ref="wb:userA", display_name="张三", template="small_business"
        )
        s.commit()
        assert r["is_new"] is True
        assert r["accounts_created"] > 0  # 小企业准则 144 科目已导入
        assert r["template"] == "small_business"
        # 双身份齐全
        admins = list_role_members(s, ledger_set_id=r["ledger_set_id"], role="admin")
        reviewers = list_role_members(s, ledger_set_id=r["ledger_set_id"], role="reviewer")
        assert r["subject_id"] in admins
        assert r["reviewer_subject_id"] in reviewers
        # HITL 红线：制单人 ≠ 审批人
        assert r["subject_id"] not in reviewers


def test_provision_idempotent_same_external_ref(engine) -> None:
    with Session(engine) as s:
        r1 = provision_personal_ledger(s, external_ref="wb:userA", display_name="张三")
        s.commit()
        ls1 = r1["ledger_set_id"]
        r2 = provision_personal_ledger(s, external_ref="wb:userA", display_name="张三")
        assert r2["is_new"] is False
        assert r2["ledger_set_id"] == ls1
        assert r2["accounts_created"] == 0  # 不重复导入科目
        # owner 与 reviewer 键稳定
        assert r2["subject_id"] == r1["subject_id"]
        assert r2["reviewer_subject_id"] == r1["reviewer_subject_id"]


def test_provision_multi_tenant_isolation(engine) -> None:
    with Session(engine) as s:
        a = provision_personal_ledger(s, external_ref="wb:A", display_name="甲")
        b = provision_personal_ledger(s, external_ref="wb:B", display_name="乙", template="individual")
        s.commit()
        assert a["ledger_set_id"] != b["ledger_set_id"]
        assert a["subject_id"] != b["subject_id"]
        # 各自拥有的账套互不可见
        assert owned_ledger_for_subject(s, a["subject_id"]) == a["ledger_set_id"]
        assert owned_ledger_for_subject(s, b["subject_id"]) == b["ledger_set_id"]
        assert owned_ledger_for_subject(s, a["subject_id"]) != b["ledger_set_id"]
        # 模板被尊重
        assert b["template"] == "individual"
        # reviewer_for_ledger 各自正确
        assert reviewer_for_ledger(s, a["ledger_set_id"]) == a["reviewer_subject_id"]
        assert reviewer_for_ledger(s, b["ledger_set_id"]) == b["reviewer_subject_id"]


def test_reviewer_is_also_accountant(engine) -> None:
    """默认审批人同时授予 accountant（可制单草稿）+ reviewer（可审批），owner 仅 admin。"""
    with Session(engine) as s:
        r = provision_personal_ledger(s, external_ref="wb:userC", display_name="丙")
        s.commit()
        ls = r["ledger_set_id"]
        accountants = list_role_members(s, ledger_set_id=ls, role="accountant")
        admins = list_role_members(s, ledger_set_id=ls, role="admin")
        assert r["reviewer_subject_id"] in accountants
        assert r["reviewer_subject_id"] not in admins  # 审批人不可自我 admin 越权


def test_template_fallback_unknown_key(engine) -> None:
    with Session(engine) as s:
        r = provision_personal_ledger(s, external_ref="wb:userD", display_name="丁", template="nope_unknown")
        s.commit()
        assert r["template"] == DEFAULT_TEMPLATE == "small_business"


def test_list_templates_four_and_default() -> None:
    tpls = list_templates()
    assert len(tpls) == 4
    keys = {t["key"] for t in tpls}
    assert keys == set(TEMPLATES.keys())
    assert get_template(None).key == DEFAULT_TEMPLATE
    assert get_template("nonprofit").key == "nonprofit"
    # 所有模板统一复用小企业准则 COA（产品化：命名/准则/引导不同，科目同一套）
    assert all(t["accounting_standard"] == "small_business" for t in tpls)


def test_personal_ledger_name_prefix(engine) -> None:
    with Session(engine) as s:
        r = provision_personal_ledger(s, external_ref="wb:userE", display_name="戊")
        s.commit()
        # 默认账套名含个人账套前缀
        from kernel.db.models import LedgerSet

        ls = s.get(LedgerSet, r["ledger_set_id"])
        assert ls.name.startswith(PERSONAL_LEDGER_PREFIX)


def test_ledger_name_collision_unique_suffix(engine) -> None:
    """同名（同一 display_name 默认账套名）需加后缀避免 LedgerSet 唯一冲突。"""
    with Session(engine) as s:
        r1 = provision_personal_ledger(s, external_ref="wb:x1", display_name="同名人")
        r2 = provision_personal_ledger(s, external_ref="wb:x2", display_name="同名人")
        s.commit()
        assert r1["ledger_set_id"] != r2["ledger_set_id"]


def test_missing_external_ref_rejected(tmp_path: Path) -> None:
    db_file = tmp_path / "ledger.db"
    url = "sqlite:///" + str(db_file).replace("\\", "/")
    ensure_schema_current(url)
    with Session(create_engine(url)) as s:
        with pytest.raises(ValueError):
            provision_personal_ledger(s, external_ref="", display_name="己")


def test_no_workbuddy_dependency() -> None:
    """L0 韧性红线：内核级开通原语零宿主依赖，也不得绕进 adapters。"""
    src = _MODULE.read_text(encoding="utf-8")
    assert not re.findall(r"^\s*(?:from|import)\s+workbuddy\b", src, flags=re.MULTILINE)
    assert "from kernel.adapters" not in src


def test_list_role_members_empty_when_ungranted(engine) -> None:
    """未授权该角色的账套应返回空（修复前 get_users_for_role_in_domain 恒空，无法区分）。"""
    with Session(engine) as s:
        r = provision_personal_ledger(s, external_ref="wb:userF", display_name="庚")
        s.commit()
        # 该账套没有 owner 被授予 "unknown_role"
        assert list_role_members(s, ledger_set_id=r["ledger_set_id"], role="unknown_role") == []

"""D7 多级签字「签字位」契约测试。

锁定（见 docs/REVIEW-ontology.md D7 / kernel/signing.py）：
- 全部 required_signers 签署 approved → 自动 APPROVED；
- 任一 rejected → 凭证退回 DRAFT（VOUCHER_REJECTED）；
- 签字未齐时 approve_voucher（state.transition APPROVED）被 PENDING_SIGNATURES 拦截；
- Agent 不能签字（AGENT_APPROVAL_FORBIDDEN）；制单人不能签自己的单（NO_SELF_APPROVAL）；
- 同一签字位不可重复签署（SLOT_ALREADY_SIGNED）；同一人幂等放行；
- 传统单层凭证（required_signers 空）行为不变，仍走 approve_voucher。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.db.models import Event, Subject, Voucher
from kernel.events import E
from kernel.posting import PostingError
from kernel.seed import seed_demo_ledger
from kernel.signing import (
    SIGN_ROLE_CASHIER,
    SIGN_ROLE_MANAGER,
    pending_signers,
    sign_voucher,
    signing_status,
)
from kernel.state import transition
from kernel.voucher_wizard import create_draft_voucher

ZERO = Decimal("0.00")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def ctx(session):
    ids = seed_demo_ledger(session)
    session.flush()
    cashier = Subject(type="user", display_name="出纳小美")
    manager = Subject(type="user", display_name="主管老李")
    agent = Subject(type="agent", display_name="RPA机器人", autonomy_level=3)
    session.add_all([cashier, manager, agent])
    session.flush()
    return {
        "s": session,
        "ids": ids,
        "drafter": ids["subject_id"],  # 丞辰（user）
        "cashier": cashier.id,
        "manager": manager.id,
        "agent": agent.id,
    }


def _make(ctx, required_signers):
    s = ctx["s"]
    v, _ = create_draft_voucher(
        s,
        ledger_set_id=ctx["ids"]["ledger_set_id"],
        actor={"type": "user", "id": ctx["drafter"]},
        voucher_date=date(2026, 8, 15),
        summary="差旅费报销",
        lines=[
            {"account_code": "6602", "debit": "100.00", "credit": ""},
            {"account_code": "1001", "debit": "", "credit": "100.00"},
        ],
        required_signers=required_signers,
    )
    transition(
        s, voucher_id=v.id, actor={"type": "user", "id": ctx["drafter"]}, target="PUSHED"
    )
    s.flush()
    return v


# ---------- 多级签字 happy path ----------


def test_multilevel_cashier_then_manager_approves(ctx):
    v = _make(ctx, [SIGN_ROLE_CASHIER, SIGN_ROLE_MANAGER])
    assert v.status == "PUSHED"
    assert pending_signers(v) == ["cashier", "manager"]

    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]})
    v = ctx["s"].get(Voucher, v.id)
    assert v.status == "PUSHED"  # 出纳签完仍待主管
    assert pending_signers(v) == ["manager"]

    sign_voucher(ctx["s"], voucher_id=v.id, slot="manager",
                 actor={"type": "user", "id": ctx["manager"]})
    v = ctx["s"].get(Voucher, v.id)
    assert v.status == "APPROVED"  # 主管签完自动审批通过
    assert pending_signers(v) == []

    sig_events = ctx["s"].scalars(
        select(Event).where(Event.event_type == E.VOUCHER_SIGNED)
    ).all()
    assert len(sig_events) == 2
    assert ctx["s"].scalars(
        select(Event).where(Event.event_type == E.VOUCHER_APPROVED)
    ).first() is not None


def test_signing_status_snapshot(ctx):
    v = _make(ctx, [SIGN_ROLE_CASHIER, SIGN_ROLE_MANAGER])
    st = signing_status(v)
    assert st["is_multilevel"] is True
    assert st["required"] == ["cashier", "manager"]
    assert st["pending"] == ["cashier", "manager"]
    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]})
    st = signing_status(ctx["s"].get(Voucher, v.id))
    assert st["approved"] == ["cashier"]
    assert st["pending"] == ["manager"]


# ---------- 守卫：缺签拦审批 ----------


def test_pending_signatures_blocks_approve(ctx):
    v = _make(ctx, ["cashier"])
    with pytest.raises(PostingError) as ei:
        transition(ctx["s"], voucher_id=v.id,
                   actor={"type": "user", "id": ctx["manager"]}, target="APPROVED")
    assert ei.value.code == "PENDING_SIGNATURES"
    assert "cashier" in ei.value.details["missing"]

    # 签字补齐后自动 APPROVED
    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]})
    assert ctx["s"].get(Voucher, v.id).status == "APPROVED"


# ---------- 铁律：人与制单分离 ----------


def test_self_sign_rejected(ctx):
    v = _make(ctx, ["cashier"])
    with pytest.raises(PostingError) as ei:
        sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                     actor={"type": "user", "id": ctx["drafter"]})
    assert ei.value.code == "NO_SELF_APPROVAL"


def test_agent_sign_rejected(ctx):
    v = _make(ctx, ["cashier"])
    with pytest.raises(PostingError) as ei:
        sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                     actor={"type": "agent", "id": ctx["agent"]})
    assert ei.value.code == "AGENT_APPROVAL_FORBIDDEN"


# ---------- 拒签退回 ----------


def test_sign_reject_kills_voucher(ctx):
    v = _make(ctx, ["cashier", "manager"])
    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]},
                 decision="rejected", reason="金额与发票不符")
    v = ctx["s"].get(Voucher, v.id)
    assert v.status == "DRAFT"  # 任一拒签即退回制单人
    assert v.signatures[0]["decision"] == "rejected"
    assert ctx["s"].scalars(
        select(Event).where(Event.event_type == E.VOUCHER_REJECTED)
    ).first() is not None


# ---------- 签字位幂等与冲突 ----------


def test_slot_already_signed_by_other(ctx):
    v = _make(ctx, ["cashier", "manager"])  # 双签字位：出纳签完仍 PUSHED
    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]})
    with pytest.raises(PostingError) as ei:
        sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                     actor={"type": "user", "id": ctx["manager"]})
    assert ei.value.code == "SLOT_ALREADY_SIGNED"


def test_same_signer_idempotent(ctx):
    v = _make(ctx, ["cashier", "manager"])  # 双签字位：出纳签完仍 PUSHED
    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]})
    before = ctx["s"].get(Voucher, v.id).status
    # 同一人重复签同一 approved 位：幂等，不报错、不重复落事件
    sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                 actor={"type": "user", "id": ctx["cashier"]})
    assert ctx["s"].get(Voucher, v.id).status == before
    n = len(ctx["s"].scalars(
        select(Event).where(Event.event_type == E.VOUCHER_SIGNED)
    ).all())
    assert n == 1


def test_unknown_slot_rejected(ctx):
    v = _make(ctx, ["cashier"])
    with pytest.raises(PostingError) as ei:
        sign_voucher(ctx["s"], voucher_id=v.id, slot="cto",
                     actor={"type": "user", "id": ctx["cashier"]})
    assert ei.value.code == "SIGN_SLOT_UNKNOWN"


# ---------- 向后兼容：传统单层凭证不走签字 ----------


def test_legacy_no_signers_single_approve(ctx):
    v = _make(ctx, None)
    with pytest.raises(PostingError) as ei:
        sign_voucher(ctx["s"], voucher_id=v.id, slot="cashier",
                     actor={"type": "user", "id": ctx["cashier"]})
    assert ei.value.code == "NO_SIGN_SLOTS"
    # 传统单层审批仍由 approve_voucher（state.transition）完成
    transition(ctx["s"], voucher_id=v.id,
               actor={"type": "user", "id": ctx["manager"]}, target="APPROVED")
    assert ctx["s"].get(Voucher, v.id).status == "APPROVED"

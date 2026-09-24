"""WB 原生审批闭环（P0-4 / P0-3）内核助手 TDD。

DoD：
- bind_subject_external_ref：绑定 / 覆盖 / 不存在报错。
- resolve_reviewer：external_ref 优先命中 → fallback 候选 → 都不中抛 NO_REVIEWER。
- route_to_workbuddy：仅写 VOUCHER_ROUTED 事件、不改 voucher.status（通知层铁律）。
- 红线回流：WB 通道把真实 actor_id 传给 transition 时，即便谎报 type=user，
  _subject_type 从 Subject 表重新派生，AGENT_APPROVAL_FORBIDDEN / NO_SELF_APPROVAL
  仍拒绝——证明 WB 通道零改动即自动继承全部 HITL 红线。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.approval import (
    bind_subject_external_ref,
    resolve_reviewer,
    route_to_workbuddy,
)
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Period, Subject, Voucher, VoucherLine
from kernel.posting import PostingError
from kernel.seed import seed_demo_ledger
from kernel.state import transition

ZERO = Decimal("0.00")


@pytest.fixture()
def sess():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)  # 含新增 external_ref 列
    with Session(engine) as s:
        yield s


@pytest.fixture()
def env(sess):
    ids = seed_demo_ledger(sess)
    import_chart_of_accounts(sess, ids["ledger_set_id"], load_template_rows())
    p = sess.scalars(
        select(Period).where(
            Period.ledger_set_id == ids["ledger_set_id"],
            Period.year == 2026, Period.month == 8,
        )
    ).first()
    if p is None:
        p = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=8, status="OPEN")
        sess.add(p)
    sess.flush()
    return ids


def _period(sess, env) -> Period:
    return sess.scalars(
        select(Period).where(Period.ledger_set_id == env["ledger_set_id"])
    ).first()


def _subj(sess, sid: str, name: str, stype: str = "user", ext: str = "") -> Subject:
    subj = sess.get(Subject, sid)
    if subj is None:
        subj = Subject(id=sid, type=stype, display_name=name, external_ref=ext)
        sess.add(subj)
    else:
        subj.external_ref = ext
    sess.flush()
    return subj


def _voucher(sess, env, owner_id: str, status: str = "PUSHED") -> Voucher:
    acct = sess.scalars(
        select(Account).where(Account.ledger_set_id == env["ledger_set_id"])
    ).first()
    v = Voucher(
        ledger_set_id=env["ledger_set_id"],
        period_id=_period(sess, env).id,
        voucher_no="PZ-001",
        voucher_date=date(2026, 8, 5),
        status=status,
        summary="测试凭证",
        created_by=owner_id,
    )
    sess.add(v)
    sess.flush()
    sess.add(
        VoucherLine(
            voucher_id=v.id, line_no=1, account_id=acct.id,
            debit=Decimal("100"), credit=ZERO,
        )
    )
    sess.flush()
    return v


# ------------------------------------------------------------ bind


def test_bind_external_ref_basic(sess):
    s = _subj(sess, "u_bind", "老板")
    bind_subject_external_ref(sess, subject_id="u_bind", external_ref="wb-owner")
    assert sess.get(Subject, "u_bind").external_ref == "wb-owner"


def test_bind_external_ref_overwrites(sess):
    s = _subj(sess, "u_bind", "老板", ext="old")
    bind_subject_external_ref(sess, subject_id="u_bind", external_ref="new")
    assert sess.get(Subject, "u_bind").external_ref == "new"


def test_bind_external_ref_missing_subject(sess):
    with pytest.raises(PostingError) as ei:
        bind_subject_external_ref(sess, subject_id="nope", external_ref="x")
    assert ei.value.code == "SUBJECT_NOT_FOUND"


# ------------------------------------------------------------ resolve_reviewer


def test_resolve_reviewer_external_ref_wins(sess, env):
    _subj(sess, "wb_owner", "老板", ext="wb-owner")
    _subj(sess, "wb_rev", "审批人", ext="wb-rev")
    r = resolve_reviewer(
        sess, ledger_set_id=env["ledger_set_id"], wb_member_ref="wb-rev"
    )
    assert r.id == "wb_rev"


def test_resolve_reviewer_fallback_when_ref_unmatched(sess, env):
    _subj(sess, "wb_owner", "老板", ext="wb-owner")
    _subj(sess, "wb_rev", "审批人", ext="wb-rev")
    # 指定未绑定的 ref → 走 fallback（reviewer 在前、admin 在后）
    r = resolve_reviewer(
        sess, ledger_set_id=env["ledger_set_id"],
        wb_member_ref="unmatched", fallback_subject_ids=["wb_rev", "wb_owner"],
    )
    assert r.id == "wb_rev"


def test_resolve_reviewer_fallback_only(sess, env):
    _subj(sess, "wb_owner", "老板")
    _subj(sess, "wb_rev", "审批人")
    r = resolve_reviewer(
        sess, ledger_set_id=env["ledger_set_id"], fallback_subject_ids=["wb_rev", "wb_owner"]
    )
    assert r.id == "wb_rev"


def test_resolve_reviewer_none_match_raises(sess, env):
    with pytest.raises(PostingError) as ei:
        resolve_reviewer(
            sess, ledger_set_id=env["ledger_set_id"], wb_member_ref="x",
            fallback_subject_ids=[],
        )
    assert ei.value.code == "NO_REVIEWER"


# ------------------------------------------------------------ route_to_workbuddy


def test_route_writes_routed_only(sess, env):
    owner = _subj(sess, "owner1", "老板")
    reviewer = _subj(sess, "rev1", "审批人")
    v = _voucher(sess, env, owner_id="owner1", status="PUSHED")
    n_events_before = len(sess.scalars(select(Event)).all())

    res = route_to_workbuddy(
        sess, voucher_id=v.id, actor={"type": "user", "id": "owner1"},
        channel="workbuddy", fallback_subject_ids=["rev1", "owner1"],
    )

    v2 = sess.get(Voucher, v.id)
    assert v2.status == "PUSHED", "route 改了 voucher.status（违反通知层铁律）"
    assert res["routed"] is True
    assert res["reviewer"].id == "rev1"
    notice = res["notice"]
    assert notice["voucher_no"] == "PZ-001"
    assert notice["deeplink"].startswith("xerp://voucher/")
    assert set(notice["verbs"]) == {"approve", "reject"}
    evs = sess.scalars(select(Event)).all()
    assert len(evs) == n_events_before + 1
    assert any(e.event_type == "VOUCHER_ROUTED" for e in evs)


def test_route_rejects_non_pushed(sess, env):
    owner = _subj(sess, "owner1", "老板")
    _subj(sess, "rev1", "审批人")
    v = _voucher(sess, env, owner_id="owner1", status="DRAFT")
    with pytest.raises(PostingError) as ei:
        route_to_workbuddy(
            sess, voucher_id=v.id, actor={"type": "user", "id": "owner1"},
            fallback_subject_ids=["rev1", "owner1"],
        )
    assert ei.value.code == "INVALID_TRANSITION"


# ------------------------------------------------------------ 红线回流（WB 通道零改动继承）


def test_wb_channel_agent_approval_forbidden(sess, env):
    """即便 WB 通道把 actor.type 谎报为 user，内核从 Subject 表重新派生 → 仍拒绝。

    证明 WB 通道不需要任何特殊防护，红线自动复用。
    """
    agent = _subj(sess, "ag1", "会计Agent", stype="agent")
    owner = _subj(sess, "owner1", "老板")
    v = _voucher(sess, env, owner_id="owner1", status="PUSHED")
    with pytest.raises(PostingError) as ei:
        # actor 谎报 type=user，但 id 指向 agent 主体
        transition(sess, voucher_id=v.id, actor={"id": "ag1", "type": "user"},
                   target="APPROVED")
    assert ei.value.code == "AGENT_APPROVAL_FORBIDDEN"


def test_wb_channel_no_self_approval(sess, env):
    """制单人与审批人同一主体 → WB 通道回流仍拒绝（制单≠审批红线复用）。"""
    owner = _subj(sess, "owner1", "老板")
    v = _voucher(sess, env, owner_id="owner1", status="PUSHED")
    with pytest.raises(PostingError) as ei:
        transition(sess, voucher_id=v.id, actor={"id": "owner1"}, target="APPROVED")
    assert ei.value.code == "NO_SELF_APPROVAL"

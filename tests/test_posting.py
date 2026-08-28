"""P0-05 TDD：记账内核纯函数 — validate_voucher / post_voucher。

DoD（DEVPLAN）：
- validate_voucher 全部分支（行数/平衡/双边/金额/科目/期间状态/期间匹配）
- post_voucher 状态机硬校验 + voucher.posted 事件 + balances 投影
- 校验失败事务零副作用；借贷不平衡为「硬拒」（VOUCHER_UNBALANCED）
金额一律 Decimal，全程禁止 float。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Balance, Event, Period, Voucher, VoucherLine
from kernel.posting import (
    PostingError,
    PostingLine,
    post_voucher,
    validate_voucher,
)
from kernel.seed import seed_demo_ledger
from kernel.state import transition

ACTOR = {"type": "user", "id": "u1", "display_name": "丞辰"}
ZERO = Decimal("0.00")


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def ids(session):
    ids = seed_demo_ledger(session)
    session.flush()
    return ids


def _pair(ids, *, amount=Decimal("100.00")):
    """一对合法分录：费用借 / 现金贷。"""
    return [
        PostingLine(account_id=ids["expense_account_id"], debit=amount, credit=ZERO),
        PostingLine(account_id=ids["cash_account_id"], debit=ZERO, credit=amount),
    ]


def _accounts_map(ids):
    return {aid: object() for aid in (
        ids["expense_account_id"],
        ids["cash_account_id"],
        ids["bank_account_id"],
        ids["revenue_account_id"],
    )}


def _validate_ok(ids, lines):
    validate_voucher(
        lines=lines,
        accounts_by_id=_accounts_map(ids),
        period_status="OPEN",
        period_year=2026,
        period_month=8,
        voucher_date=date(2026, 8, 27),
    )


# ---------- validate_voucher ----------

def test_validate_rejects_single_line(ids):
    with pytest.raises(PostingError) as ei:
        _validate_ok(ids, _pair(ids)[:1])
    assert ei.value.code == "NO_LINES"


def test_validate_rejects_unbalanced_hard(ids):
    """借贷不平衡必须硬拒（DoD）。"""
    lines = [
        PostingLine(account_id=ids["expense_account_id"], debit=Decimal("100.00"), credit=ZERO),
        PostingLine(account_id=ids["cash_account_id"], debit=ZERO, credit=Decimal("99.00")),
    ]
    with pytest.raises(PostingError) as ei:
        _validate_ok(ids, lines)
    assert ei.value.code == "VOUCHER_UNBALANCED"
    assert ei.value.message_zh  # 中文信息可直接展示


def test_validate_rejects_line_both_sides(ids):
    lines = [
        PostingLine(
            account_id=ids["cash_account_id"],
            debit=Decimal("10.00"),
            credit=Decimal("10.00"),
        ),
        PostingLine(account_id=ids["expense_account_id"], debit=ZERO, credit=Decimal("10.00")),
    ]
    with pytest.raises(PostingError) as ei:
        _validate_ok(ids, lines)
    assert ei.value.code == "LINE_BOTH_SIDES"


def test_validate_rejects_both_zero(ids):
    lines = [
        PostingLine(account_id=ids["expense_account_id"], debit=ZERO, credit=ZERO),
        PostingLine(account_id=ids["cash_account_id"], debit=ZERO, credit=Decimal("1.00")),
    ]
    with pytest.raises(PostingError) as ei:
        _validate_ok(ids, lines)
    assert ei.value.code == "AMOUNT_INVALID"


@pytest.mark.parametrize("side", ["debit", "credit"])
def test_validate_rejects_negative_amount(ids, side):
    kw = {"debit": ZERO, "credit": Decimal("50.00")}
    kw[side] = Decimal("-5.00")
    other = "credit" if side == "debit" else "debit"
    lines = [
        PostingLine(account_id=ids["expense_account_id"], **kw),
        PostingLine(account_id=ids["cash_account_id"], **{other: Decimal("50.00"), side: ZERO}),
    ]
    with pytest.raises(PostingError) as ei:
        _validate_ok(ids, lines)
    assert ei.value.code == "AMOUNT_INVALID"


def test_validate_rejects_unknown_account(ids):
    lines = [
        PostingLine(account_id="no-such-account", debit=Decimal("1.00"), credit=ZERO),
        PostingLine(account_id=ids["cash_account_id"], debit=ZERO, credit=Decimal("1.00")),
    ]
    with pytest.raises(PostingError) as ei:
        _validate_ok(ids, lines)
    assert ei.value.code == "ACCOUNT_NOT_FOUND"
    assert ei.value.details["account_id"] == "no-such-account"


def test_validate_rejects_period_not_open(ids):
    with pytest.raises(PostingError) as ei:
        validate_voucher(
            lines=_pair(ids),
            accounts_by_id=_accounts_map(ids),
            period_status="CLOSED",
            period_year=2026,
            period_month=8,
            voucher_date=date(2026, 8, 27),
        )
    assert ei.value.code == "PERIOD_NOT_OPEN"


def test_validate_rejects_period_mismatch(ids):
    """凭证日期年月与所属期间不匹配。"""
    with pytest.raises(PostingError) as ei:
        validate_voucher(
            lines=_pair(ids),
            accounts_by_id=_accounts_map(ids),
            period_status="OPEN",
            period_year=2026,
            period_month=8,
            voucher_date=date(2026, 9, 1),
        )
    assert ei.value.code == "PERIOD_MISMATCH"


def test_validate_accepts_valid_voucher(ids):
    _validate_ok(ids, _pair(ids))  # 不抛错即通过


# ---------- post_voucher ----------

def _add_voucher(
    session,
    ids,
    *,
    status="APPROVED",
    lines=None,
    voucher_no="记-0001",
    voucher_date=date(2026, 8, 27),
):
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"],
        period_id=ids["period_id"],
        voucher_no=voucher_no,
        voucher_date=voucher_date,
        status=status,
        summary="测试凭证",
        created_by=ids["subject_id"],
    )
    if lines is None:
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("100.00"), credit=ZERO),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=ZERO, credit=Decimal("100.00")),
        ]
    else:
        v.lines = lines
    session.add(v)
    session.flush()
    return v


def test_post_rejects_draft(session, ids):
    v = _add_voucher(session, ids, status="DRAFT")
    with pytest.raises(PostingError) as ei:
        post_voucher(session, voucher_id=v.id, actor=ACTOR)
    assert ei.value.code == "INVALID_TRANSITION"
    assert v.status == "DRAFT"  # 状态未被改动


def test_post_happy_path_posts_event_and_balances(session, ids):
    """APPROVED → POSTED：事件 + 投影按 (账套,期间,科目,dims 规范键) 累计。"""
    aux = {"department": "销售部"}
    v = _add_voucher(session, ids, lines=[
        VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                    debit=Decimal("100.00"), credit=ZERO, aux_dims=dict(aux)),
        VoucherLine(line_no=2, account_id=ids["expense_account_id"],
                    debit=Decimal("50.00"), credit=ZERO, aux_dims=dict(aux)),
        VoucherLine(line_no=3, account_id=ids["cash_account_id"],
                    debit=ZERO, credit=Decimal("150.00")),
    ])

    evt = post_voucher(session, voucher_id=v.id, actor=ACTOR)

    assert evt.event_type == "voucher.posted"
    assert evt.aggregate_id == v.id
    assert evt.actor == ACTOR
    assert evt.payload["voucher_no"] == "记-0001"
    assert len(evt.payload["lines"]) == 3
    assert Decimal(evt.payload["lines"][0]["debit"]) == Decimal("100.00")
    assert evt.payload["lines"][2]["aux_dims"] is None

    assert v.status == "POSTED"
    assert v.posted_at is not None

    rows = {
        (b.account_id, b.dims_key): b
        for b in session.scalars(select(Balance)).all()
    }
    expense_key = '{"department":"销售部"}'  # canonical：排序键+无空白
    exp_bal = rows[(ids["expense_account_id"], expense_key)]
    cash_bal = rows[(ids["cash_account_id"], "")]
    assert Decimal(exp_bal.debit_total) == Decimal("150.00")
    assert Decimal(exp_bal.credit_total) == Decimal("0.00")
    assert Decimal(cash_bal.credit_total) == Decimal("150.00")
    assert Decimal(cash_bal.debit_total) == Decimal("0.00")

    session.commit()  # commit 由调用方负责


def test_post_twice_raises_invalid_transition(session, ids):
    v = _add_voucher(session, ids)
    post_voucher(session, voucher_id=v.id, actor=ACTOR)
    session.commit()
    with pytest.raises(PostingError) as ei:
        post_voucher(session, voucher_id=v.id, actor=ACTOR)
    assert ei.value.code == "INVALID_TRANSITION"


def test_post_validation_failure_leaves_no_trace(session, ids):
    """校验失败 → 事务回滚后 events 无新行、status 不变（零副作用）。"""
    bad = [
        VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                    debit=Decimal("100.00"), credit=ZERO),
        VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                    debit=ZERO, credit=Decimal("88.88")),
    ]
    v = _add_voucher(session, ids, lines=bad)
    session.commit()  # 先落盘，使 rollback 只作用于 post 尝试
    before = session.scalar(select(func.count()).select_from(Event))
    with pytest.raises(PostingError) as ei:
        post_voucher(session, voucher_id=v.id, actor=ACTOR)
    assert ei.value.code == "VOUCHER_UNBALANCED"
    session.rollback()
    after = session.scalar(select(func.count()).select_from(Event))
    assert before == after
    fresh = session.get(Voucher, v.id)
    assert fresh.status == "APPROVED"
    assert fresh.posted_at is None


def test_post_period_not_open_blocked(session, ids):
    period = session.get(Period, ids["period_id"])
    period.status = "CLOSING"
    v = _add_voucher(session, ids)
    with pytest.raises(PostingError) as ei:
        post_voucher(session, voucher_id=v.id, actor=ACTOR)
    assert ei.value.code == "PERIOD_NOT_OPEN"
    assert v.status != "POSTED"


def test_posted_event_aggregate_matches_voucher():
    """回归：voucher.posted 的 aggregate_id 必须是凭证 id（可按凭证回放轨迹）。"""
    from kernel.db.models import Event

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
        voucher_no="记-9001", voucher_date=date(2026, 8, 28), status="DRAFT",
        summary="agg 回归", created_by=ids["subject_id"],
    )
    v.lines = [
        VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                    debit=Decimal("5.00"), credit=Decimal("0.00")),
        VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                    debit=Decimal("0.00"), credit=Decimal("5.00")),
    ]
    s.add(v)
    s.flush()
    transition(s, voucher_id=v.id, actor={"type": "agent", "id": "a"}, target="PUSHED")
    from kernel.db.models import Subject

    reviewer = Subject(type="user", display_name="回归审批人", autonomy_level=3)
    s.add(reviewer)
    s.flush()
    transition(s, voucher_id=v.id, actor={"type": "user", "id": reviewer.id}, target="APPROVED")
    post_voucher(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]})
    s.commit()
    posted = s.scalars(
        select(Event).where(Event.event_type == "voucher.posted")
    ).all()
    assert [e.aggregate_id for e in posted] == [v.id]
    s.close()

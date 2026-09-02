"""P0-2 回归：期初余额导入幂等性（防「重复导入导致期初翻倍」毁账级缺陷）。

缺陷复现（修复前）：
    同一账套连续两次 import_opening_balances → 生成 期初-0001 / 期初-0002
    两张凭证，余额投影累加 → 资产 200,000 → 400,000。因为期初凭证不经审批直接
    POSTED，审计链上「合法」，事后极难发现。

修复约定：
    1. 默认（force=False）已有期初 → 抛 OPENING_ALREADY_IMPORTED，details 带回已有凭证
    2. force=True → 先红字冲销全部旧期初（余额归零 + OPENING_BALANCE_REVERSED 事件），
       再导入新期初；原凭证不删不改，审计链完整
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Balance, Event, Voucher
from kernel.events import E
from kernel.opening import import_opening_balances
from kernel.posting import PostingError
from kernel.seed import seed_demo_ledger

LINES_A = [
    {"account_code": "100201", "debit": "200000.00", "credit": ""},
    {"account_code": "3001", "debit": "", "credit": "200000.00"},
]
LINES_B = [
    {"account_code": "100201", "debit": "150000.00", "credit": ""},
    {"account_code": "3001", "debit": "", "credit": "150000.00"},
]


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    return s, ids


def _import(s, ids, lines, **kw):
    return import_opening_balances(
        s,
        ledger_set_id=ids["ledger_set_id"],
        actor={"type": "user", "id": ids["subject_id"]},
        lines=lines,
        **kw,
    )


def _opening_vouchers(s, ids):
    return list(
        s.scalars(
            select(Voucher)
            .where(
                Voucher.ledger_set_id == ids["ledger_set_id"],
                Voucher.voucher_no.like("期初-%"),
            )
            .order_by(Voucher.voucher_no)
        ).all()
    )


def _net(s, ids, account_code: str) -> Decimal:
    """某科目的净余额（借方 - 贷方）。

    注意口径：不能用「全部科目借/贷方合计」来判断期初是否翻倍——
    红字冲销本身会产生反向分录（冲销原贷方科目时记借方），
    合计里会带上这些冲销额。净余额才是账户真实口径。
    """
    from kernel.db.models import Account

    acc = s.scalar(
        select(Account).where(
            Account.ledger_set_id == ids["ledger_set_id"],
            Account.code == account_code,
        )
    )
    assert acc is not None, f"科目 {account_code} 不存在"
    d = Decimal(
        str(
            s.scalar(
                select(func.coalesce(func.sum(Balance.debit_total), 0)).where(
                    Balance.ledger_set_id == ids["ledger_set_id"],
                    Balance.account_id == acc.id,
                )
            )
            or 0
        )
    )
    c = Decimal(
        str(
            s.scalar(
                select(func.coalesce(func.sum(Balance.credit_total), 0)).where(
                    Balance.ledger_set_id == ids["ledger_set_id"],
                    Balance.account_id == acc.id,
                )
            )
            or 0
        )
    )
    return d - c


def _trial_balance_ok(s, ids) -> bool:
    """全部科目净额之和为 0 → 试算平衡（借贷必相等）。"""
    total = Decimal("0")
    for code in ("100201", "3001"):
        total += _net(s, ids, code)
    return total == Decimal("0")


# ════════════════════════════════════════════════════════════
# 1. 核心防翻倍
# ════════════════════════════════════════════════════════════
def test_repeat_import_rejected(ctx):
    """重复导入必须被拒绝，且余额不翻倍（缺陷核心）。"""
    s, ids = ctx
    _import(s, ids, LINES_A)
    s.commit()
    assert _net(s, ids, "100201") == Decimal("200000.00")

    with pytest.raises(PostingError) as ei:
        _import(s, ids, LINES_A)
    assert ei.value.code == "OPENING_ALREADY_IMPORTED"
    s.rollback()

    # 余额纹丝不动，凭证仍然只有一张
    assert _net(s, ids, "100201") == Decimal("200000.00")
    assert len(_opening_vouchers(s, ids)) == 1


def test_error_details_carry_existing_vouchers(ctx):
    """拒绝时 details 必须带回已有凭证，便于用户核对后再决定。"""
    s, ids = ctx
    first = _import(s, ids, LINES_A)
    s.commit()

    with pytest.raises(PostingError) as ei:
        _import(s, ids, LINES_A)
    det = ei.value.details or {}
    assert det.get("existing_vouchers")
    assert det["existing_vouchers"][0]["voucher_no"] == first.voucher_no


def test_web_double_submit_safe(ctx):
    """Web 端重复 POST 同一份期初（用户双击/刷新）→ 只生效一次。"""
    s, ids = ctx
    _import(s, ids, LINES_A)
    s.commit()
    before = _net(s, ids, "100201")

    for _ in range(3):  # 模拟连点三次
        with pytest.raises(PostingError):
            _import(s, ids, LINES_A)
        s.rollback()

    assert _net(s, ids, "100201") == before
    assert len(_opening_vouchers(s, ids)) == 1


# ════════════════════════════════════════════════════════════
# 2. force 覆盖语义
# ════════════════════════════════════════════════════════════
def test_force_reimport_replaces_not_accumulates(ctx):
    """force=True：新期初应当『替换』而非『累加』。"""
    s, ids = ctx
    _import(s, ids, LINES_A)  # 20 万
    s.commit()

    _import(s, ids, LINES_B, force=True)  # 改为 15 万
    s.commit()

    # 关键：不是 20+15=35 万，而是 15 万
    assert _net(s, ids, "100201") == Decimal("150000.00")
    assert _trial_balance_ok(s, ids)


def test_force_leaves_audit_trail(ctx):
    """force 重导必须留审计痕：冲销事件存在，且原凭证不删不改。"""
    s, ids = ctx
    first = _import(s, ids, LINES_A)
    s.commit()
    first_no, first_id = first.voucher_no, first.id

    _import(s, ids, LINES_B, force=True)
    s.commit()

    # 原凭证仍在（append-only）
    assert s.get(Voucher, first_id) is not None
    assert s.get(Voucher, first_id).voucher_no == first_no

    # 冲销事件已追加
    rev = list(
        s.scalars(
            select(Event).where(
                Event.ledger_set_id == ids["ledger_set_id"],
                Event.event_type == E.OPENING_BALANCE_REVERSED,
            )
        ).all()
    )
    assert len(rev) == 1
    assert rev[0].aggregate_id == first_id
    assert rev[0].payload["voucher_no"] == first_no
    assert rev[0].payload["reason"] == "force_reimport"


def test_force_sequence_not_reused(ctx):
    """凭证号继续递增，不复用旧号（审计可追溯）。"""
    s, ids = ctx
    v1 = _import(s, ids, LINES_A)
    s.commit()
    v2 = _import(s, ids, LINES_B, force=True)
    s.commit()
    assert v1.voucher_no == "期初-0001"
    assert v2.voucher_no == "期初-0002"


def test_force_thrice_converges(ctx):
    """连续 force 三次，余额收敛到最后一次的值（冲销链闭合）。"""
    s, ids = ctx
    _import(s, ids, LINES_A)  # 20 万
    s.commit()
    _import(s, ids, LINES_B, force=True)  # 15 万
    s.commit()
    _import(s, ids, LINES_A, force=True)  # 再改回 20 万
    s.commit()
    assert _net(s, ids, "100201") == Decimal("200000.00")
    assert _trial_balance_ok(s, ids)


# ════════════════════════════════════════════════════════════
# 3. 既有行为不回归
# ════════════════════════════════════════════════════════════
def test_first_import_unchanged(ctx):
    """首次导入行为完全不变（凭证号、状态、余额）。"""
    s, ids = ctx
    v = _import(s, ids, LINES_A)
    s.commit()
    assert v.voucher_no == "期初-0001"
    assert v.status == "POSTED"
    assert _net(s, ids, "100201") == Decimal("200000.00")
    assert _net(s, ids, "3001") == Decimal("-200000.00")
    assert _trial_balance_ok(s, ids)


def test_unbalanced_still_rejected(ctx):
    """试算不平衡仍整体拒绝，且不落任何数据。"""
    s, ids = ctx
    with pytest.raises(PostingError) as ei:
        _import(
            s,
            ids,
            [
                {"account_code": "100201", "debit": "100000.00", "credit": ""},
                {"account_code": "3001", "debit": "", "credit": "99999.00"},
            ],
        )
    assert ei.value.code == "TRIAL_BALANCE_UNBALANCED"
    s.rollback()
    assert len(_opening_vouchers(s, ids)) == 0
    assert _net(s, ids, "100201") == Decimal("0")


def test_force_on_empty_ledger_ok(ctx):
    """空账套传 force=True 不应报错（无旧期初可冲销）。"""
    s, ids = ctx
    v = _import(s, ids, LINES_A, force=True)
    s.commit()
    assert v.voucher_no == "期初-0001"
    assert _net(s, ids, "100201") == Decimal("200000.00")

"""P0-03 TDD：Ontology Schema v0 — 先红后绿。

DoD（DEVPLAN）：
- 种子脚本建演示账套成功
- 科目/辅助核算维度(JSON)/期间/往来对象/凭证草稿 全部可落库且约束生效
- alembic 迁移可 upgrade head / downgrade base
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_seed_demo_ledger(session):
    ids = seed_demo_ledger(session)
    session.flush()

    from kernel.db import models as m

    ls = session.get(m.LedgerSet, ids["ledger_set_id"])
    assert ls is not None and ls.name == "演示账套"
    assert ls.functional_currency == "CNY"

    accounts = session.scalars(
        select(m.Account).where(m.Account.ledger_set_id == ids["ledger_set_id"])
    ).all()
    # 库存现金/银行存款/实收资本/主营业务收入/管理费用
    assert len(accounts) >= 5
    cash = next(a for a in accounts if a.code == "1001")
    assert cash.direction == "debit"

    period = session.get(m.Period, ids["period_id"])
    assert period.status == "OPEN" and (period.year, period.month) == (2026, 8)

    party = session.get(m.Party, ids["party_id"])
    assert party.party_type == "customer"
    assert party.aux_attrs == {"level": "A"}


def test_voucher_draft_with_aux_dims(session):
    ids = seed_demo_ledger(session)
    session.flush()

    from kernel.db import models as m

    v = m.Voucher(
        ledger_set_id=ids["ledger_set_id"],
        period_id=ids["period_id"],
        voucher_no="记-0001",
        voucher_date=date(2026, 8, 27),
        status="DRAFT",
        summary="测试凭证",
        created_by=ids["subject_id"],
    )
    v.lines = [
        m.VoucherLine(
            line_no=1,
            account_id=ids["expense_account_id"],
            debit=Decimal("800.00"),
            credit=Decimal("0.00"),
            aux_dims={"department": "销售部"},
        ),
        m.VoucherLine(
            line_no=2,
            account_id=ids["cash_account_id"],
            debit=Decimal("0.00"),
            credit=Decimal("800.00"),
        ),
    ]
    session.add(v)
    session.flush()

    row = session.scalars(select(m.Voucher).where(m.Voucher.voucher_no == "记-0001")).one()
    assert row.status == "DRAFT"
    assert len(row.lines) == 2
    assert row.lines[0].aux_dims == {"department": "销售部"}
    assert Decimal(row.lines[0].debit) == Decimal("800.00")


def test_unique_account_code_per_ledger(session):
    from kernel.db import models as m

    ids = seed_demo_ledger(session)
    session.flush()
    dup = m.Account(
        ledger_set_id=ids["ledger_set_id"],
        code="1001",
        name="重复现金",
        direction="debit",
        category="asset",
    )
    session.add(dup)
    with pytest.raises(IntegrityError):  # unique(ledger_set_id, code)
        session.flush()
    session.rollback()


def test_alembic_upgrade_downgrade(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    db = tmp_path / "mig.db"
    env = {**os.environ, "LEDGEROS_DB": f"sqlite:///{db}"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stdout + r.stderr

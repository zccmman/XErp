"""P0-06 TDD：小企业会计准则科目模板 + 导入工具。

DoD：模板导入后科目数 >=140；编码层级正确（父子前缀/根为 4 位）；幂等可重导。
"""

import io
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kernel.coa import (
    CoaImportError,
    builtin_template_path,
    import_chart_of_accounts,
    load_template_rows,
)
from kernel.db.base import Base
from kernel.db.models import Account, LedgerSet


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ls = LedgerSet(name="模板测试账套")
        s.add(ls)
        s.flush()
        yield s, ls.id


def _import(session, ls_id):
    return import_chart_of_accounts(session, ls_id, load_template_rows())


def test_template_has_140_plus():
    rows = load_template_rows()
    assert len(rows) >= 140


def test_hierarchy_rules():
    rows = load_template_rows()
    codes = {r["code"] for r in rows}
    for r in rows:
        assert r["code"].isdigit(), r
        assert len(r["code"]) in (4, 6), f"编码长度非法: {r['code']}"
        assert r["direction"] in ("debit", "credit")
        assert r["category"] in ("asset", "liability", "equity", "cost", "pnl")
        # 类别与前缀一致
        expect = {"1": "asset", "2": "liability", "3": "equity", "5": "cost", "6": "pnl"}
        assert r["category"] == expect[r["code"][0]], r
        if r["parent_code"]:
            assert r["parent_code"] == r["code"][:-2], f"前缀断裂: {r}"
            assert r["parent_code"] in codes, f"父科目缺失: {r}"


def test_import_creates_linked_tree(session):
    s, ls_id = session
    rows = load_template_rows()
    stats = _import(s, ls_id)
    assert stats["created"] == len(rows) >= 140

    cash = s.scalars(select(Account).where(Account.code == "1001")).one()
    assert cash.is_leaf is True and cash.parent_id is None

    bank_child = s.scalars(select(Account).where(Account.code == "100201")).one()
    bank = s.scalars(select(Account).where(Account.code == "1002")).one()
    assert bank_child.parent_id == bank.id and bank.is_leaf is False

    ar = s.scalars(select(Account).where(Account.code == "1122")).one()
    assert ar.aux_dim_defs == ["customer"]

    n = s.scalar(select(func.count()).select_from(Account))
    assert n == len(rows)


def test_import_idempotent(session):
    s, ls_id = session
    first = _import(s, ls_id)
    second = _import(s, ls_id)
    assert first["created"] == len(load_template_rows())
    assert second["created"] == 0
    n = s.scalar(select(func.count()).select_from(Account))
    assert n == len(load_template_rows())


def test_bad_parent_rejected(session):
    s, ls_id = session
    bad = io.StringIO(
        "code,name,direction,category,parent_code\n"
        "100299,孤儿科目,debit,asset,9999\n"
    )
    with pytest.raises(CoaImportError):
        import_chart_of_accounts(s, ls_id, __import__("csv").DictReader(bad))


def test_duplicate_code_rejected(session):
    s, ls_id = session
    bad = io.StringIO(
        "code,name,direction,category,parent_code\n"
        "1001,现金A,debit,asset,\n"
        "1001,现金B,debit,asset,\n"
    )
    with pytest.raises(CoaImportError):
        import_chart_of_accounts(s, ls_id, __import__("csv").DictReader(bad))


def test_category_prefix_mismatch_rejected(session):
    s, ls_id = session
    bad = io.StringIO(
        "code,name,direction,category,parent_code\n"
        "2222,伪资产,debit,asset,\n"
    )
    with pytest.raises(CoaImportError):
        import_chart_of_accounts(s, ls_id, __import__("csv").DictReader(bad))


def test_builtin_template_file_exists():
    p: Path = builtin_template_path()
    assert p.exists() and p.stat().st_size > 1000

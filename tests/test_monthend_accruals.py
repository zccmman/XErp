"""G8 周期性预提/计提（TDD）。

DoD：
- 内置模板 计提坏账准备 / 预提利息 注册且结构合法（validate_template 通过）。
- run_template(计提坏账准备) 按 1122 余额×5% 生成 PUSHED 凭证（Dr 6701 / Cr 1231，借贷平衡）；
  余额为零 → NOTHING_TO_TRANSFER；重复执行 → ALREADY_RUN。
- 月结 step 2.6 recurring_accruals 默认只列出本月到期 monthly 模板（只读、不改账）；
  auto_prepare=True 且非 dry_run 时自动制备 PUSHED 草稿（HITL，绝不自动过账）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Period, Subject, Voucher, VoucherLine
from kernel.monthend import MonthendError, run_monthend
from kernel.posting import post_voucher
from kernel.reporting.statements import ReportError
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.transfers import (
    TransferError,
    list_templates,
    load_builtin_templates,
    run_template,
    validate_template,
)
from kernel.voucher_wizard import create_draft_voucher


@pytest.fixture()
def sess():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
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
        p = Period(ledger_set_id=ids["ledger_set_id"], year=2026, month=8,
                   status="OPEN")
        sess.add(p)
    sess.flush()
    return ids


@pytest.fixture()
def ctx():
    """复用 test_monthend 的会话夹具：建账 + 导入科目 + 审批人。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
    s.add(reviewer)
    s.commit()
    return {"s": s, "ids": ids,
            "actor": {"type": "user", "id": ids["subject_id"]},
            "reviewer": {"type": "user", "id": reviewer.id}}


def _book(s, ids, ctx, no, day, summary, amount, status="PUSHED",
          debit_code="660202", credit_code="100201"):
    accs = {a.code: a for a in s.scalars(select(Account)).all()}
    v = Voucher(
        ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
        voucher_no=no, voucher_date=day, status="DRAFT", summary=summary,
        created_by=ids["subject_id"],
    )
    v.lines = [
        VoucherLine(line_no=1, account_id=accs[debit_code].id,
                    debit=amount, credit=Decimal("0.00")),
        VoucherLine(line_no=2, account_id=accs[credit_code].id,
                    debit=Decimal("0.00"), credit=amount),
    ]
    s.add(v)
    s.flush()
    transition(s, voucher_id=v.id, actor=ctx["actor"], target="PUSHED")
    if status in ("APPROVED", "POSTED"):
        transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="APPROVED")
    if status == "POSTED":
        post_voucher(s, voucher_id=v.id, actor=ctx["actor"])
    s.commit()
    return v


def _seed_balance(sess, env, code, debit_total):
    """注入一条科目余额投影（模拟关账后累计），供转账模板取数。"""
    acc = sess.scalars(
        select(Account).where(
            Account.ledger_set_id == env["ledger_set_id"], Account.code == code)
    ).first()
    period = sess.scalars(
        select(Period).where(
            Period.ledger_set_id == env["ledger_set_id"],
            Period.year == 2026, Period.month == 8)
    ).first()
    sess.add(Balance(
        id=f"bal-{code}", ledger_set_id=env["ledger_set_id"],
        period_id=period.id, account_id=acc.id, dims_key="",
        debit_total=Decimal(str(debit_total)), credit_total=Decimal("0.00"),
    ))
    sess.commit()


def test_builtin_templates_registered():
    load_builtin_templates()
    names = {t["name"] for t in list_templates()}
    assert "计提坏账准备" in names
    assert "预提利息" in names


def test_builtin_templates_valid():
    load_builtin_templates()
    for t in list_templates():
        if t["name"] in ("计提坏账准备", "预提利息"):
            # 取原始 JSON 重新校验结构
            from pathlib import Path
            import json

            p = Path(__file__).resolve().parents[1] / "kernel" / "data" / "transfers"
            raw = json.loads((p / f"{t['name']}.json").read_text(encoding="utf-8"))
            validate_template(raw)  # 不抛即合法


def test_run_template_bad_debt(sess, env):
    _seed_balance(sess, env, "1122", "10000")  # 应收账款余额 10000
    rep = run_template(sess, ledger_set_id=env["ledger_set_id"],
                       template_name="计提坏账准备", year=2026, month=8,
                       actor={"id": "u3"})
    sess.commit()
    assert rep["voucher"]["status"] == "PUSHED"
    v = sess.get(Voucher, rep["voucher"]["id"])
    dr = Decimal("0.00")
    cr = Decimal("0.00")
    for ln in v.lines:
        dr += Decimal(str(ln.debit))
        cr += Decimal(str(ln.credit))
    assert dr == cr == Decimal("500.00")  # 10000 × 5%
    codes = {sess.get(Account, ln.account_id).code for ln in v.lines}
    assert "6701" in codes and "1231" in codes


def test_run_template_zero_balance(sess, env):
    # 未注入 1122 余额 → 取数全零 → 无需转账
    with pytest.raises(TransferError) as ei:
        run_template(sess, ledger_set_id=env["ledger_set_id"],
                     template_name="计提坏账准备", year=2026, month=8,
                     actor={"id": "u3"})
    assert ei.value.code == "NOTHING_TO_TRANSFER"


def test_run_template_idempotent(sess, env):
    _seed_balance(sess, env, "1122", "10000")
    run_template(sess, ledger_set_id=env["ledger_set_id"],
                 template_name="计提坏账准备", year=2026, month=8,
                 actor={"id": "u3"})
    sess.commit()
    with pytest.raises(TransferError) as ei:
        run_template(sess, ledger_set_id=env["ledger_set_id"],
                     template_name="计提坏账准备", year=2026, month=8,
                     actor={"id": "u3"})
    assert ei.value.code == "ALREADY_RUN"


def test_monthend_lists_accruals_readonly(ctx):
    """默认只列出本月到期模板（只读，不改账）。"""
    s, ids = ctx["s"], ctx["ids"]
    _book(s, ids, ctx, "记-7201", __import__("datetime").date(2026, 8, 8),
          "已审批费用", Decimal("800.00"), status="POSTED")
    rep = run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026,
                       month=8, actor=ctx["actor"])
    step = rep["steps"]["recurring_accruals"]
    assert step["due_count"] >= 2
    assert all(not it["prepared"] for it in step["items"])
    # 未注入余额 → 即使列出也不该误生成凭证（默认不制备）
    assert all(it["voucher_no"] is None for it in step["items"])


def test_monthend_auto_prepare(ctx):
    """auto_prepare=True 且非 dry_run 时自动制备坏账准备 PUSHED 草稿。

    制备的草稿处于 PUSHED（待人审）状态，因此月结门禁会正确地因「存在待审凭证」
    而中止（HITL：Agent 不替 Boss 审批/过账）。测试同时验证两点：
    (1) 计提坏账准备草稿确已生成，金额=应收账款余额×5%；
    (2) 关账被 HITL 门禁拦截（PENDING_VOUCHERS）。
    """
    s, ids = ctx["s"], ctx["ids"]
    # 一笔已审费用，使账套有常规业务
    _book(s, ids, ctx, "记-7202", __import__("datetime").date(2026, 8, 9),
          "已审批费用", Decimal("800.00"), status="POSTED")
    # 赊销形成应收账款余额 20000（已审 → 自动落地 1122 余额投影，保证账账核对通过）
    _book(s, ids, ctx, "记-7203", __import__("datetime").date(2026, 8, 10),
          "赊销", Decimal("20000.00"), status="POSTED",
          debit_code="1122", credit_code="100201")
    with pytest.raises(MonthendError) as ei:
        run_monthend(s, ledger_set_id=ids["ledger_set_id"], year=2026,
                     month=8, actor=ctx["actor"], auto_prepare=True)
    # 制备的草稿为 PUSHED，门禁按 HITL 拦截（绝不过账/代审）
    assert ei.value.code == "PENDING_VOUCHERS"
    s.commit()
    # 验证计提坏账准备草稿已制备：20000 × 5% = 1000（Dr 6701 / Cr 1231）
    v = s.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ids["ledger_set_id"],
            Voucher.summary.like("%计提坏账准备%"))
    ).first()
    assert v is not None
    assert v.status == "PUSHED"
    dr = cr = Decimal("0.00")
    codes: set[str] = set()
    for ln in s.scalars(select(VoucherLine).where(VoucherLine.voucher_id == v.id)):
        dr += Decimal(str(ln.debit))
        cr += Decimal(str(ln.credit))
        codes.add(s.get(Account, ln.account_id).code)
    assert dr == cr == Decimal("1000.00")
    assert "6701" in codes and "1231" in codes

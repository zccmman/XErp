"""外币试算平衡（②）：按（科目 × 币种）汇总本月 POSTED 凭证的本币与原币发生额。

数据源：POSTED 凭证的 VoucherLine（与 cash_flow 同一直读口径），不读余额投影
（投影不含原币字段）。只统计带 currency 的明细行——本币科目不混入，
因此本表天然就是「外币户/外汇交易」的专项试算。

口径铁律：
- 本币借/贷 = ln.debit / ln.credit（账面权威值，已含汇率折算后的本币金额）
- 原币借/贷 = ln.foreign_debit / ln.foreign_credit
- 同一科目理论上可跨多币种，按（科目, 币种）分组展示
- 纯增量、零内核状态机改动；可被账账核对独立验证
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, LedgerSet, Period, Voucher, VoucherLine
from kernel.reporting.statements import ReportError

ZERO = Decimal("0.00")


def foreign_trial_balance(
    session: Session, *, ledger_set_id: str, year: int, month: int
) -> dict:
    """按（科目 × 币种）汇总本月 POSTED 凭证明细的本币与原币借/贷。

    仅含带币种（currency 非空）的明细行。返回 rows（按科目编码排序）+ totals。
    """
    ls = session.get(LedgerSet, ledger_set_id)
    if ls is None:
        raise ReportError(f"账套 {ledger_set_id} 不存在")
    func = ls.functional_currency or "CNY"

    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise ReportError(f"期间 {year}-{month:02d} 不存在")

    accounts = {
        a.id: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
        )
    ).all()

    # (科目编码, 科目名, 币种) -> 发生额
    agg: dict[tuple[str, str, str], dict] = {}
    for v in vouchers:
        for ln in session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ).all():
            if not ln.currency:
                continue
            acc = accounts.get(ln.account_id)
            if acc is None:
                continue
            key = (acc.code, acc.name, ln.currency)
            bucket = agg.setdefault(
                key,
                {
                    "debit": ZERO,
                    "credit": ZERO,
                    "foreign_debit": ZERO,
                    "foreign_credit": ZERO,
                },
            )
            bucket["debit"] += Decimal(str(ln.debit))
            bucket["credit"] += Decimal(str(ln.credit))
            bucket["foreign_debit"] += Decimal(str(ln.foreign_debit))
            bucket["foreign_credit"] += Decimal(str(ln.foreign_credit))

    rows = [
        {
            "account_code": code,
            "account_name": name,
            "currency": ccy,
            "debit": str(b["debit"]),
            "credit": str(b["credit"]),
            "foreign_debit": str(b["foreign_debit"]),
            "foreign_credit": str(b["foreign_credit"]),
        }
        for (code, name, ccy), b in sorted(agg.items())
    ]
    total_debit = sum((Decimal(r["debit"]) for r in rows), ZERO)
    total_credit = sum((Decimal(r["credit"]) for r in rows), ZERO)
    total_fdebit = sum((Decimal(r["foreign_debit"]) for r in rows), ZERO)
    total_fcredit = sum((Decimal(r["foreign_credit"]) for r in rows), ZERO)
    return {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "functional_currency": func,
        "rows": rows,
        "totals": {
            "debit": str(total_debit),
            "credit": str(total_credit),
            "foreign_debit": str(total_fdebit),
            "foreign_credit": str(total_fcredit),
        },
        "basis": "仅 POSTED 凭证中带币种的明细行；本币=ln.debit/credit，原币=ln.foreign_debit/credit",
    }


class FxError(ValueError):
    """汇兑损益重估的错误：code / message_zh / details 可被 MCP 层直接消费。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


CENT = Decimal("0.01")


def _fx_period(session: Session, ledger_set_id: str, year: int, month: int) -> Period:
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        raise FxError("PERIOD_NOT_FOUND", f"期间 {year}-{month:02d} 不存在")
    return period


def _cumulative_foreign(
    session: Session, ledger_set_id: str, year: int, month: int
) -> tuple[dict[tuple[str, str, str], dict], Decimal]:
    """累计至期间末（含）的（科目×币种）外币余额（含本币账面值与原币值）。

    口径：全部 POSTED 凭证、期间 <= 目标期间；与 foreign_trial_balance 单月口径不同，
    这里取**累计**外币净额，因为期末重估针对的是期未仍未结算的外币头寸。
    （本币 debit/credit 仍是权威账面值；原币 foreign_debit/credit。）
    """
    accounts = {
        a.id: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    periods = session.scalars(
        select(Period).where(Period.ledger_set_id == ledger_set_id)
    ).all()
    target = max(
        (p for p in periods if (p.year, p.month) <= (year, month)),
        key=lambda p: (p.year, p.month),
        default=None,
    )
    if target is None:
        raise FxError(
            "PERIOD_NOT_FOUND",
            f"账套无 {year}-{month:02d} 及之前的期间，无法累计外币余额",
        )
    due_period_ids = {
        p.id for p in periods if (p.year, p.month) <= (year, month)
    }
    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status == "POSTED",
            Voucher.period_id.in_(due_period_ids),
        )
    ).all()

    # (科目编码, 科目名, 币种) -> 发生额
    agg: dict[tuple[str, str, str], dict] = {}
    for v in vouchers:
        for ln in session.scalars(
            select(VoucherLine).where(VoucherLine.voucher_id == v.id)
        ).all():
            if not ln.currency:
                continue
            acc = accounts.get(ln.account_id)
            if acc is None:
                continue
            key = (acc.code, acc.name, ln.currency)
            bucket = agg.setdefault(
                key,
                {
                    "debit": ZERO,
                    "credit": ZERO,
                    "foreign_debit": ZERO,
                    "foreign_credit": ZERO,
                },
            )
            bucket["debit"] += Decimal(str(ln.debit))
            bucket["credit"] += Decimal(str(ln.credit))
            bucket["foreign_debit"] += Decimal(str(ln.foreign_debit))
            bucket["foreign_credit"] += Decimal(str(ln.foreign_credit))

    return agg


def has_foreign_exposure(
    session: Session, *, ledger_set_id: str, year: int, month: int
) -> bool:
    """累计至期间末是否仍有未结算外币头寸（无需期末汇率，仅判存在性）。

    供账本精灵主动推送判定：有外币业务即提示月结前做汇兑损益重估。
    """
    agg = _cumulative_foreign(session, ledger_set_id, year, month)
    return any(
        (b["foreign_debit"] - b["foreign_credit"]) != ZERO
        for b in agg.values()
    )


def fx_revaluation_posted(
    session: Session, *, ledger_set_id: str, year: int, month: int
) -> bool:
    """本期汇兑损益重估凭证（PUSHED/POSTED）是否已生成，避免重复提醒/重复计提。"""
    from kernel.db.models import Voucher

    return session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.summary.like(f"[汇兑损益重估:{year}-{month:02d}]%"),
            Voucher.status.in_(("PUSHED", "APPROVED", "POSTED")),
        )
    ).first() is not None


def fx_revaluation_draft(
    session: Session,
    *,
    ledger_set_id: str,
    year: int,
    month: int,
    fx_rates: dict,
    fx_gain_loss_account: str = "660304",
    standard: str = "small_business",
) -> dict:
    """汇兑损益期末重估（只读草稿）：对每 (科目, 币种) 累计外币头寸，按期末汇率计提。

    计算：目标本币 = 外币净额 × 期末汇率；调整额 = 目标本币 − 当前账面本币。
    正调整→借该科目/贷汇兑损益（升值）；负调整→贷该科目/借汇兑损益（贬值）。
    所有调整 + 一笔汇兑损益平衡分录，借贷恒等（对冲后净额归零）。

    铁律：只读、只出草稿，**绝不写账**。生成凭证由 create_fx_revaluation_voucher（HITL）执行。
    缺期末汇率的币种跳过并在 notes 标注（Boss 补全汇率后即得完整重估）。
    """
    ls = session.get(LedgerSet, ledger_set_id)
    if ls is None:
        raise FxError("LEDGER_NOT_FOUND", f"账套 {ledger_set_id} 不存在")
    func = ls.functional_currency or "CNY"

    fx_acc = session.scalars(
        select(Account).where(
            Account.ledger_set_id == ledger_set_id,
            Account.code == fx_gain_loss_account,
        )
    ).first()
    if fx_acc is None:
        raise FxError(
            "FX_ACCOUNT_MISSING",
            f"汇兑损益科目 {fx_gain_loss_account} 在账套中不存在",
            {"fx_gain_loss_account": fx_gain_loss_account,
             "hint": "可新增 660304 汇兑损益 叶子科目，或传入已存在的损益科目"},
        )

    _fx_period(session, ledger_set_id, year, month)  # 校验期间存在
    agg = _cumulative_foreign(session, ledger_set_id, year, month)

    rates = {k: Decimal(str(v)) for k, v in (fx_rates or {}).items()}
    lines: list[dict] = []
    notes: list[str] = []
    by_currency: dict[str, Decimal] = {}

    for (code, name, ccy), b in sorted(agg.items()):
        rate = rates.get(ccy)
        if rate is None:
            notes.append(f"{code} {ccy} 缺少期末汇率，已跳过该币种重估")
            continue
        foreign_net = (b["foreign_debit"] - b["foreign_credit"]).quantize(CENT)
        if foreign_net == ZERO:
            continue
        carrying = (b["debit"] - b["credit"]).quantize(CENT)
        target_domestic = (foreign_net * rate).quantize(CENT)
        delta = (target_domestic - carrying).quantize(CENT)
        if abs(delta) < CENT:
            continue
        lines.append({
            "account_code": code,
            "account_name": name,
            "currency": ccy,
            "foreign_net": str(foreign_net),
            "rate": str(rate),
            "carrying_domestic": str(carrying),
            "target_domestic": str(target_domestic),
            "delta": str(delta),
            "side": "debit" if delta > 0 else "credit",
            "amount": str(abs(delta)),
        })
        by_currency[ccy] = by_currency.get(ccy, ZERO) + delta

    total_gain = ZERO
    total_loss = ZERO
    sum_delta = sum((Decimal(l["delta"]) for l in lines), ZERO).quantize(CENT)
    fx_amount = (-sum_delta).quantize(CENT)
    if fx_amount != ZERO:
        lines.append({
            "account_code": fx_gain_loss_account,
            "account_name": fx_acc.name,
            "currency": "",
            "fx_gain_loss": True,
            "side": "debit" if fx_amount > 0 else "credit",
            "amount": str(abs(fx_amount)),
        })
        if fx_amount > 0:
            total_loss += fx_amount
        else:
            total_gain += abs(fx_amount)

    return {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "functional_currency": func,
        "fx_gain_loss_account": fx_gain_loss_account,
        "lines": lines,
        "by_currency": {k: str(v) for k, v in by_currency.items()},
        "total_gain": str(total_gain),
        "total_loss": str(total_loss),
        "net_impact": str(total_gain - total_loss),
        "needs_revaluation": len(lines) > 0,
        "notes": notes,
    }


def create_fx_revaluation_voucher(
    session: Session,
    *,
    ledger_set_id: str,
    year: int,
    month: int,
    fx_rates: dict,
    actor: dict,
    fx_gain_loss_account: str = "660304",
    voucher_date=None,
) -> dict:
    """把 fx_revaluation_draft 的结果落成**待审（PUSHED）凭证**（HITL，绝不自动过账）。

    幂等：同期已生成（idempotency_key 唯一约束）→ ALREADY_RUN。
    凭证处于 PUSHED，需 Boss 审批→过账才生效；Agent 只产草稿。
    """
    from datetime import date as _date
    from sqlalchemy.exc import IntegrityError

    from kernel.events import E
    from kernel.ledger import append_event

    draft = fx_revaluation_draft(
        session, ledger_set_id=ledger_set_id, year=year, month=month,
        fx_rates=fx_rates, fx_gain_loss_account=fx_gain_loss_account,
    )
    if not draft["needs_revaluation"]:
        raise FxError(
            "NO_REVALUATION",
            "本期无需要重估的外币余额（或缺少期末汇率），无需生成重估凭证",
            {"notes": draft["notes"]},
        )

    accounts = {
        a.code: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    period = _fx_period(session, ledger_set_id, year, month)
    v_date = voucher_date or _date(year, month, 28)

    orm_lines: list[VoucherLine] = []
    for idx, l in enumerate(draft["lines"], start=1):
        acc = accounts.get(l["account_code"])
        if acc is None:
            raise FxError(
                "ACCOUNT_NOT_FOUND", f"分录科目 {l['account_code']} 不存在"
            )
        amt = Decimal(str(l["amount"]))
        orm_lines.append(VoucherLine(
            line_no=idx, account_id=acc.id,
            debit=amt if l["side"] == "debit" else ZERO,
            credit=ZERO if l["side"] == "debit" else amt,
        ))

    existing = session.scalars(
        select(Voucher.voucher_no).where(Voucher.ledger_set_id == ledger_set_id)
    ).all()
    seq = max(
        (int(no[2:]) for no in existing
         if no.startswith("记-") and no[2:].isdigit()),
        default=0,
    )
    marker = f"fxrev:{year}{month:02d}"
    voucher = Voucher(
        ledger_set_id=ledger_set_id, period_id=period.id,
        voucher_no=f"记-{seq + 1:04d}", voucher_date=v_date,
        status="PUSHED", summary=f"[汇兑损益重估:{year}-{month:02d}]",
        created_by=str(actor.get("id") or ""), lines=orm_lines,
    )
    voucher.idempotency_key = marker
    session.add(voucher)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise FxError(
            "ALREADY_RUN", f"汇兑损益重估在 {year}-{month:02d} 已生成过"
        ) from None

    append_event(
        session, ledger_set_id=ledger_set_id,
        event_type=E.VOUCHER_PUSHED.value, aggregate_id=marker,
        payload={"voucher_no": voucher.voucher_no,
                 "total_gain": draft["total_gain"],
                 "total_loss": draft["total_loss"]},
        actor=actor,
    )
    session.flush()
    return {
        "voucher": {"id": voucher.id, "voucher_no": voucher.voucher_no,
                    "status": voucher.status, "summary": voucher.summary},
        "total_gain": draft["total_gain"],
        "total_loss": draft["total_loss"],
    }


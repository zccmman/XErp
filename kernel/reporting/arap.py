"""应收应付深化报表（AR/AP 深化）：往来对账单 + 账龄分析。

单一真源（ADR-002）
-------------------
与 ``kernel/adapters/partners.py`` 的 ``partner_balances`` 复用**同一取数口径**——
直接按凭证明细聚合，覆盖 ``PUSHED/APPROVED/POSTED``（在途口径），净额公式与
``Account.direction`` 严格一致。因此：

- 对账单期末余额 == ``partner_balances`` 同客户/供应商的同科目余额；
- 账龄未结清余额之和 == ``partner_balances`` 同口径余额。

不引入任何新投影、不复制配平逻辑——本模块只做"切片 + 排序 + 配比展示"。

账龄配比方法
-----------
未开立 open-item 核销（用户本轮未选核销）时的标准做法：**FIFO 配比**。
按业务发生日顺序，回款/付款冲减最早的未结发票；截至 ``as_of_date`` 仍未冲减的
开票金额按账龄入桶。余额（欠款方向净额）可能为负，表示预付。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from kernel.coa import DEFAULT_AP_ACCOUNTS, DEFAULT_AR_ACCOUNTS
from kernel.db.models import Account, ArapClearing, Voucher, VoucherLine

ZERO = Decimal("0.00")

# 账龄分桶阈值（天），与主流 ERP 对齐：0-30 / 30-60 / 60-90 / 90+
DEFAULT_BUCKETS = (30, 60, 90)

INCLUDED_STATUS = ("PUSHED", "APPROVED", "POSTED")


class ArapError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _resolve_accounts(
    session: Session, ledger_set_id: str, dim_key: str
) -> dict[str, Account]:
    """按 dim_key 取应收/应付科目集合（customer→应收科目，supplier→应付科目）。"""
    if dim_key == "customer":
        prefixes = DEFAULT_AR_ACCOUNTS
    elif dim_key == "supplier":
        prefixes = DEFAULT_AP_ACCOUNTS
    else:
        raise ArapError(
            "BAD_DIM",
            f"仅支持 customer/supplier 维度，收到 {dim_key!r}",
            {"dim_key": dim_key},
        )
    return {
        a.id: a
        for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id,
                or_(*[Account.code.like(p + "%") for p in prefixes]),
            )
        ).all()
    }


def _collect_lines(
    session: Session,
    ledger_set_id: str,
    accounts: dict[str, Account],
    dim_key: str,
    partner: str,
    as_of_date: date,
) -> list[dict]:
    """取该往来单位、在途口径、截至 as_of_date 的全部凭证明细行（含日期/凭证号/摘要/借贷/科目）。"""
    rows = session.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status.in_(INCLUDED_STATUS),
            VoucherLine.account_id.in_(list(accounts)),
            Voucher.voucher_date <= as_of_date,
        )
    ).all()
    out: list[dict] = []
    for line, v in rows:
        dims = line.aux_dims or {}
        if dims.get(dim_key) != partner:
            continue
        out.append(
            {
                "date": v.voucher_date,
                "voucher_no": v.voucher_no,
                "summary": (v.summary or line.summary or ""),
                "debit": Decimal(str(line.debit)),
                "credit": Decimal(str(line.credit)),
                "account_id": line.account_id,
            }
        )
    out.sort(key=lambda r: (r["date"], r["voucher_no"]))
    return out


def _line_delta(line: dict, acc: Account) -> Decimal:
    """该行的『欠款方向』净变动：借项科目取借-贷，贷项科目取贷-借。"""
    if acc.direction == "debit":
        return line["debit"] - line["credit"]
    return line["credit"] - line["debit"]


def statement_of_account(
    session: Session,
    *,
    ledger_set_id: str,
    dim_key: str,
    partner: str,
    as_of_date: date,
    from_date: date | None = None,
) -> dict[str, Any]:
    """往来对账单：某客户/供应商在 [from_date, as_of_date] 的期初、逐笔流水、运行余额、期末。

    - 不传 ``from_date``：期初=0，展示截至 ``as_of_date`` 的全部流水；
    - 传 ``from_date``：期初=该日前累计净欠款，流水仅落在 [from_date, as_of_date]。

    余额一律以『欠款方向』呈现：应收正数 = 客户欠我，应付正数 = 我欠供应商。
    期末余额可直接对其 ``partner_balances`` 同客户/同科目余额复核（单一真源）。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return _empty_statement(dim_key, partner, as_of_date, from_date)
    acc_by_id = accounts
    all_lines = _collect_lines(
        session, ledger_set_id, accounts, dim_key, partner, as_of_date
    )

    opening = ZERO
    if from_date is not None:
        for line in all_lines:
            if line["date"] < from_date:
                opening += _line_delta(line, acc_by_id[line["account_id"]])
    detail_lines = [
        line for line in all_lines if from_date is None or line["date"] >= from_date
    ]

    running = opening
    detail: list[dict] = []
    for line in detail_lines:
        running += _line_delta(line, acc_by_id[line["account_id"]])
        detail.append(
            {
                "date": line["date"].isoformat(),
                "voucher_no": line["voucher_no"],
                "summary": line["summary"],
                "debit": f"{line['debit']:.2f}",
                "credit": f"{line['credit']:.2f}",
                "balance": f"{running:.2f}",
            }
        )
    return {
        "dim_key": dim_key,
        "partner": partner,
        "as_of_date": as_of_date.isoformat(),
        "from_date": from_date.isoformat() if from_date else None,
        "accounts": sorted({a.code for a in accounts.values()}),
        "opening_balance": f"{opening:.2f}",
        "closing_balance": f"{running:.2f}",
        "lines": detail,
        "basis": "与 partner_balances 同一取数口径（在途：PUSHED/APPROVED/POSTED），"
        "期末余额可对其逐客户复核",
    }


def _empty_statement(
    dim_key: str, partner: str, as_of_date: date, from_date: date | None
) -> dict[str, Any]:
    return {
        "dim_key": dim_key,
        "partner": partner,
        "as_of_date": as_of_date.isoformat(),
        "from_date": from_date.isoformat() if from_date else None,
        "accounts": [],
        "opening_balance": "0.00",
        "closing_balance": "0.00",
        "lines": [],
        "basis": "无相关往来科目",
    }


def _zero_totals(bucket_days: tuple[int, ...]) -> dict[str, Any]:
    return {
        "balance": ZERO,
        "outstanding": ZERO,
        "buckets": {
            f"b0_{bucket_days[0]}": ZERO,
            f"b{bucket_days[0]}_{bucket_days[1]}": ZERO,
            f"b{bucket_days[1]}_{bucket_days[2]}": ZERO,
            f"b{bucket_days[2]}_plus": ZERO,
        },
    }


def _bucket(
    open_items: list[list], as_of_date: date, bucket_days: tuple[int, ...]
) -> dict[str, Decimal]:
    b = _zero_totals(bucket_days)["buckets"]
    for d, amt in open_items:
        days = (as_of_date - d).days
        if days <= bucket_days[0]:
            key = f"b0_{bucket_days[0]}"
        elif days <= bucket_days[1]:
            key = f"b{bucket_days[0]}_{bucket_days[1]}"
        elif days <= bucket_days[2]:
            key = f"b{bucket_days[1]}_{bucket_days[2]}"
        else:
            key = f"b{bucket_days[2]}_plus"
        b[key] += amt
    return b


def _is_invoice_side(line: dict) -> bool:
    """余额增加侧 = 发票；减少侧 = 回款/付款。line 为含 direction/debit/credit 的 dict。"""
    if line["direction"] == "debit":
        return line["debit"] > ZERO
    return line["credit"] > ZERO


def _bucket_key(days: int, bucket_days: tuple[int, ...]) -> str:
    if days <= bucket_days[0]:
        return f"b0_{bucket_days[0]}"
    if days <= bucket_days[1]:
        return f"b{bucket_days[0]}_{bucket_days[1]}"
    if days <= bucket_days[2]:
        return f"b{bucket_days[1]}_{bucket_days[2]}"
    return f"b{bucket_days[2]}_plus"


def _collect_arap_lines(
    session: Session, ledger_set_id: str, accounts: dict[str, Account],
    dim_key: str, partner: str | None, as_of_date: date,
) -> list[dict]:
    """取该口径下全部 AR/AP 凭证明细行（含 line_id / 借贷 / 科目方向 / 往来单位）。"""
    rows = session.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status.in_(INCLUDED_STATUS),
            VoucherLine.account_id.in_(list(accounts)),
            Voucher.voucher_date <= as_of_date,
        )
    ).all()
    out: list[dict] = []
    for line, v in rows:
        dims = line.aux_dims or {}
        p = dims.get(dim_key)
        if not p:
            continue
        if partner and p != partner:
            continue
        out.append({
            "line_id": line.id,
            "voucher_no": v.voucher_no,
            "date": v.voucher_date,
            "partner": p,
            "debit": Decimal(str(line.debit)),
            "credit": Decimal(str(line.credit)),
            "account_id": line.account_id,
            "account_code": accounts[line.account_id].code,
            "direction": accounts[line.account_id].direction,
        })
    return out


def _cleared_map(
    session: Session, ledger_set_id: str, dim_key: str, partner: str | None,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """返回 (invoice_line_id→已核销额, payment_line_id→已用额)。"""
    q = select(ArapClearing).where(
        ArapClearing.ledger_set_id == ledger_set_id,
        ArapClearing.dim_key == dim_key,
    )
    if partner:
        q = q.where(ArapClearing.partner == partner)
    inv: dict[str, Decimal] = {}
    pay: dict[str, Decimal] = {}
    for c in session.scalars(q).all():
        inv[c.invoice_line_id] = inv.get(c.invoice_line_id, ZERO) + c.amount
        pay[c.payment_line_id] = pay.get(c.payment_line_id, ZERO) + c.amount
    return inv, pay


def _empty_open_items(dim_key: str, as_of_date: date | None, bucket_days: tuple[int, ...]) -> dict[str, Any]:
    return {
        "dim_key": dim_key,
        "as_of_date": (as_of_date.isoformat() if as_of_date else date.today().isoformat()),
        "bucket_days": list(bucket_days),
        "items": [],
        "totals": _zero_totals(bucket_days),
        "basis": "无往来科目",
    }


def open_items(
    session: Session,
    *,
    ledger_set_id: str,
    dim_key: str,
    partner: str | None = None,
    as_of_date: date | None = None,
    bucket_days: tuple[int, ...] = DEFAULT_BUCKETS,
) -> dict[str, Any]:
    """未清项清单：每张未核销完的发票行 = 发票金额 − 已核销额，按账龄分桶。

    与 SAP open-item 管理对齐——单据级核销，不依赖 FIFO 近似。
    未清项完全由「凭证明细行 + arap_clearing 记录」重建（ADR-002 单一真源）。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return _empty_open_items(dim_key, as_of_date, bucket_days)
    if as_of_date is None:
        as_of_date = date.today()
    lines = _collect_arap_lines(session, ledger_set_id, accounts, dim_key, partner, as_of_date)
    inv_cleared, _ = _cleared_map(session, ledger_set_id, dim_key, partner)

    items: list[dict] = []
    totals = _zero_totals(bucket_days)
    for ln in lines:
        if not _is_invoice_side(ln):  # 仅列发票侧未清项
            continue
        gross = ln["debit"] if ln["direction"] == "debit" else ln["credit"]
        open_amt = gross - inv_cleared.get(ln["line_id"], ZERO)
        if open_amt <= ZERO:
            continue
        days = (as_of_date - ln["date"]).days
        bk = _bucket_key(days, bucket_days)
        items.append({
            "partner": ln["partner"],
            "voucher_no": ln["voucher_no"],
            "invoice_line_id": ln["line_id"],
            "account_code": ln["account_code"],
            "date": ln["date"].isoformat(),
            "original_amount": f"{gross:.2f}",
            "cleared_amount": f"{inv_cleared.get(ln['line_id'], ZERO):.2f}",
            "open_amount": f"{open_amt:.2f}",
            "days": days,
            "bucket": bk,
        })
        totals["balance"] += open_amt
        totals["outstanding"] += open_amt
        totals["buckets"][bk] += open_amt
    items.sort(key=lambda i: (i["partner"], i["date"]))
    totals["balance"] = f"{totals['balance']:.2f}"
    totals["outstanding"] = f"{totals['outstanding']:.2f}"
    totals["buckets"] = {k: f"{v:.2f}" for k, v in totals["buckets"].items()}
    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "bucket_days": list(bucket_days),
        "items": items,
        "totals": totals,
        "basis": "单据级未清项（open = 发票金额 − 已核销）；由凭证 + arap_clearing 重建",
    }


def record_clearing(
    session: Session,
    *,
    ledger_set_id: str,
    dim_key: str,
    partner: str,
    assignments: list[dict],
    source: str = "manual",
    actor: dict | None = None,
) -> list[dict]:
    """记录核销（业务动作，需 HITL 确认后调用）。新增不可变 arap_clearing 记录，
    不改动任何凭证或余额投影；超额校验防止发票/回款被过度核销。

    assignments: [{invoice_line_id, payment_line_id, amount}, ...]
    返回新建记录摘要。调用方负责 commit（与 ingest_event 一致）。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        raise ArapError("NO_ACCOUNTS", f"账套无 {dim_key} 往来科目")
    if not assignments:
        raise ArapError("NO_ASSIGNMENTS", "assignments 不能为空")
    inv_cleared, pay_cleared = _cleared_map(session, ledger_set_id, dim_key, partner)

    line_ids = {a["invoice_line_id"] for a in assignments} | {a["payment_line_id"] for a in assignments}
    fetched = {
        l.id: (l, v)
        for l, v in session.execute(
            select(VoucherLine, Voucher)
            .join(Voucher, VoucherLine.voucher_id == Voucher.id)
            .where(VoucherLine.id.in_(list(line_ids)))
        ).all()
    }
    created_by = (actor or {}).get("id", "") if isinstance(actor, dict) else (actor or "")

    rows: list[ArapClearing] = []
    for a in assignments:
        inv_id = a["invoice_line_id"]
        pay_id = a["payment_line_id"]
        try:
            amt = Decimal(str(a["amount"]))
        except (TypeError, ValueError):
            raise ArapError("BAD_AMOUNT", f"核销金额非法：{a}")
        if amt <= ZERO:
            raise ArapError("BAD_AMOUNT", f"核销金额必须为正：{a}")
        il = fetched.get(inv_id)
        pl = fetched.get(pay_id)
        if il is None:
            raise ArapError("INVOICE_LINE_NOT_FOUND", f"发票行不存在：{inv_id}")
        if pl is None:
            raise ArapError("PAYMENT_LINE_NOT_FOUND", f"回款行不存在：{pay_id}")
        inv_line, inv_v = il
        pay_line, pay_v = pl
        inv_dims = inv_line.aux_dims or {}
        pay_dims = pay_line.aux_dims or {}
        if inv_dims.get(dim_key) != partner or pay_dims.get(dim_key) != partner:
            raise ArapError("PARTNER_MISMATCH", "核销双方必须同为往来单位 " + partner)
        inv_acc = accounts.get(inv_line.account_id)
        pay_acc = accounts.get(pay_line.account_id)
        if inv_acc is None or pay_acc is None:
            raise ArapError("ACCOUNT_NOT_ARAP", "核销双方必须同属往来科目")
        inv_line_d = {"direction": inv_acc.direction, "debit": Decimal(str(inv_line.debit)), "credit": Decimal(str(inv_line.credit))}
        pay_line_d = {"direction": pay_acc.direction, "debit": Decimal(str(pay_line.debit)), "credit": Decimal(str(pay_line.credit))}
        if not _is_invoice_side(inv_line_d):
            raise ArapError("NOT_INVOICE_SIDE", "invoice_line_id 必须指向发票(余额增加侧)")
        if _is_invoice_side(pay_line_d):
            raise ArapError("NOT_PAYMENT_SIDE", "payment_line_id 必须指向回款/付款(余额减少侧)")
        inv_gross = inv_line_d["debit"] if inv_acc.direction == "debit" else inv_line_d["credit"]
        pay_gross = pay_line_d["credit"] if pay_acc.direction == "debit" else pay_line_d["debit"]
        if inv_cleared.get(inv_id, ZERO) + amt > inv_gross + Decimal("0.005"):
            raise ArapError("OVER_CLEAR_INVOICE", f"发票行超额核销：{inv_id}")
        if pay_cleared.get(pay_id, ZERO) + amt > pay_gross + Decimal("0.005"):
            raise ArapError("OVER_APPLY_PAYMENT", f"回款行超额使用：{pay_id}")
        rows.append(ArapClearing(
            ledger_set_id=ledger_set_id, dim_key=dim_key, partner=partner,
            invoice_line_id=inv_id, payment_line_id=pay_id, amount=amt,
            cleared_at=pay_v.voucher_date, source=source, created_by=created_by,
        ))
        inv_cleared[inv_id] = inv_cleared.get(inv_id, ZERO) + amt
        pay_cleared[pay_id] = pay_cleared.get(pay_id, ZERO) + amt

    session.add_all(rows)
    session.flush()
    return [
        {"id": r.id, "invoice_line_id": r.invoice_line_id,
         "payment_line_id": r.payment_line_id, "amount": f"{r.amount:.2f}",
         "cleared_at": r.cleared_at.isoformat(), "source": r.source}
        for r in rows
    ]


def propose_clearing(
    session: Session,
    *,
    ledger_set_id: str,
    dim_key: str,
    partner: str | None = None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """AI 核销草稿（只读，不落库）：对未指定核销的回款，按「金额优先 + 最旧优先(FIFO 兜底)」
    匹配未清发票，输出建议 assignments，供 Boss 确认。Phase C 收款自动匹配的种子。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return {"dim_key": dim_key, "partner": partner, "proposals": [],
                "basis": "无往来科目"}
    if as_of_date is None:
        as_of_date = date.today()
    lines = _collect_arap_lines(session, ledger_set_id, accounts, dim_key, partner, as_of_date)
    inv_cleared, pay_cleared = _cleared_map(session, ledger_set_id, dim_key, partner)

    open_inv: list[dict] = []
    for ln in lines:
        if not _is_invoice_side(ln):
            continue
        gross = ln["debit"] if ln["direction"] == "debit" else ln["credit"]
        rem = gross - inv_cleared.get(ln["line_id"], ZERO)
        if rem > ZERO:
            open_inv.append({"line_id": ln["line_id"], "date": ln["date"], "remaining": rem, "partner": ln["partner"]})
    open_inv.sort(key=lambda x: x["date"])

    avail_pay: list[dict] = []
    for ln in lines:
        if _is_invoice_side(ln):
            continue
        pay_amt = ln["credit"] if ln["direction"] == "debit" else ln["debit"]
        rem = pay_amt - pay_cleared.get(ln["line_id"], ZERO)
        if rem > ZERO:
            avail_pay.append({"line_id": ln["line_id"], "date": ln["date"], "remaining": rem, "partner": ln["partner"]})
    avail_pay.sort(key=lambda x: x["date"])

    proposals: list[dict] = []
    pi = 0
    for pay in avail_pay:
        avail = pay["remaining"]
        while avail > ZERO and pi < len(open_inv):
            inv = open_inv[pi]
            if inv["remaining"] <= ZERO:
                pi += 1
                continue
            take = min(avail, inv["remaining"])
            proposals.append({
                "partner": pay["partner"],
                "invoice_line_id": inv["line_id"],
                "invoice_date": inv["date"].isoformat(),
                "payment_line_id": pay["line_id"],
                "payment_date": pay["date"].isoformat(),
                "amount": f"{take:.2f}",
            })
            inv["remaining"] -= take
            avail -= take
    return {
        "dim_key": dim_key,
        "partner": partner,
        "proposals": proposals,
        "basis": "AI 草稿（FIFO 兜底匹配：最旧发票优先）；只读不落库，需 Boss 确认",
    }


def _aging_clearing_aware(
    session: Session, ledger_set_id: str, dim_key: str,
    accounts: dict[str, Account], as_of_date: date, bucket_days: tuple[int, ...],
) -> dict[str, Any]:
    """已启用单据级核销时的账龄：按未清发票金额（发票额 − 已核销）分桶，修正 FIFO 漂移。"""
    lines = _collect_arap_lines(session, ledger_set_id, accounts, dim_key, None, as_of_date)
    inv_cleared, _ = _cleared_map(session, ledger_set_id, dim_key, None)
    by_partner: dict[str, list[list]] = {}
    for ln in lines:
        if not _is_invoice_side(ln):
            continue
        gross = ln["debit"] if ln["direction"] == "debit" else ln["credit"]
        open_amt = gross - inv_cleared.get(ln["line_id"], ZERO)
        if open_amt <= ZERO:
            continue
        by_partner.setdefault(ln["partner"], []).append([ln["date"], open_amt])

    items: list[dict] = []
    totals = _zero_totals(bucket_days)
    for partner, open_items_list in sorted(by_partner.items()):
        balance = ZERO
        outstanding = ZERO
        buckets = _zero_totals(bucket_days)["buckets"]
        for d, amt in open_items_list:
            balance += amt
            outstanding += amt
            buckets[_bucket_key((as_of_date - d).days, bucket_days)] += amt
        items.append({
            "partner": partner,
            "balance": f"{balance:.2f}",
            "outstanding": f"{outstanding:.2f}",
            "buckets": {k: f"{v:.2f}" for k, v in buckets.items()},
        })
        totals["balance"] += balance
        totals["outstanding"] += outstanding
        for k in buckets:
            totals["buckets"][k] += buckets[k]
    totals["balance"] = f"{totals['balance']:.2f}"
    totals["outstanding"] = f"{totals['outstanding']:.2f}"
    totals["buckets"] = {k: f"{v:.2f}" for k, v in totals["buckets"].items()}
    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "bucket_days": list(bucket_days),
        "items": items,
        "totals": totals,
        "basis": "单据级未清项口径（核销记录由凭证 + arap_clearing 重建）；存在核销记录时启用，修正 FIFO 漂移",
    }


def aging_analysis(
    session: Session,
    *,
    ledger_set_id: str,
    dim_key: str,
    as_of_date: date | None = None,
    bucket_days: tuple[int, ...] = DEFAULT_BUCKETS,
) -> dict[str, Any]:
    """账龄分析：按往来单位把未结清欠款按逾期天数分桶（0-30/30-60/60-90/90+）。

    采用 **FIFO 配比**（不开立 open-item 核销时的标准做法）：按业务发生日顺序，
    回款/付款冲减最早的未结发票；截止 ``as_of_date`` 仍未冲减的开票金额按账龄入桶。
    未结清余额之和 == ``partner_balances`` 同口径余额（单一真源）。
    """
    if as_of_date is None:
        as_of_date = date.today()
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return {
            "dim_key": dim_key,
            "as_of_date": as_of_date.isoformat(),
            "bucket_days": list(bucket_days),
            "items": [],
            "totals": _zero_totals(bucket_days),
        }

    # 已启用单据级核销 → 改用未清项口径（修正 FIFO 漂移）；否则保持既有 FIFO 兼容。
    has_clearing = session.scalars(
        select(ArapClearing.id)
        .where(ArapClearing.ledger_set_id == ledger_set_id, ArapClearing.dim_key == dim_key)
        .limit(1)
    ).first() is not None
    if has_clearing:
        return _aging_clearing_aware(session, ledger_set_id, dim_key, accounts, as_of_date, bucket_days)

    rows = session.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status.in_(INCLUDED_STATUS),
            VoucherLine.account_id.in_(list(accounts)),
            Voucher.voucher_date <= as_of_date,
        )
    ).all()

    by_partner: dict[str, list[dict]] = {}
    for line, v in rows:
        dims = line.aux_dims or {}
        p = dims.get(dim_key)
        if not p:
            continue
        by_partner.setdefault(p, []).append(
            {
                "date": v.voucher_date,
                "debit": Decimal(str(line.debit)),
                "credit": Decimal(str(line.credit)),
                "account_id": line.account_id,
            }
        )

    items: list[dict] = []
    totals = _zero_totals(bucket_days)

    for partner, plines in sorted(by_partner.items()):
        open_items: list[list] = []  # [date, amount]
        balance = ZERO
        for ln in sorted(plines, key=lambda r: r["date"]):
            acc = accounts[ln["account_id"]]
            if acc.direction == "debit":
                inc, dec = ln["debit"], ln["credit"]
            else:
                inc, dec = ln["credit"], ln["debit"]
            # 欠款方向净额：inc 增加欠款，dec 减少欠款（可冲成负=预付）
            balance += inc - dec
            if inc > 0:
                open_items.append([ln["date"], inc])
            # 用回款/付款 FIFO 冲减最早的开票，剩余 open_items 用于账龄分桶
            for it in open_items:
                if dec <= 0:
                    break
                take = min(it[1], dec)
                it[1] -= take
                dec -= take
            open_items = [it for it in open_items if it[1] > 0]
        buckets = _bucket(open_items, as_of_date, bucket_days)
        outstanding = sum((it[1] for it in open_items), ZERO)
        items.append(
            {
                "partner": partner,
                "balance": f"{balance:.2f}",
                "outstanding": f"{outstanding:.2f}",
                "buckets": {k: f"{v:.2f}" for k, v in buckets.items()},
            }
        )
        totals["balance"] += balance
        totals["outstanding"] += outstanding
        for k in buckets:
            totals["buckets"][k] += buckets[k]

    totals["balance"] = f"{totals['balance']:.2f}"
    totals["outstanding"] = f"{totals['outstanding']:.2f}"
    totals["buckets"] = {k: f"{v:.2f}" for k, v in totals["buckets"].items()}
    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "bucket_days": list(bucket_days),
        "items": items,
        "totals": totals,
        "basis": "FIFO 配比（不依赖 open-item 核销），未结清余额可对其 partner_balances 复核",
    }


# ============================================================ Phase C：AI 收款自动匹配（G4）
# 承接 Phase A 的 open_items / record_clearing，是 propose_clearing（FIFO 兜底）的**智能升级**。
# 铁律（事件溯源 + ADR-002）：本模块**只读**，不新增任何投影；匹配结果由 Boss 确认后
# 经 record_clearing 落库（HITL，推送 ≠ 执行）。所有取数复用 open_items / arap_clearing。


def _normalize_ref(token: str) -> str:
    return re.sub(r"[\s\-_.]", "", token).upper()


def _parse_invoice_refs(reference: str | None) -> list[str]:
    """从回款备注里抽取候选发票号（归一化大写、去分隔符）。

    识别：① 字母前缀 + 数字（可含 -_. 分隔），如 INV-2026-001 / FP001 / AB-12345；
          ② 独立纯数字发票号（4 位及以上，前后非字母/数字/分隔符，避免误吞金额小数）。
    重复归一并保序。
    """
    if not reference:
        return []
    refs: list[str] = []
    for m in re.finditer(r"[A-Za-z]{1,4}(?:[ \-_.]?\d+)+", reference, re.IGNORECASE):
        refs.append(_normalize_ref(m.group(0)))
    for m in re.finditer(r"(?<![A-Za-z\d\-_.])(\d{4,})(?![A-Za-z\d\-_.])", reference):
        refs.append(_normalize_ref(m.group(1)))
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _fuzzy_best(payer: str, candidates: list[str]) -> tuple[str | None, float]:
    """在候选往来单位名里模糊匹配 payer，返回 (最佳名, 相似度 0-1)。

    兼顾两种信号：① difflib 编辑距离比；② 子串/包含（付款方名是客户名的
    子串，如「示例科技」∈「北京示例科技有限公司」）→ 视为强匹配（0.9）。
    """
    if not payer or not candidates:
        return None, 0.0
    p = payer.strip()
    best, score = None, 0.0
    for c in candidates:
        s = SequenceMatcher(None, p, c).ratio()
        if p in c or c in p:  # 子串/包含加成
            s = max(s, 0.9)
        if s > score:
            best, score = c, s
    return best, score


def unmatched_receipts(
    session: Session, *, ledger_set_id: str, dim_key: str = "customer",
    partner: str | None = None, as_of_date: date | None = None,
) -> dict[str, Any]:
    """待匹配收款/付款清单（只读）：回款/付款侧尚未完全核销的行 + 剩余可匹配额。

    与 open_items 同一取数口径（凭证 + arap_clearing 重建，ADR-002）。
    用途：① sprite_push 主动提醒「有几笔回款还没匹配发票」；
          ② Boss 一键查待匹配回款，再调 arap_propose_receipt_match 出匹配方案。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return {"dim_key": dim_key, "items": [],
                "totals": {"count": 0, "remaining": "0.00"},
                "basis": "无往来科目"}
    if as_of_date is None:
        as_of_date = date.today()
    lines = _collect_arap_lines(session, ledger_set_id, accounts, dim_key, partner, as_of_date)
    _, pay_cleared = _cleared_map(session, ledger_set_id, dim_key, partner)
    items: list[dict] = []
    total_rem = ZERO
    for ln in lines:
        if _is_invoice_side(ln):
            continue  # 仅回款/付款侧
        gross = ln["credit"] if ln["direction"] == "debit" else ln["debit"]
        rem = gross - pay_cleared.get(ln["line_id"], ZERO)
        if rem <= ZERO:
            continue
        items.append({
            "partner": ln["partner"],
            "payment_line_id": ln["line_id"],
            "voucher_no": ln["voucher_no"],
            "date": ln["date"].isoformat(),
            "amount": f"{gross:.2f}",
            "cleared_amount": f"{pay_cleared.get(ln['line_id'], ZERO):.2f}",
            "remaining": f"{rem:.2f}",
        })
        total_rem += rem
    items.sort(key=lambda i: (i["partner"], i["date"]))
    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "items": items,
        "totals": {"count": len(items), "remaining": f"{total_rem:.2f}"},
        "basis": "回款/付款侧未清额 = 行额 − 已用额（arap_clearing 重建）；仅列未完全匹配的行",
    }


def propose_receipt_match(
    session: Session, *, ledger_set_id: str, dim_key: str = "customer",
    payment_line_id: str | None = None, receipt: dict | None = None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """AI 收款自动匹配（只读草稿，不落库）：把一笔回款智能匹配到未清发票。

    承接 Phase A 的 open_items / record_clearing，是 propose_clearing（FIFO 兜底）的
    **智能升级**——在 FIFO 之外叠加多信号匹配 + 可解释置信度：
      · 备注发票号命中（最高置信）：从回款备注解析发票号，精确指向对应发票；
      · 金额精确匹配：单张发票未清额 == 回款，或多张发票合计 == 回款；
      · 付款方名称模糊匹配：限定候选客户、提升置信；
      · 部分核销 / 多付预警：回款 < / > 未清合计时给明确提示；
      · 退化 FIFO 兜底：无备注无精确匹配时退回最旧优先（与 propose_clearing 一致）。
    每条匹配都带 confidence（0-1）与 rationale（中文可解释），便于 Boss 信任并一键确认。

    输入二选一：
      · payment_line_id：已入账的回款行 id（推荐，匹配结果可直接喂 record_clearing）；
      · receipt：自由文本收款 {amount, date, reference, payer}（银行导入/AI 解析场景）；
        此时 payment_line_id 为 None，需先据建议分录入账该回款再回填匹配。
    输出 proposals：[{invoice_line_id, payment_line_id, voucher_no, invoice_date,
                     amount, confidence, rationale, signals}] + receipt 摘要 /
                     matched_amount / unmatched_amount / overpayment / partner / advice / basis。
    终态动作（record_clearing，HITL）由 Boss 确认后调用，XErp 不自动落账。
    """
    if payment_line_id is None and not receipt:
        raise ArapError("NEED_RECEIPT", "必须提供 payment_line_id 或 receipt 之一")

    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return {"dim_key": dim_key, "partner": None, "proposals": [],
                "receipt": {}, "matched_amount": "0.00",
                "unmatched_amount": "0.00", "overpayment": False,
                "needs_recording": payment_line_id is None,
                "basis": "无往来科目"}

    if as_of_date is None:
        as_of_date = date.today()

    resolved_partner: str | None = None
    receipt_amount: Decimal | None = None
    receipt_date = as_of_date
    reference = (receipt or {}).get("reference") if isinstance(receipt, dict) else None
    payer = (receipt or {}).get("payer") if isinstance(receipt, dict) else None

    if payment_line_id is not None:
        _, pay_cleared = _cleared_map(session, ledger_set_id, dim_key, None)
        row = session.execute(
            select(VoucherLine, Voucher)
            .join(Voucher, VoucherLine.voucher_id == Voucher.id)
            .where(VoucherLine.id == payment_line_id)
        ).first()
        if row is None:
            raise ArapError("PAYMENT_LINE_NOT_FOUND", f"回款行不存在：{payment_line_id}")
        line, v = row
        acc = accounts.get(line.account_id)
        if acc is None:
            raise ArapError("NOT_ARAP_LINE", "回款行不属于应收/应付科目")
        dims = line.aux_dims or {}
        resolved_partner = dims.get(dim_key)
        if resolved_partner is None:
            raise ArapError("NO_PARTNER", "回款行无往来单位，无法匹配")
        gross = (Decimal(str(line.credit)) if acc.direction == "debit"
                 else Decimal(str(line.debit)))
        receipt_amount = gross - pay_cleared.get(payment_line_id, ZERO)
        if receipt_amount <= ZERO:
            raise ArapError("RECEIPT_FULLY_CLEARED", f"该回款已完全匹配：{payment_line_id}")
        receipt_date = v.voucher_date
    else:
        try:
            receipt_amount = Decimal(str((receipt or {}).get("amount")))
        except (TypeError, ValueError, AttributeError):
            raise ArapError("BAD_AMOUNT", f"回款金额非法：{receipt}")
        if receipt_amount <= ZERO:
            raise ArapError("BAD_AMOUNT", "回款金额必须为正")
        if isinstance(receipt, dict) and receipt.get("date"):
            receipt_date = date.fromisoformat(receipt["date"])

    # 取候选未清发票（按 resolved_partner 或 payer 模糊匹配收敛）
    oi = open_items(session, ledger_set_id=ledger_set_id, dim_key=dim_key,
                    partner=resolved_partner, as_of_date=as_of_date)
    line_ids = [it["invoice_line_id"] for it in oi.get("items", [])]
    summ: dict[str, str] = {}
    if line_ids:  # 取发票凭证摘要（含业务发票号，如「销售开票 INV-2026-001」）
        for lid, sm in session.execute(
            select(VoucherLine.id, Voucher.summary)
            .join(Voucher, VoucherLine.voucher_id == Voucher.id)
            .where(VoucherLine.id.in_(line_ids))
        ).all():
            summ[lid] = sm or ""
    open_inv: list[dict] = []
    for it in oi.get("items", []):
        open_inv.append({
            "invoice_line_id": it["invoice_line_id"],
            "voucher_no": it["voucher_no"],
            "date": date.fromisoformat(it["date"]),
            "open_amount": Decimal(it["open_amount"]),
            "partner": it["partner"],
            "summary": summ.get(it["invoice_line_id"], ""),
        })

    # 付款方名称模糊匹配（仅自由文本 receipt 且未解析出 partner 时）
    if resolved_partner is None and payer:
        cands = sorted({i["partner"] for i in open_inv})
        best, score = _fuzzy_best(payer, cands)
        if best is not None and score >= 0.6:
            resolved_partner = best
            open_inv = [i for i in open_inv if i["partner"] == best]

    if not open_inv:
        return {
            "dim_key": dim_key, "partner": resolved_partner,
            "receipt": {"amount": f"{receipt_amount:.2f}",
                        "date": receipt_date.isoformat(),
                        "reference": reference, "payer": payer,
                        "payment_line_id": payment_line_id},
            "proposals": [], "matched_amount": "0.00",
            "unmatched_amount": f"{receipt_amount:.2f}", "overpayment": False,
            "needs_recording": payment_line_id is None,
            "basis": "无未清发票可匹配（该客户无欠款或回款方不匹配）",
        }

    proposals, matched, leftover = _match_engine(
        receipt_amount, open_inv, reference, payment_line_id,
    )
    overpayment = leftover > ZERO
    confs = [p["confidence"] for p in proposals] or [0.0]
    overall = (sum(confs) / len(confs)) if confs else 0.0
    advice = ("高置信，建议直接确认" if overall >= 0.9
              else "中置信，建议复核后确认" if overall >= 0.7
              else "低置信（FIFO 兜底），务必人工复核")

    return {
        "dim_key": dim_key,
        "partner": resolved_partner or open_inv[0]["partner"],
        "receipt": {
            "amount": f"{receipt_amount:.2f}",
            "date": receipt_date.isoformat(),
            "reference": reference, "payer": payer,
            "payment_line_id": payment_line_id,
        },
        "proposals": proposals,
        "matched_amount": f"{matched:.2f}",
        "unmatched_amount": f"{leftover:.2f}",
        "overpayment": overpayment,
        "needs_recording": payment_line_id is None,
        "confidence_overall": round(overall, 2),
        "advice": advice,
        "basis": ("AI 多信号匹配（备注发票号/金额精确/名称模糊/部分-多付/FIFO 兜底）；"
                  "只读草稿，确认后由 record_clearing 落库（HITL）"),
    }


def _match_engine(
    receipt_amount: Decimal, open_inv: list[dict], reference, payment_line_id,
) -> tuple[list[dict], Decimal, Decimal]:
    """核心匹配（纯函数、确定性、可测试）：返回 (proposals, 已匹配额, 剩余额)。"""
    inv_sorted = sorted(open_inv, key=lambda i: i["date"])
    ref_tokens = set(_parse_invoice_refs(reference)) if reference else set()
    ref_hits = ([i for i in inv_sorted
                 if (_normalize_ref(i["voucher_no"]) in ref_tokens
                     or any(tok in _normalize_ref(i.get("summary") or "")
                            for tok in ref_tokens))]
                if ref_tokens else [])

    proposals: list[dict] = []
    avail = receipt_amount
    used_ids: set[str] = set()

    def add(inv: dict, amt: Decimal, conf: float, rationale: str, signals: list[str]) -> None:
        proposals.append({
            "invoice_line_id": inv["invoice_line_id"],
            "voucher_no": inv["voucher_no"],
            "invoice_date": inv["date"].isoformat(),
            "amount": f"{amt:.2f}",
            "confidence": conf,
            "rationale": rationale,
            "signals": signals,
            "payment_line_id": payment_line_id,
        })
        inv["open_amount"] -= amt
        used_ids.add(inv["invoice_line_id"])

    # —— 备注发票号命中（最高置信）——
    if ref_hits:
        ref_total = sum(i["open_amount"] for i in ref_hits)
        if ref_total == avail:
            for i in ref_hits:
                add(i, i["open_amount"], 0.99,
                    f"回款备注含发票号 {i['voucher_no']} 精确命中，金额相等",
                    ["reference_exact"])
            return proposals, avail, ZERO
        if ref_total < avail:
            for i in ref_hits:
                add(i, i["open_amount"], 0.95,
                    f"回款备注含发票号 {i['voucher_no']} 命中，优先核销",
                    ["reference"])
            avail -= ref_total  # 余下金额继续走精确/FIFO
        else:  # ref_total > avail：按最旧优先部分核销命中的发票
            for i in sorted(ref_hits, key=lambda x: x["date"]):
                if avail <= ZERO:
                    break
                take = min(avail, i["open_amount"])
                add(i, take, 0.9,
                    f"回款备注含发票号 {i['voucher_no']} 命中，回款不足额按最旧优先部分核销",
                    ["reference", "partial"])
                avail -= take
            return proposals, receipt_amount - avail, avail

    # —— 金额精确匹配（无备注或备注已消化余量）——
    for i in inv_sorted:  # 单张精确
        if i["invoice_line_id"] not in used_ids and i["open_amount"] == avail:
            add(i, avail, 0.95, "单一发票未清额精确等于回款金额", ["exact_amount"])
            return proposals, avail, ZERO
    pool = [i for i in inv_sorted if i["invoice_line_id"] not in used_ids]
    if len(pool) <= 20:  # 多张合计精确（小集合穷举）
        found = _subset_sum(pool, avail)
        if found:
            for i in found:
                add(i, i["open_amount"], 0.9,
                    "多张发票未清额合计精确等于回款金额", ["exact_sum"])
            return proposals, avail, ZERO

    # —— 部分 / 多付 / FIFO 兜底 ——
    total_open = sum(i["open_amount"] for i in inv_sorted
                     if i["invoice_line_id"] not in used_ids)
    if avail >= total_open and total_open > ZERO:
        for i in inv_sorted:
            if i["invoice_line_id"] in used_ids or i["open_amount"] <= ZERO:
                continue
            add(i, i["open_amount"], 0.6,
                "回款≥全部未清发票合计，全额核销（疑似多付/预付，请人工确认）",
                ["overpay_risk", "fifo"])
        return proposals, total_open, avail - total_open
    for i in inv_sorted:  # 部分核销：最旧优先
        if avail <= ZERO:
            break
        if i["invoice_line_id"] in used_ids or i["open_amount"] <= ZERO:
            continue
        take = min(avail, i["open_amount"])
        add(i, take, 0.6, "无备注/无精确匹配，退回 FIFO 兜底（最旧发票优先）",
            ["fifo"])
        avail -= take
    return proposals, receipt_amount - avail, avail


def _subset_sum(items: list[dict], target: Decimal) -> list[dict] | None:
    """小集合精确子集和：返回和为 target 的发票子集；无解返回 None。
    调用方已保证 items 规模（≤20），DFS + 早停足够。"""
    n = len(items)

    def dfs(idx: int, running: Decimal, chosen: list[int]):
        if running == target:
            return [items[k] for k in chosen]
        if idx >= n or running > target:
            return None
        r1 = dfs(idx + 1, running + items[idx]["open_amount"], chosen + [idx])
        if r1 is not None:
            return r1
        return dfs(idx + 1, running, chosen)

    return dfs(0, ZERO, [])


# ============================================================ Phase D（G9）：子账↔总账对账
# 控制科目（应收 1122 / 应付 2202）总额必须等于各往来单位（客户/供应商）明细余额之和。
# 只读、复用 _resolve_accounts / _line_delta / INCLUDED_STATUS——与往来对账单、账龄同一取数口径
# （ADR-002 单一真源），不引入新投影、不复制配平逻辑。


def subledger_gl_reconcile(
    session: Session, *, ledger_set_id: str, dim_key: str,
    as_of_date: date | None = None, tolerance: Decimal = Decimal("0.00"),
) -> dict[str, Any]:
    """子账↔总账对账（只读）：应收/应付控制科目 vs 按往来单位拆分的明细余额。

    控制科目（1122/2202）的借贷净额合计（control_total），应等于「逐张凭证明细按
    customer/supplier 维度拆分后」的往来单位余额之和（subledger_total）。

    差异（difference = control_total − subledger_total）即「入了控制科目但未挂往来单位」
    的分录——典型的子账↔总账失配（如一张 J/E 直接借 1122 却漏填客户）。

    返回 {dim_key, as_of_date, accounts, control_total, subledger_total, difference,
          ok, unassigned_total, unassigned_lines, unassigned_count, partner_count,
          partners, by_account, basis}。ok 表示对账一致（|difference| <= tolerance）。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return {
            "dim_key": dim_key,
            "as_of_date": (as_of_date.isoformat()
                           if as_of_date else date.today().isoformat()),
            "accounts": [],
            "control_total": "0.00",
            "subledger_total": "0.00",
            "difference": "0.00",
            "ok": True,
            "unassigned_total": "0.00",
            "unassigned_lines": [],
            "unassigned_count": 0,
            "partner_count": 0,
            "partners": [],
            "by_account": {},
            "basis": "无往来科目，无需对账",
        }
    if as_of_date is None:
        as_of_date = date.today()

    rows = session.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status.in_(INCLUDED_STATUS),
            VoucherLine.account_id.in_(list(accounts)),
            Voucher.voucher_date <= as_of_date,
        )
    ).all()

    control_total = ZERO
    subledger_total = ZERO
    by_partner: dict[str, Decimal] = {}
    by_account: dict[str, Decimal] = {}
    unassigned_lines: list[dict] = []
    for line, v in rows:
        acc = accounts[line.account_id]
        delta = _line_delta(
            {"debit": Decimal(str(line.debit)), "credit": Decimal(str(line.credit))},
            acc,
        )
        control_total += delta
        by_account[acc.code] = by_account.get(acc.code, ZERO) + delta
        dims = line.aux_dims or {}
        partner = dims.get(dim_key)
        if partner:
            subledger_total += delta
            by_partner[partner] = by_partner.get(partner, ZERO) + delta
        else:
            unassigned_lines.append({
                "voucher_no": v.voucher_no,
                "date": v.voucher_date.isoformat(),
                "account_code": acc.code,
                "debit": f"{line.debit:.2f}",
                "credit": f"{line.credit:.2f}",
                "summary": (v.summary or line.summary or ""),
            })

    difference = control_total - subledger_total
    ok = abs(difference) <= tolerance
    partners = [
        {"partner": p, "balance": f"{b:.2f}"}
        for p, b in sorted(by_partner.items(), key=lambda kv: -abs(kv[1]))
    ]
    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "accounts": sorted({a.code for a in accounts.values()}),
        "control_total": f"{control_total:.2f}",
        "subledger_total": f"{subledger_total:.2f}",
        "difference": f"{difference:.2f}",
        "ok": ok,
        "unassigned_total": f"{difference:.2f}",
        "unassigned_lines": unassigned_lines[:50],
        "unassigned_count": len(unassigned_lines),
        "partner_count": len(partners),
        "partners": partners,
        "by_account": {k: f"{v:.2f}" for k, v in by_account.items()},
        "basis": ("控制科目(1122/2202)借贷净额合计 == 各往来单位明细余额之和；"
                  "差异 = 入控制科目但未挂往来单位的分录（如漏填客户的 J/E）。只读，守 ADR-002。"),
    }

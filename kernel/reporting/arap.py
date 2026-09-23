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

from datetime import date
from decimal import Decimal
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

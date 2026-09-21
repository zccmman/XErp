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
from kernel.db.models import Account, Voucher, VoucherLine

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

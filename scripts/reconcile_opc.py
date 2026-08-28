"""P0-15 对账：用独立于内核的纯 Decimal 重算期末余额，与系统投影逐行比对。

用法: python scripts/reconcile_opc.py [账套名]
输出: 控制台对账表 + docs/dogfood_reconcile.md
"""

from __future__ import annotations

import io
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp-server"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kernel.db.models import Account, Balance, LedgerSet, Period  # noqa: E402

DB = f"sqlite:///{REPO / 'ledgeros_dev.db'}"


def direction(code: str) -> str:
    """余额方向：资产/成本/费用=借，负债/权益/收入=贷。"""
    head = code[0]
    if head == "1":
        return "借"
    if code.startswith(("6001", "6051", "6301")):
        return "贷"
    if head == "6":  # 6401 主营业务成本 / 6601 销售费用 / 6602 管理费用
        return "借"
    return "贷"


def main(name: str = "OPC 一人公司") -> int:
    eng = create_engine(DB)
    with Session(eng) as s:
        ls = s.scalars(select(LedgerSet).where(LedgerSet.name == name)).one()
        period = s.scalars(
            select(Period).where(
                Period.ledger_set_id == ls.id, Period.year == 2026, Period.month == 8
            )
        ).one()
        accounts = {a.id: a for a in s.scalars(select(Account))}

        # 系统投影
        sys_map = {
            accounts[b.account_id].code: (Decimal(str(b.debit_total)),
                                          Decimal(str(b.credit_total)))
            for b in s.scalars(select(Balance).where(Balance.period_id == period.id))
        }

    # 独立重算：直接读凭证分录（不读投影表）
    with Session(eng) as s:
        from kernel.db.models import Voucher, VoucherLine

        vouchers = s.scalars(
            select(Voucher).where(Voucher.ledger_set_id == ls.id, Voucher.status == "POSTED")
        ).all()
        manual: dict[str, list[Decimal]] = {}
        for v in vouchers:
            for ln in s.scalars(
                select(VoucherLine).where(VoucherLine.voucher_id == v.id)
            ):
                code = accounts[ln.account_id].code
                d, c = manual.get(code, [Decimal("0"), Decimal("0")])
                manual[code] = [
                    d + Decimal(str(ln.debit)),
                    c + Decimal(str(ln.credit)),
                ]

    rows = []
    diff = []
    for code in sorted(set(sys_map) | set(manual)):
        sd, sc = sys_map.get(code, (Decimal("0"), Decimal("0")))
        md, mc = manual.get(code, (Decimal("0"), Decimal("0")))
        ok = (sd, sc) == (md, mc)
        net = md - mc if direction(code) == "借" else mc - md
        rows.append((code, accounts_by_code(code), sd, sc, md, mc, net, "✅" if ok else "❌"))
        if not ok:
            diff.append(code)

    # 输出
    lines = [
        f"# {name} · 2026-08 对账表（系统投影 vs 独立重算）",
        "",
        "| 科目 | 名称 | 系统借 | 系统贷 | 重算借 | 重算贷 | 期末余额 | 方向 | 一致 |",
        "|---|---|---:|---:|---:|---:|---:|:--:|:--:|",
    ]
    for code, nm, sd, sc, md, mc, net, ok in rows:
        lines.append(
            f"| {code} | {nm} | {sd:,.2f} | {sc:,.2f} | {md:,.2f} | {mc:,.2f} | "
            f"{net:,.2f} | {direction(code)} | {ok} |"
        )
    tot_d = sum(md for *_, md, mc, net, ok in [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows])
    lines += ["", f"**差异科目：{diff if diff else '无 ✅'}**"]
    md_path = REPO / "docs" / "dogfood_reconcile.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"凭证数(POSTED): {len(vouchers)}  科目行: {len(rows)}")
    print("\n".join(lines[-len(rows) - 1 :]))
    print(f"\n对账表 → {md_path.relative_to(REPO)}")
    return 0 if not diff else 2


def accounts_by_code(code: str) -> str:
    eng = create_engine(DB)
    with Session(eng) as s:
        a = s.scalars(select(Account).where(Account.code == code)).first()
        return a.name if a else "?"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "OPC 一人公司"))

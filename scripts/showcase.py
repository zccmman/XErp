"""演示账套一键生成：建账 → 期初 → 六张凭证走完审批记账 → 输出怀旧口径结果。

用法：
    python scripts/showcase.py                 # 默认生成到 .showcase.db
    python scripts/showcase.py --db <sqlite路径>

跑完后用下面的命令起 Web 看效果（脚本会打印完整命令）：
    XERP_DB=... XERP_WEB_PASSWORD=demo123 python -m kernel.webapp
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kernel.authz import grant_ledger_role  # noqa: E402
from kernel.classic import (  # noqa: E402
    precheck_close,
    status_zh,
)
from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Account, LedgerSet, Period, Subject, Voucher  # noqa: E402
from kernel.opening import import_opening_balances  # noqa: E402
from kernel.posting import post_voucher  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402
from kernel.state import transition  # noqa: E402
from kernel.voucher_wizard import create_draft_voucher  # noqa: E402

COMPANY = "演示科技有限公司"

# 期初：股东投入 50 万
OPENING = [
    {"account_code": "1002", "debit": "500000", "credit": ""},
    {"account_code": "1001", "debit": "10000", "credit": ""},
    {"account_code": "3001", "debit": "", "credit": "510000"},
]

# 本月业务：(凭证类别, 摘要, 分录)
BUSINESS = [
    ("收", "收到客户货款", [
        {"account_code": "1002", "debit": "50000", "credit": ""},
        {"account_code": "1122", "debit": "", "credit": "50000"},
    ]),
    ("付", "支付办公室房租", [
        {"account_code": "6602", "debit": "8000", "credit": ""},
        {"account_code": "1002", "debit": "", "credit": "8000"},
    ]),
    ("付", "支付员工工资", [
        {"account_code": "2211", "debit": "30000", "credit": ""},
        {"account_code": "1002", "debit": "", "credit": "30000"},
    ]),
    ("转", "确认服务收入", [
        {"account_code": "1122", "debit": "30000", "credit": ""},
        {"account_code": "6001", "debit": "", "credit": "30000"},
    ]),
    ("转", "计提本月折旧", [
        {"account_code": "6602", "debit": "3000", "credit": ""},
        {"account_code": "1602", "debit": "", "credit": "3000"},
    ]),
    ("付", "报销差旅费", [
        {"account_code": "6602", "debit": "2500", "credit": ""},
        {"account_code": "1001", "debit": "", "credit": "2500"},
    ]),
]


def build(db_path: str) -> None:
    if Path(db_path).exists():
        Path(db_path).unlink()

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    s = Session(engine)

    ids = seed_demo_ledger(s)
    ls = s.get(LedgerSet, ids["ledger_set_id"])
    ls.name = COMPANY
    period = s.get(Period, ids["period_id"])
    s.flush()

    n = import_chart_of_accounts(s, ls.id, load_template_rows())
    print(f"[1/5] 建账：{COMPANY}　科目 {n['created']} 个　期间 {period.year}-{period.month:02d}")

    maker = s.get(Subject, ids["subject_id"])
    maker.display_name = "张会计"
    reviewer = Subject(type="user", display_name="李主管", autonomy_level=3)
    s.add(reviewer)
    s.commit()  # 必须先提交：casbin_rule 不在 Base.metadata 里，adapter 会另开连接

    grant_ledger_role(s, ledger_set_id=ls.id, subject_id=maker.id, role="admin")
    grant_ledger_role(s, ledger_set_id=ls.id, subject_id=reviewer.id, role="admin")
    s.commit()
    print(f"[2/5] 人员：制单 {maker.display_name}　审批 {reviewer.display_name}")

    import_opening_balances(
        s, ledger_set_id=ls.id, actor={"type": "user", "id": maker.id},
        lines=OPENING, period_year=period.year, period_month=period.month,
    )
    s.commit()
    print("[3/5] 期初：银行存款 500,000 + 库存现金 10,000 / 实收资本 510,000")

    ym = f"{period.year}-{period.month:02d}"
    print(f"[4/5] 制单（{ym}）：")
    maker_a = {"type": "user", "id": maker.id}
    reviewer_a = {"type": "user", "id": reviewer.id}

    for i, (vtype, summary, lines) in enumerate(BUSINESS, start=1):
        v, _ = create_draft_voucher(
            s, ledger_set_id=ls.id, actor=maker_a,
            voucher_date=f"{ym}-{10 + i:02d}", summary=summary, lines=lines,
            prefix=f"{vtype}-", per_prefix=True,
        )
        transition(s, voucher_id=v.id, actor=maker_a, target="PUSHED")
        transition(s, voucher_id=v.id, actor=reviewer_a, target="APPROVED")
        post_voucher(s, voucher_id=v.id, actor=maker_a)
        s.commit()
        vs = s.get(Voucher, v.id)
        print(f"      {vs.voucher_no}　{summary:12s}　{status_zh(vs.status)}")

    print("[5/5] 结账体检：")
    rep = precheck_close(s, ledger_set_id=ls.id, year=period.year, month=period.month)
    for c in rep["checks"]:
        mark = "√" if c["passed"] else "×"
        print(f"      {mark} {c['item']}：{c['detail']}")
    print(f"      → {rep['summary']}")

    print("\n科目余额（发生额非零）：")
    from kernel.db.models import Balance

    acc_by_id = {a.id: a for a in s.scalars(
        select(Account).where(Account.ledger_set_id == ls.id)).all()}
    rows = []
    for b in s.scalars(select(Balance).where(Balance.period_id == period.id)).all():
        a = acc_by_id.get(b.account_id)
        dr, cr = float(b.debit_total), float(b.credit_total)
        if a and (dr or cr):
            rows.append((a.code, a.name, dr, cr))
    for code, name, dr, cr in sorted(rows):
        bal = dr - cr
        flag = "借" if bal >= 0 else "贷"
        print(f"      {code} {name:12s} 借 {dr:>12,.2f}　贷 {cr:>12,.2f}　余额 {abs(bal):>12,.2f} {flag}")

    print("\n" + "=" * 62)
    print("起 Web 看效果（复制执行）：")
    print(f'  XERP_DB=sqlite:///{db_path.replace(chr(92), "/")} '
          f'XERP_WEB_PASSWORD=demo123 python -m kernel.webapp')
    print("  然后打开 http://127.0.0.1:8001  口令 demo123")
    print("=" * 62)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT.parent / ".showcase.db"))
    a = ap.parse_args()
    build(a.db)


if __name__ == "__main__":
    main()

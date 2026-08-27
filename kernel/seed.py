"""演示账套种子（P0-03 DoD）：小企业准则科目片段 + 当前期间 + 往来对象。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, LedgerSet, Party, Period, Subject

DEMO_ACCOUNTS = [
    # (code, name, direction, category)
    ("1001", "库存现金", "debit", "asset"),
    ("1002", "银行存款", "debit", "asset"),
    ("1122", "应收账款", "debit", "asset"),
    ("2202", "应付账款", "credit", "liability"),
    ("4001", "实收资本", "credit", "equity"),
    ("6001", "主营业务收入", "credit", "pnl"),
    ("6602", "管理费用", "debit", "pnl"),
]


def seed_demo_ledger(session: Session) -> dict[str, str]:
    """创建演示账套并返回关键 id。幂等：同名账套已存在则直接复用。"""
    ls = session.scalars(select(LedgerSet).where(LedgerSet.name == "演示账套")).first()
    if ls is None:
        ls = LedgerSet(name="演示账套", accounting_standard="small_business")
        session.add(ls)
        session.flush()

    acc_ids: dict[str, str] = {}
    for code, name, direction, category in DEMO_ACCOUNTS:
        acc = session.scalars(
            select(Account).where(Account.ledger_set_id == ls.id, Account.code == code)
        ).first()
        if acc is None:
            acc = Account(
                ledger_set_id=ls.id, code=code, name=name,
                direction=direction, category=category,
            )
            session.add(acc)
            session.flush()
        acc_ids[code] = acc.id

    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ls.id, Period.year == 2026, Period.month == 8
        )
    ).first()
    if period is None:
        period = Period(ledger_set_id=ls.id, year=2026, month=8, status="OPEN")
        session.add(period)
        session.flush()

    party = session.scalars(
        select(Party).where(Party.ledger_set_id == ls.id, Party.name == "演示客户A")
    ).first()
    if party is None:
        party = Party(
            ledger_set_id=ls.id, party_type="customer", name="演示客户A",
            aux_attrs={"level": "A"},
        )
        session.add(party)
        session.flush()

    subject = session.scalars(select(Subject).where(Subject.display_name == "丞辰")).first()
    if subject is None:
        subject = Subject(type="user", display_name="丞辰", autonomy_level=3)
        session.add(subject)
        session.flush()

    return {
        "ledger_set_id": ls.id,
        "period_id": period.id,
        "party_id": party.id,
        "subject_id": subject.id,
        "cash_account_id": acc_ids["1001"],
        "bank_account_id": acc_ids["1002"],
        "receivable_account_id": acc_ids["1122"],
        "payable_account_id": acc_ids["2202"],
        "capital_account_id": acc_ids["4001"],
        "revenue_account_id": acc_ids["6001"],
        "expense_account_id": acc_ids["6602"],
    }

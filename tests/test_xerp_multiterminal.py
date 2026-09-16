"""v1.5 多端触达 O17：同一账套，内核写入 == statements 读 == Web 渲染，三端一致。

实证「账在哪端改都一样」——这是 O17 的核心验收（≥2 端对同一账套读写一致）。

- 终端 A（写入）：kernel.posting.post_voucher，即 AI/MCP「过账」工具的同一内核路径
- 终端 B（读）：kernel.reporting.statements（CLI / AI 查账复用同一份逻辑，不复制配平）
- 终端 C（渲染）：kernel.webapp 的 /card 与 /boss 路由（Web 端，TestClient 复用线上渲染）

三者读同一 SQLite + 同一内核，故数值必然一致；本测试把这条不变量钉死。
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, LedgerSet, Period, Subject, Voucher, VoucherLine
from kernel.posting import post_voucher
from kernel.seed import seed_demo_ledger
from kernel.state import transition
from kernel.webapp import _boss_data, build_app


@pytest.fixture(scope="module")
def env():
    from tempfile import mkdtemp

    d = mkdtemp()
    url = f"sqlite:///{d}/multi.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()
        ids["reviewer_id"] = reviewer.id
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    c = TestClient(build_app(env["url"]))
    r = c.post(
        "/login",
        data={"subject_id": env["ids"]["subject_id"], "password": ""},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture(scope="module")
def ls_info(env):
    engine = create_engine(env["url"])
    with Session(engine) as s:
        ls = s.get(LedgerSet, env["ids"]["ledger_set_id"])
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ls.id)
        ).first()
    return (ls.id, ls.accounting_standard, per.year, per.month)


def _post_revenue_voucher(env, amount: str = "3000.00") -> None:
    """终端 A：制单 → 换人审批 → 过账，记一笔收入（AI/MCP 真实写入路径）。"""
    engine = create_engine(env["url"])
    with Session(engine) as s:
        ids = env["ids"]
        accs = {a.code: a for a in s.scalars(select(Account)).all()}
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ids["ledger_set_id"])
        ).first()
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"], period_id=per.id,
            voucher_no="记-MT1", voucher_date=date(per.year, per.month, 15),
            status="DRAFT", summary="多端测试·课程收入",
            created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=accs["100201"].id,
                        debit=Decimal(amount), credit=Decimal("0")),
            VoucherLine(line_no=2, account_id=accs["6001"].id,
                        debit=Decimal("0"), credit=Decimal(amount)),
        ]
        s.add(v)
        s.flush()
        # 换人审批（人是 Boss）：制单人 → 审批人 → 过账
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["subject_id"]}, target="PUSHED")
        transition(s, voucher_id=v.id,
                   actor={"type": "user", "id": ids["reviewer_id"]}, target="APPROVED")
        post_voucher(s, voucher_id=v.id,
                     actor={"type": "user", "id": ids["subject_id"]})
        s.commit()


def test_multiterminal_same_ledger(env, client, ls_info):
    ls_id, std, yr, mo = ls_info

    # 终端 B（CLI/AI 读）：写入前先读一次基准值
    engine = create_engine(env["url"])
    with Session(engine) as s:
        before_rev = _boss_data(s, ls_id, yr, mo, std)["inc_now"]["revenue"]

    # 终端 A 写入一笔收入
    _post_revenue_voucher(env, "3000.00")

    # 终端 B 再读：数值必须已变化（写入已落到同一账套）
    with Session(engine) as s:
        d = _boss_data(s, ls_id, yr, mo, std)
        after_rev = d["inc_now"]["revenue"]
        after_np = d["inc_now"]["net_profit"]
    assert after_rev - before_rev == Decimal("3000.00"), \
        f"写入未反映到内核 statements（终端B）：Δ={after_rev - before_rev}"

    # 终端 C（Web 渲染）：/card 把营收/净利以原始数字呈现——这是与内核对齐的硬证据
    rev_str = f"{after_rev:,.2f}"
    np_str = f"{after_np:,.2f}"
    card = client.get(f"/ledger/{ls_id}/card?year={yr}&month={mo}")
    boss = client.get(f"/ledger/{ls_id}/boss?year={yr}&month={mo}")
    assert card.status_code == 200, card.text[:300]
    assert boss.status_code == 200, boss.text[:300]
    # /card 原始数字 == 内核 statements 数值（Web 终端与 CLI/AI 终端同源）
    assert rev_str in card.text, "财报卡片未呈现写入后的营收（Web≠内核）"
    assert np_str in card.text, "财报卡片未呈现写入后的净利（Web≠内核）"
    # /boss 经营看板渲染同一账套同一期间（图表化视图，不重复配平）
    assert "经营看板" in boss.text and "账本精灵" in boss.text
    assert f"{yr}-{mo:02d}" in boss.text, "经营看板未呈现同一期间"

    # /card 含手机/企微触达提示：手机浏览器、企微都是可达终端
    assert "手机/企微看账" in card.text


def test_cli_terminal_reuses_kernel(env, client, ls_info):
    """终端 B（CLI/AI 查账）复用内核 statements——其数值与 Web 同源，不复制配平。

    实证：CLI 通过 statements 读到的营收，能原样出现在 Web 财报卡片上。
    """
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        rev = _boss_data(s, ls_id, yr, mo, std)["inc_now"]["revenue"]
    rev_str = f"{rev:,.2f}"
    # CLI 读到的数值，Web 财报卡片以原始数字呈现 → 两端同源
    card = client.get(f"/ledger/{ls_id}/card?year={yr}&month={mo}")
    assert card.status_code == 200
    assert rev_str in card.text, "CLI 读到的营收未出现在 Web 卡片（两端应同源）"
    # 经营看板同样渲染同一账套
    boss = client.get(f"/ledger/{ls_id}/boss?year={yr}&month={mo}")
    assert boss.status_code == 200 and "经营看板" in boss.text

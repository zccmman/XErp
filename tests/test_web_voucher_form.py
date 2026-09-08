"""G1-50 Web 制单表单 TDD。

此前 Web 端**只能看不能做**：要制单必须回到 MCP 对话里敲工具。
对一个「客户自己拿来即用」的系统来说，这是不可接受的——会计不用命令行。

本文件覆盖制单页与提交落库，重点验证三件容易被忽略、但直接决定好不好用的事：
  1. 下拉只给末级科目（选非末级必然被内核拒，等于给用户埋坑）
  2. 提交失败必须原地回填（重定向到空表 = 让会计整张凭证重打一遍）
  3. 建单走内核原语，与 MCP 是同一条校验路径
"""

import re
import tempfile
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Period, Subject, Voucher, VoucherLine
from kernel.events import E
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webvoucher.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
        s.add(reviewer)
        s.commit()
        ids["reviewer_id"] = reviewer.id
        p = s.get(Period, ids["period_id"])
        ids["year"], ids["month"] = p.year, p.month
        ids["non_leaf_codes"] = [
            a.code for a in s.scalars(
                select(Account).where(
                    Account.ledger_set_id == ids["ledger_set_id"],
                    Account.is_leaf.is_(False),
                )
            ).all()
        ]
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": env["ids"]["subject_id"], "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture()
def anon_client(env):
    from fastapi.testclient import TestClient

    from kernel.webapp import build_app

    return TestClient(build_app(env["url"]))


def _in_period_date(ids, day=15) -> str:
    return f"{ids['year']}-{ids['month']:02d}-{day:02d}"


def _leaf_codes(client, ls_id) -> list[str]:
    """从制单页下拉里取出可选科目编码（即页面实际暴露给用户的集合）。"""
    r = client.get(f"/ledger/{ls_id}/voucher/new")
    assert r.status_code == 200
    # 只取「科目」下拉内的 option：页面上还有凭证类别等其它 select，
    # 抓全局 option 会把类别值（收/付/转）当成科目编码提交。
    m = re.search(r"<select class=acct[^>]*>(.*?)</select>", r.text, re.S)
    scope = m.group(1) if m else r.text
    return [c for c in re.findall(r'<option value="([^"]+)"', scope) if c]


def _submit(client, ls_id, *, codes, debits, credits, voucher_date, summary="测试",
            action="draft"):
    return client.post(
        f"/ledger/{ls_id}/voucher/new",
        data={
            "voucher_date": voucher_date,
            "summary": summary,
            "account_code": codes,
            "debit": debits,
            "credit": credits,
            "action": action,
        },
        follow_redirects=False,
    )


# ---------- 页面渲染 ----------


def test_new_voucher_page_reachable_and_has_form(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/voucher/new")
    assert r.status_code == 200
    assert "新建凭证" in r.text
    assert f'action="/ledger/{ls}/voucher/new"' in r.text
    # 实时试算与科目过滤是可用性的两条命脉，丢了就等于回到盲填
    assert "sumDebit" in r.text and "sumDiff" in r.text
    assert "filterAccounts" in r.text


def test_new_voucher_requires_login(anon_client, env):
    ls = env["ids"]["ledger_set_id"]
    r = anon_client.get(f"/ledger/{ls}/voucher/new", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")


def test_only_leaf_accounts_offered(client, env):
    """非末级科目过账必被内核拒绝，放进下拉就是给用户埋坑。"""
    ls = env["ids"]["ledger_set_id"]
    offered = set(_leaf_codes(client, ls))
    if env["ids"]["non_leaf_codes"]:  # 演示科目表确实存在非末级科目时才有意义
        for code in env["ids"]["non_leaf_codes"]:
            assert code not in offered, f"非末级科目 {code} 不应出现在下拉中"


def test_dashboard_has_new_voucher_entry(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}")
    assert f"/ledger/{ls}/voucher/new" in r.text


def test_default_date_inside_period(client, env):
    """默认日期必须落在开放期间内，否则一进来提交就报 PERIOD_MISMATCH。"""
    ls = env["ids"]["ledger_set_id"]
    r = client.get(f"/ledger/{ls}/voucher/new")
    m = re.search(r'<input type=date name=voucher_date value="([^"]+)"', r.text)
    assert m, "表单应有日期输入框"
    d = date.fromisoformat(m.group(1))
    assert (d.year, d.month) == (env["ids"]["year"], env["ids"]["month"])


# ---------- 提交落库 ----------


def test_submit_balanced_creates_draft(client, env):
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]

    r = _submit(client, ls, codes=[a, b], debits=["100.00", ""], credits=["", "100.00"],
                voucher_date=_in_period_date(env["ids"]), summary="网页制单")

    assert r.status_code == 303, r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalars(
            select(Voucher).where(
                Voucher.ledger_set_id == ls, Voucher.summary == "网页制单"
            )
        ).first()
        assert v is not None, "凭证应已落库"
        assert v.status == "DRAFT"
        assert str(v.created_by) == env["ids"]["subject_id"], "制单人应取会话身份"
        assert len(v.lines) == 2
        ev = s.scalars(
            select(Event).where(
                Event.aggregate_id == v.id, Event.event_type == E.VOUCHER_CREATED
            )
        ).first()
        assert ev is not None, "新建凭证必须留审计事件"
        assert (ev.actor or {}).get("id") == env["ids"]["subject_id"]
    engine.dispose()


def test_submit_and_push_goes_to_pushed(client, env):
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]

    r = _submit(client, ls, codes=[a, b], debits=["250.50", ""], credits=["", "250.50"],
                voucher_date=_in_period_date(env["ids"]), summary="直接送审",
                action="submit")
    assert r.status_code == 303, r.text

    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalars(
            select(Voucher).where(Voucher.summary == "直接送审")
        ).first()
        assert v.status == "PUSHED"
        ev = s.scalars(
            select(Event).where(
                Event.aggregate_id == v.id, Event.event_type == E.VOUCHER_PUSHED
            )
        ).first()
        assert ev is not None
    engine.dispose()


def test_voucher_no_auto_increments(client, env):
    """连开两张，凭证号不能撞车。"""
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]
    for i in (1, 2):
        r = _submit(client, ls, codes=[a, b], debits=["10", ""], credits=["", "10"],
                    voucher_date=_in_period_date(env["ids"]), summary=f"连开-{i}")
        assert r.status_code == 303, r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        nos = [
            v.voucher_no for v in s.scalars(
                select(Voucher).where(Voucher.summary.like("连开-%"))
            ).all()
        ]
    engine.dispose()
    assert len(set(nos)) == 2, f"凭证号重复：{nos}"


def test_blank_rows_are_ignored(client, env):
    """未选科目的空行不应变成金额为 0 的分录。"""
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]
    r = _submit(
        client, ls,
        codes=[a, b, "", ""], debits=["80", "", "", ""], credits=["", "80", "", ""],
        voucher_date=_in_period_date(env["ids"]), summary="空行过滤",
    )
    assert r.status_code == 303, r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalars(select(Voucher).where(Voucher.summary == "空行过滤")).first()
        assert len(v.lines) == 2, f"空行不应落库，实际 {len(v.lines)} 行"
    engine.dispose()


# ---------- 校验失败必须回填 ----------


def test_unbalanced_is_rejected_and_prefilled(client, env):
    """借贷不等被拒时，已填内容必须留在表单里。"""
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]

    r = _submit(client, ls, codes=[a, b], debits=["100.00", ""], credits=["", "90.00"],
                voucher_date=_in_period_date(env["ids"]), summary="不平的凭证")

    assert r.status_code == 200, "应原地重渲染而非重定向"
    assert "新建凭证" in r.text
    assert "平衡" in r.text or "不等" in r.text or "借" in r.text
    # 回填：摘要与金额都在，用户只需改一个数字
    assert "不平的凭证" in r.text
    assert 'value="100.00"' in r.text and 'value="90.00"' in r.text
    # 科目也应保持选中
    assert f'value="{a}" selected' in r.text or f'selected>{a} ' in r.text


def test_unknown_account_is_rejected_and_prefilled(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = _submit(client, ls, codes=["9999"], debits=["10"], credits=[""],
                voucher_date=_in_period_date(env["ids"]), summary="错误科目")
    assert r.status_code == 200
    assert "科目" in r.text
    assert "错误科目" in r.text, "摘要应被回填"


def test_date_outside_period_is_rejected(client, env):
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]
    r = _submit(client, ls, codes=[a, b], debits=["10", ""], credits=["", "10"],
                voucher_date="1999-01-01", summary="日期越界")
    assert r.status_code == 200
    assert "1999-01-01" in r.text, "越界日期应回填，便于用户改正"


def test_rejected_submission_creates_nothing(client, env):
    """被拒的提交不能留下半成品凭证（回滚必须干净）。"""
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]
    before = _count_vouchers(env)
    _submit(client, ls, codes=[a, b], debits=["100", ""], credits=["", "1"],
            voucher_date=_in_period_date(env["ids"]), summary="半成品")
    assert _count_vouchers(env) == before, "校验失败不应留下任何凭证"


def _count_vouchers(env) -> int:
    engine = create_engine(env["url"])
    with Session(engine) as s:
        n = len(s.scalars(select(Voucher.id)).all())
    engine.dispose()
    return n


# ---------- 与 MCP 同一条内核路径 ----------


def test_web_uses_same_kernel_primitive_as_mcp(client, env):
    """Web 与 MCP 必须共用 create_draft_voucher。

    验证方式：Monkeypatch 内核原语，Web 提交若绕过它就不会被打点。
    """
    import kernel.voucher_wizard as wz
    import kernel.webapp as webapp

    calls = []
    orig = wz.create_draft_voucher

    def spy(*a, **kw):
        calls.append(kw)
        return orig(*a, **kw)

    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]

    webapp_path = "kernel.voucher_wizard.create_draft_voucher"
    import unittest.mock as mock

    with mock.patch(webapp_path, side_effect=spy):
        r = _submit(client, ls, codes=[a, b], debits=["7.77", ""], credits=["", "7.77"],
                    voucher_date=_in_period_date(env["ids"]), summary="路径一致")
    assert r.status_code == 303, r.text
    assert len(calls) == 1, "Web 制单必须且只能调用一次内核原语"
    assert calls[0]["ledger_set_id"] == ls


def test_created_amounts_persist_as_decimal(client, env):
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]
    _submit(client, ls, codes=[a, b], debits=["1234.56", ""], credits=["", "1234.56"],
            voucher_date=_in_period_date(env["ids"]), summary="金额精度")
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalars(select(Voucher).where(Voucher.summary == "金额精度")).first()
        lines = sorted(v.lines, key=lambda x: x.line_no)
        assert lines[0].debit == Decimal("1234.56")
        assert lines[1].credit == Decimal("1234.56")
    engine.dispose()


def test_blank_lines_never_persist_zero_amounts(client, env):
    """空行被过滤后，落库分录不应出现借贷都为 0 的脏行。"""
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    a, b = codes[0], codes[1]
    _submit(client, ls, codes=[a, b, "", ""], debits=["5", "", "", ""],
            credits=["", "5", "", ""], voucher_date=_in_period_date(env["ids"]),
            summary="无零行")
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalars(select(Voucher).where(Voucher.summary == "无零行")).first()
        for ln in v.lines:
            assert not (ln.debit == 0 and ln.credit == 0), "存在借贷双零的脏行"
    engine.dispose()


# ---------- 本体预检 HITL（阶段1） ----------


def _submit_raw(client, ls_id, *, codes, debits, credits, voucher_date,
                summary="测试", action="draft", ignore_ontology=None):
    data = {
        "voucher_date": voucher_date,
        "summary": summary,
        "account_code": codes,
        "debit": debits,
        "credit": credits,
        "action": action,
    }
    if ignore_ontology is not None:
        data["ignore_ontology"] = ignore_ontology
    return client.post(f"/ledger/{ls_id}/voucher/new", data=data,
                       follow_redirects=False)


def test_ontology_findings_block_until_ack(client, env):
    """应收无客户维度 → 本体提示拦截，勾选确认前不落库。"""
    ls = env["ids"]["ledger_set_id"]
    r = _submit_raw(
        client, ls, codes=["100201", "1122"], debits=["500.00", ""],
        credits=["", "500.00"], voucher_date=_in_period_date(env["ids"]),
        summary="本体拦截凭证",
    )
    assert r.status_code == 200, "命中本体规则应原地回填而非创建"
    assert "本体提示" in r.text
    assert "R-1122-01" in r.text
    assert 'name=ignore_ontology' in r.text
    assert "1122" in r.text  # 原地回填：科目仍保留
    engine = create_engine(env["url"])
    with Session(engine) as s:
        assert s.scalars(
            select(Voucher).where(Voucher.summary == "本体拦截凭证")
        ).first() is None, "确认前不得落库"
    engine.dispose()


def test_ontology_ack_allows_creation(client, env):
    ls = env["ids"]["ledger_set_id"]
    r = _submit_raw(
        client, ls, codes=["100201", "1122"], debits=["500.00", ""],
        credits=["", "500.00"], voucher_date=_in_period_date(env["ids"]),
        summary="本体确认凭证", ignore_ontology="1",
    )
    assert r.status_code == 303, r.text
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.scalars(
            select(Voucher).where(Voucher.summary == "本体确认凭证")
        ).first()
        assert v is not None and v.status == "DRAFT"
    engine.dispose()


def test_clean_lines_skip_ontology_gate(client, env):
    """纯现金科目不命中任何规则 → 不出现确认勾选，直接创建。"""
    ls = env["ids"]["ledger_set_id"]
    r = _submit_raw(
        client, ls, codes=["1001", "100201"], debits=["30.00", ""],
        credits=["", "30.00"], voucher_date=_in_period_date(env["ids"]),
        summary="现金提现",
    )
    assert r.status_code == 303, r.text
    assert "本体提示" not in r.text

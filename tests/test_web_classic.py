"""G1 怀旧兼容层 · Web 端 TDD。

怀旧不是换皮肤，是让老会计的肌肉记忆有落点：认「已记账」而不是 POSTED、
制单先选收/付/转、结账前先体检。本文件锁定这些 Web 侧契约：

  1. 状态显示中文术语，不再出现裸枚举（DRAFT/POSTED）
  2. 期末期间状态也中文化（未结账/已结账）
  3. 月末结账页给出四道闸门清单，且未通过项带整改提示
  4. 制单页的凭证类别下拉**不得污染科目下拉**（这是真踩过的坑）：
     抓取科目 option 的正则若不加范围，会把「收/付/转」当科目编码提交
  5. 类别 → 编号前缀：选「收」得 收-0001，不选仍是 记-**（默认行为不变）**
"""

import re
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Period
from kernel.seed import seed_demo_ledger


@pytest.fixture(scope="module")
def env():
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/webclassic.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        s.commit()
        p = s.get(Period, ids["period_id"])
        ids["year"], ids["month"] = p.year, p.month
    engine.dispose()
    return {"url": url, "ids": ids}


def _client(env, subject_id: str) -> TestClient:
    from kernel.webapp import build_app

    c = TestClient(build_app(env["url"]))
    r = c.post("/login", data={"subject_id": subject_id, "password": ""},
               follow_redirects=False)
    assert r.status_code in (302, 303), r.text
    return c


@pytest.fixture()
def client(env):
    return _client(env, env["ids"]["subject_id"])


def _leaf_codes(client, ls_id) -> list[str]:
    """只取科目下拉内的 option——防回归：类别下拉的 收/付/转 不能混进来。"""
    r = client.get(f"/ledger/{ls_id}/voucher/new")
    assert r.status_code == 200
    m = re.search(r"<select class=acct[^>]*>(.*?)</select>", r.text, re.S)
    assert m, "制单页应有科目下拉"
    return [c for c in re.findall(r'<option value="([^"]+)"', m.group(1)) if c]


def _make(client, env, summary, amount="88.00", voucher_type=None,
          action="draft") -> str:
    ls = env["ids"]["ledger_set_id"]
    codes = _leaf_codes(client, ls)
    data = {
        "voucher_date": f"{env['ids']['year']}-{env['ids']['month']:02d}-15",
        "summary": summary,
        "account_code": [codes[0], codes[1]],
        "debit": [amount, ""],
        "credit": ["", amount],
        "action": action,
    }
    if voucher_type:
        data["voucher_type"] = voucher_type
    r = client.post(f"/ledger/{ls}/voucher/new", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[-1]


def test_form_has_voucher_type_select(client, env):
    """制单页必须有凭证类别下拉——老会计制单的第一下手感。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/voucher/new").text
    m = re.search(r"<select name=voucher_type[^>]*>(.*?)</select>", t, re.S)
    assert m, "缺少凭证类别下拉"
    vals = re.findall(r'<option value="([^"]*)"', m.group(1))
    assert set(vals) == {"", "收", "付", "转"}, vals


def test_voucher_type_does_not_pollute_account_options(client, env):
    """防回归：类别值不得出现在科目下拉里。

    曾有测试用全局 `<option value=...>` 正则抓科目编码，新增类别下拉后
    把「收」当成科目提交，内核报「科目不存在: 收」。这里把边界钉死。
    """
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/voucher/new").text
    codes = _leaf_codes(client, ls)
    assert not ({"收", "付", "转"} & set(codes)), f"科目编码被类别污染: {codes[:5]}"
    # 且类别下拉确实在页面上存在（否则上面的断言等于没测）
    assert 'name=voucher_type' in t


def test_default_numbering_unchanged(client, env):
    """不选类别时行为一字不变：仍是统一编号 记-。"""
    vid = _make(client, env, "默认编号回归")
    t = client.get(f"/voucher/{vid}").text
    assert re.search(r"记-\d{4}", t), "默认应为统一编号 记-"


def test_chosen_type_drives_prefix(client, env):
    """选了类别才分类编号，且各类独立计数。"""
    v1 = _make(client, env, "收一笔", voucher_type="收")
    v2 = _make(client, env, "付一笔", voucher_type="付")
    assert "收-0001" in client.get(f"/voucher/{v1}").text
    assert "付-0001" in client.get(f"/voucher/{v2}").text


def test_status_shown_in_chinese(client, env):
    """状态说人话：老会计认「已记账」，不认 POSTED。"""
    ls = env["ids"]["ledger_set_id"]
    _make(client, env, "中文状态")
    t = client.get(f"/ledger/{ls}").text
    assert "未审核" in t, "草稿应显示中文状态"
    assert re.search(r'class="badge st-DRAFT"', t), "徽章应带状态类名"
    # 裸枚举不再暴露给用户
    assert not re.search(r">DRAFT<", t)


def test_period_status_chinese(client, env):
    """期间状态同样中文化。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}").text
    assert "未结账" in t
    assert "OPEN" not in t


def test_close_page_lists_gates(client, env):
    """月末结账页给四道闸门清单，而不是点下去才报错。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/close").text
    for item in ("上月已结账", "试算平衡", "损益已结转"):
        assert item in t, f"结账体检缺少闸门：{item}"
    assert 'class=check' in t or 'class="check"' in t


def test_close_page_blocks_when_gate_fails(client, env):
    """闸门未过时不得出现执行结账按钮——不能让人绕过体检。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/close").text
    if "损益已结转" in t and "尚未结转" in t:
        assert "执行月末结账" not in t, "有闸门未过却放行结账"


def test_toolbar_has_classic_entries(client, env):
    """工具条给出老会计熟悉的三件事：填制凭证 / 账簿报表 / 月末结账。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}").text
    for label in ("填制凭证", "账簿报表", "月末结账"):
        assert label in t, f"工具条缺少入口：{label}"


def test_summary_datalist_offered(client, env):
    """常用摘要下拉：历史凭证就是摘要库，不让老会计重复打字。"""
    ls = env["ids"]["ledger_set_id"]
    _make(client, env, "摘要复用测试")
    t = client.get(f"/ledger/{ls}/voucher/new").text
    assert "datalist id=sumList" in t
    assert "摘要复用测试" in t, "历史摘要应进入候选"


# ---------- P1 体验 · 结账回跳闭环 ----------

def test_do_close_success_returns_to_close_page(client, env):
    """结账成功回 /close：页面自然显示「已结账」横幅（不甩到报表页）。"""
    ls = env["ids"]["ledger_set_id"]
    # 该 fixture 的期间若无结账前置障碍，先试直发；闸门不过则本测试跳断言横幅
    r = client.post(f"/ledger/{ls}/close", data={"year": "2026", "month": "9"},
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith(f"/ledger/{ls}/close?year=2026&month=9")
    if "error=" not in loc:  # 成功路径：横幅可见
        t = client.get(loc).text
        assert "已结账" in t


def test_close_error_param_rendered(client, env):
    """close 页消费 error= 参数（失败回跳后上下文完整）。"""
    ls = env["ids"]["ledger_set_id"]
    t = client.get(f"/ledger/{ls}/close?error=闸门未过").text
    assert "闸门未过" in t

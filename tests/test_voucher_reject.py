"""G1-49 审批驳回路径（PUSHED → DRAFT）TDD。

背景：状态机此前只有 DRAFT→PUSHED→APPROVED 正向链路，审批人不同意时
**没有任何路径把凭证退回制单人**——只能作废重开，凭证号断号、审计链上
多出一张废单。财务上「退回修改」是最高频动作，缺它等于制单闭环不成立。

同时收敛历史遗留：企微/飞书/卡片三个 IM 通道此前**绕过状态机手写驳回**，
用旧式点号事件名 `voucher.rejected`，与注册表常量 `VOUCHER_REJECTED` 不一致，
导致「按事件类型统计驳回」必然漏掉 IM 渠道。本文件用同一套断言覆盖四个入口
（内核 / MCP / 文本指令 / 卡片按钮），确保事件类型只有一种写法。

设计约定：
- 审批人（非制单人）执行 → VOUCHER_REJECTED，原因**必填**
- 制单人本人执行       → VOUCHER_WITHDRAWN，原因选填
- Agent 一律不得处置待审队列
"""

import asyncio
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import (  # noqa: E402
    Balance,
    Event,
    Subject,
    Voucher,
    VoucherLine,
)
from kernel.events import E  # noqa: E402
from kernel.posting import PostingError, post_voucher  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402
from kernel.state import transition  # noqa: E402


@pytest.fixture()
def ctx():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())

    maker = s.get(Subject, ids["subject_id"])
    reviewer = Subject(type="user", display_name="王审批", autonomy_level=3)
    agent = Subject(type="agent", display_name="助理Agent", autonomy_level=3)
    s.add_all([reviewer, agent])
    s.commit()

    ids["reviewer_id"] = reviewer.id
    ids["agent_id"] = agent.id

    maker_actor = {"type": "user", "id": maker.id}
    reviewer_actor = {"type": "user", "id": reviewer.id}
    agent_actor = {"type": "agent", "id": agent.id}

    seq = {"n": 7000}

    def make_pushed(summary="驳回测试"):
        seq["n"] += 1
        v = Voucher(
            ledger_set_id=ids["ledger_set_id"],
            period_id=ids["period_id"],
            voucher_no=f"记-{seq['n']}",
            voucher_date=date(2026, 8, 20),
            status="DRAFT",
            summary=summary,
            created_by=maker.id,
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("500.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("500.00")),
        ]
        s.add(v)
        s.flush()
        transition(s, voucher_id=v.id, actor=maker_actor, target="PUSHED")
        s.commit()
        return s.get(Voucher, v.id)

    yield {
        "s": s,
        "ids": ids,
        "maker": maker_actor,
        "reviewer": reviewer_actor,
        "agent": agent_actor,
        "make_pushed": make_pushed,
    }
    s.close()


def _last_event(s, voucher_id):
    """最新一条事件（events 表用自增 id 定序）。"""
    return s.scalars(
        select(Event).where(Event.aggregate_id == voucher_id).order_by(Event.id.desc())
    ).first()


def _events_of(s, voucher_id, event_type):
    return s.scalars(
        select(Event).where(
            Event.aggregate_id == voucher_id, Event.event_type == event_type
        )
    ).all()


# ---------- 内核：驳回 ----------


def test_reject_returns_to_draft_and_records_reason(ctx):
    s, v = ctx["s"], ctx["make_pushed"]()

    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT",
               reason="差旅超标，需附审批单")
    s.commit()

    fresh = s.get(Voucher, v.id)
    assert fresh.status == "DRAFT", "驳回后应回到草稿，制单人才能修改"

    ev = _last_event(s, v.id)
    assert ev.event_type == E.VOUCHER_REJECTED
    assert (ev.payload or {}).get("reason") == "差旅超标，需附审批单"
    assert (ev.payload or {}).get("from") == "PUSHED"
    assert (ev.payload or {}).get("to") == "DRAFT"
    # 审批人身份必须入审计链，否则事后追责无据
    assert (ev.actor or {}).get("id") == ctx["ids"]["reviewer_id"]


def test_rejected_voucher_can_be_edited_and_resubmitted(ctx):
    """驳回的核心价值：改完可以重新提交，不必作废重开。"""
    s, v = ctx["s"], ctx["make_pushed"]()
    original_no = v.voucher_no

    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT",
               reason="金额有误")
    s.commit()

    # 制单人修改金额
    v = s.get(Voucher, v.id)
    for ln in v.lines:
        if ln.debit:
            ln.debit = Decimal("300.00")
        else:
            ln.credit = Decimal("300.00")
    s.commit()

    # 重新提交 → 审批 → 过账，全程复用原凭证号
    transition(s, voucher_id=v.id, actor=ctx["maker"], target="PUSHED")
    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="APPROVED")
    post_voucher(s, voucher_id=v.id, actor=ctx["maker"])
    s.commit()

    fresh = s.get(Voucher, v.id)
    assert fresh.status == "POSTED"
    assert fresh.voucher_no == original_no, "驳回重提不应换号，否则凭证号断号"

    from kernel.ledger import verify_chain

    ok, problem = verify_chain(s, ctx["ids"]["ledger_set_id"])
    assert ok and problem is None, f"审计链应完好：{problem}"


def test_reject_requires_reason(ctx):
    """驳回不填原因 → 拒绝。制单人必须知道要改什么。"""
    s, v = ctx["s"], ctx["make_pushed"]()
    with pytest.raises(PostingError) as ei:
        transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT")
    assert ei.value.code == "REJECT_REASON_REQUIRED"

    # 纯空白同样不算填了
    with pytest.raises(PostingError) as ei2:
        transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT",
                   reason="   ")
    assert ei2.value.code == "REJECT_REASON_REQUIRED"


def test_agent_cannot_reject(ctx):
    s, v = ctx["s"], ctx["make_pushed"]()
    with pytest.raises(PostingError) as ei:
        transition(s, voucher_id=v.id, actor=ctx["agent"], target="DRAFT",
                   reason="AI 认为有问题")
    assert ei.value.code == "AGENT_APPROVAL_FORBIDDEN"
    assert s.get(Voucher, v.id).status == "PUSHED"


# ---------- 内核：撤回（制单人本人） ----------


def test_maker_withdraw_own_voucher_is_withdrawn_not_rejected(ctx):
    """制单人收回自己的单据 → 记 VOUCHER_WITHDRAWN，原因选填。"""
    s, v = ctx["s"], ctx["make_pushed"]()

    transition(s, voucher_id=v.id, actor=ctx["maker"], target="DRAFT")
    s.commit()

    assert s.get(Voucher, v.id).status == "DRAFT"
    ev = _last_event(s, v.id)
    assert ev.event_type == E.VOUCHER_WITHDRAWN
    assert not _events_of(s, v.id, E.VOUCHER_REJECTED), "不应同时记为驳回"


def test_withdraw_reason_optional_and_kept(ctx):
    s, v = ctx["s"], ctx["make_pushed"]()
    transition(s, voucher_id=v.id, actor=ctx["maker"], target="DRAFT",
               reason="附发票后再提")
    s.commit()
    ev = _last_event(s, v.id)
    assert ev.event_type == E.VOUCHER_WITHDRAWN
    assert (ev.payload or {}).get("reason") == "附发票后再提"


# ---------- 状态机边界 ----------


def test_reject_non_pushed_is_invalid_transition(ctx):
    """草稿不能被驳回（还没提交），已批准/已过账也不行。"""
    s = ctx["s"]
    v = Voucher(
        ledger_set_id=ctx["ids"]["ledger_set_id"],
        period_id=ctx["ids"]["period_id"],
        voucher_no="记-7999",
        voucher_date=date(2026, 8, 20),
        status="DRAFT",
        summary="未提交",
        created_by=ctx["ids"]["subject_id"],
    )
    s.add(v)
    s.commit()
    with pytest.raises(PostingError) as ei:
        transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT",
                   reason="x")
    assert ei.value.code == "INVALID_TRANSITION"


def test_reject_twice_is_invalid(ctx):
    s, v = ctx["s"], ctx["make_pushed"]()
    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT", reason="退回")
    s.commit()
    with pytest.raises(PostingError) as ei:
        transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT",
                   reason="再退")
    assert ei.value.code == "INVALID_TRANSITION"


def test_reject_does_not_touch_balances(ctx):
    """驳回/撤回的凭证从未过账，余额投影不应有任何变化。"""
    s, v = ctx["s"], ctx["make_pushed"]()
    before = {b.account_id: (b.debit_total, b.credit_total)
              for b in s.scalars(select(Balance)).all()}
    transition(s, voucher_id=v.id, actor=ctx["reviewer"], target="DRAFT", reason="退回")
    s.commit()
    after = {b.account_id: (b.debit_total, b.credit_total)
             for b in s.scalars(select(Balance)).all()}
    assert before == after


# ---------- IM 通道：事件类型必须与内核一致 ----------


def test_text_command_reject_uses_registry_event_type(ctx):
    """历史缺陷回归：IM 通道曾绕过状态机写 `voucher.rejected`（旧式点号名）。"""
    from kernel.approval_bot import handle_approval_command

    s, v = ctx["s"], ctx["make_pushed"]()
    reply = handle_approval_command(
        s, f"驳回 {v.voucher_no} 金额与合同不符",
        actor=ctx["reviewer"], channel_label="企业微信",
        channel_user_id="wecom_boss", on_bind=lambda uid: None,
    )
    assert "已驳回" in reply
    assert s.get(Voucher, v.id).status == "DRAFT"

    ev = _last_event(s, v.id)
    assert ev.event_type == E.VOUCHER_REJECTED, (
        f"IM 通道必须与内核同事件类型，实际 {ev.event_type}"
    )
    assert (ev.payload or {}).get("reason") == "金额与合同不符"
    # 旧式点号名不得再出现
    assert not _events_of(s, v.id, "voucher.rejected")


def test_text_command_reject_requires_reason(ctx):
    from kernel.approval_bot import handle_approval_command

    s, v = ctx["s"], ctx["make_pushed"]()
    reply = handle_approval_command(
        s, f"驳回 {v.voucher_no}",
        actor=ctx["reviewer"], channel_label="企业微信",
        channel_user_id="wecom_boss", on_bind=lambda uid: None,
    )
    assert "原因" in reply and "❌" in reply
    assert s.get(Voucher, v.id).status == "PUSHED", "未填原因不应改状态"


def test_text_command_maker_reject_is_recorded_as_withdraw(ctx):
    from kernel.approval_bot import handle_approval_command

    s, v = ctx["s"], ctx["make_pushed"]()
    handle_approval_command(
        s, f"驳回 {v.voucher_no} 我自己写错了",
        actor=ctx["maker"], channel_label="飞书",
        channel_user_id="feishu_u", on_bind=lambda uid: None,
    )
    ev = _last_event(s, v.id)
    assert ev.event_type == E.VOUCHER_WITHDRAWN


def test_card_event_reject_uses_registry_event_type(ctx):
    from kernel import wecom

    s, v = ctx["s"], ctx["make_pushed"]()
    result = wecom.handle_card_event(s, f"reject:{v.id}", "wecom_boss")
    assert result == f"rejected:{v.voucher_no}"

    ev = _last_event(s, v.id)
    assert ev.event_type == E.VOUCHER_REJECTED
    assert "未填意见" in (ev.payload or {}).get("reason", ""), (
        "卡片无输入框，原因应以占位文本留痕，不能留空"
    )


# ---------- MCP 工具层 ----------


@pytest.fixture(scope="module")
def env():
    """落盘的种子库：MCP 工具用独立连接访问，数据必须真正 commit。"""
    d = tempfile.mkdtemp()
    url = f"sqlite:///{d}/reject.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        reviewer = Subject(type="user", display_name="王审批", autonomy_level=3)
        peer = Subject(type="user", display_name="李会计", autonomy_level=0)
        s.add_all([reviewer, peer])
        s.flush()
        ids["reviewer_id"] = reviewer.id
        ids["peer_id"] = peer.id  # 非制单人的会计：有 voucher:push 无 voucher:approve
        s.commit()

        from kernel.authz import grant_ledger_role

        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=ids["subject_id"], role="accountant")
        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=reviewer.id, role="reviewer")
        grant_ledger_role(s, ledger_set_id=ids["ledger_set_id"],
                          subject_id=peer.id, role="accountant")
        s.commit()

        from kernel.state import transition as _t

        v = Voucher(
            ledger_set_id=ids["ledger_set_id"], period_id=ids["period_id"],
            voucher_no="记-8001", voucher_date=date(2026, 8, 20), status="DRAFT",
            summary="MCP 驳回测试", created_by=ids["subject_id"],
        )
        v.lines = [
            VoucherLine(line_no=1, account_id=ids["expense_account_id"],
                        debit=Decimal("800.00"), credit=Decimal("0.00")),
            VoucherLine(line_no=2, account_id=ids["cash_account_id"],
                        debit=Decimal("0.00"), credit=Decimal("800.00")),
        ]
        s.add(v)
        s.flush()
        _t(s, voucher_id=v.id, actor={"type": "user", "id": ids["subject_id"]},
           target="PUSHED")
        s.commit()
        ids["voucher_id"] = v.id
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture()
def server(env):
    from xerp_mcp.server import build_server

    return build_server(env["url"])


def _call(server, name, **args):
    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(name, args)
            if getattr(res, "data", None) is not None:
                return res.data
            import json

            return json.loads(res.content[0].text)

    return asyncio.run(inner())


@pytest.fixture()
def pushed(env):
    """每个 MCP 用例前把凭证重置为 PUSHED（用例间互相隔离）。

    库按 module 复用，直接改状态比重建库快，且断言的是跃迁行为本身。
    """
    engine = create_engine(env["url"])
    with Session(engine) as s:
        v = s.get(Voucher, env["ids"]["voucher_id"])
        v.status = "PUSHED"
        s.commit()
    engine.dispose()
    return env["ids"]["voucher_id"]


def test_mcp_reject_voucher_ok(server, env, pushed):
    out = _call(server, "reject_voucher", voucher_id=pushed,
                actor_id=env["ids"]["reviewer_id"], reason="缺发票")
    assert out.get("ok") is True, out
    assert out["voucher"]["status"] == "DRAFT"


def test_mcp_reject_by_maker_is_denied(server, env, pushed):
    """制单人不能驳回自己的凭证——错误信息要指到正确的替代动作。"""
    out = _call(server, "reject_voucher", voucher_id=pushed,
                actor_id=env["ids"]["subject_id"], reason="反悔了")
    assert out.get("ok") is False
    assert out["error"]["code"] == "NO_SELF_APPROVAL"
    assert "withdraw_voucher" in out["error"]["message_zh"]


def test_mcp_withdraw_by_maker_ok(server, env, pushed):
    out = _call(server, "withdraw_voucher", voucher_id=pushed,
                actor_id=env["ids"]["subject_id"])
    assert out.get("ok") is True, out
    assert out["voucher"]["status"] == "DRAFT"


def test_mcp_withdraw_by_reviewer_is_denied(server, env, pushed):
    out = _call(server, "withdraw_voucher", voucher_id=pushed,
                actor_id=env["ids"]["reviewer_id"])
    assert out.get("ok") is False
    assert out["error"]["code"] == "NOT_VOUCHER_MAKER"


def test_mcp_reject_requires_approve_permission(server, env, pushed):
    """非制单人的会计只有 voucher:push 权限，驳回走 voucher:approve 应被拒。"""
    out = _call(server, "reject_voucher", voucher_id=pushed,
                actor_id=env["ids"]["peer_id"], reason="我觉得不对")
    assert out.get("ok") is False
    assert out["error"]["code"] == "FORBIDDEN"
    assert "voucher:approve" in out["error"]["message_zh"]


def test_mcp_withdraw_by_peer_accountant_is_denied(server, env, pushed):
    """撤回是制单人专属动作，同部门的会计也不能替他撤回。"""
    out = _call(server, "withdraw_voucher", voucher_id=pushed,
                actor_id=env["ids"]["peer_id"])
    assert out.get("ok") is False
    assert out["error"]["code"] == "NOT_VOUCHER_MAKER"

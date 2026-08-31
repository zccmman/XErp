"""A2UI 消息生成器（P1-05 阶段 2）：把账套报表投影为 A2UI v0.9 协议消息。

A2UI（Agent-to-User Interface）：Agent 输出声明式 JSON，客户端用官方
React 渲染器（@a2ui/react）呈现。本项目负责把三大报表/对账结果编译成
createSurface + updateComponents + updateDataModel 三段消息。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from kernel.reconcile import reconcile_ledger
from kernel.reporting.statements import balance_sheet, cash_flow, income_statement

CATALOG_BASIC = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"


def _fmt(x: Decimal | str) -> str:
    if isinstance(x, Decimal):
        return f"{x:,.2f}"
    return str(x)


def build_ledger_messages(
    session: Session,
    ledger_set_id: str,
    year: int,
    month: int,
    ledger_name: str,
    standard: str = "small_business",
) -> dict:
    """生成账套三表 + 对账的 A2UI 消息序列。"""
    surface_id = f"xerp-ledger-{ledger_set_id[:8]}-{year}{month:02d}"

    bs = balance_sheet(session, ledger_set_id, year, month, standard)
    inc = income_statement(session, ledger_set_id, year, month, standard)
    cf = cash_flow(session, ledger_set_id, year, month, standard)
    rec = reconcile_ledger(session, ledger_set_id, year, month, standard)

    lines: list[str] = ["【利润表】"]
    for it in inc["items"]:
        lines.append(f"  {it['item']}：{_fmt(it['amount'])}")
    lines.append(f"  净利润：{_fmt(inc['net_profit'])}")
    lines.append("【资产负债表】")
    for key, label in (("assets", "资产"), ("liabilities", "负债"),
                       ("equity", "所有者权益")):
        for it in bs[key]["items"]:
            lines.append(f"  {label} · {it['group']}：{_fmt(it['amount'])}")
    lines.append(f"  资产合计：{_fmt(bs['assets']['total'])}")
    lines.append(f"  负债和权益合计：{_fmt(bs['liabilities']['total'] + bs['equity']['total'])}")
    lines.append("【现金流量表】")
    for it in cf["items"]:
        lines.append(f"  {it['item']}：{_fmt(it['amount'])}")
    lines.append(f"  现金净增加：{_fmt(cf['net_increase'])}")
    rec_text = (
        "✅ 账账核对一致" if rec["ok"]
        else "❌ 对账异常：" + "、".join(i["kind"] for i in rec["issues"])
    )

    data = {
        "title": f"{ledger_name} · {year}-{month:02d} 三大报表",
        "subtitle": rec_text,
        "lines": lines,
        "net_profit": _fmt(inc["net_profit"]),
    }

    # 组件树：Column(标题, 副标题, 逐行 Text)
    # 官方 zod schema 为 strict 模式：Text 只接受 text/weight/variant/accessibility，
    # variant 枚举 h1~h5/caption/body（写 style/title 等字段会被整条消息拒绝）
    children_ids = ["title", "subtitle"]
    components = [
        {"id": "title", "component": "Text",
         "text": {"path": "/title"}, "variant": "h1"},
        {"id": "subtitle", "component": "Text",
         "text": {"path": "/subtitle"}, "variant": "caption"},
    ]
    for i in range(len(lines)):
        cid = f"line-{i}"
        children_ids.append(cid)
        components.append({
            "id": cid, "component": "Text", "text": {"path": f"/lines/{i}"},
        })

    messages = [
        {
            "version": "v0.9",
            "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_BASIC},
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {"id": "root", "component": "Column", "children": children_ids},
                    *components,
                ],
            },
        },
        {
            "version": "v0.9",
            "updateDataModel": {"surfaceId": surface_id, "path": "/", "value": data},
        },
    ]
    return {"surfaceId": surface_id, "messages": messages, "protocol": "v0.9"}

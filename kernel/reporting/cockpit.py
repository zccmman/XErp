"""AI 原生财务驾驶舱——零日结账（持续对账）视图 + 持续预测（what-if）的只读聚合。

把 OpenAI《Building an AI-Native Finance Function》两核心点重诠为 XErp 本地优先区隔：

① Zero-day close（零日结账）→ 持续对账视图：管理者随时看到已对账、可追溯的
   财务状况——实时三大报表 KPI + 应收(客户)/应付(供应商) 子账↔总账对账健康度
   + JEV 确定性决策异常标记；
② Continuously updated forecasting（持续预测）→ 哪个决策改结果：what-if 预设
   杠杆推演，显示哪个杠杆对期末现金/净利等影响最大（持续滚动，实际数一变即重算）。

全部只读、复用内核单一真源（绝不复制配平逻辑、绝不写账）。Web 路由与 MCP 工具
共用本函数，是驾驶舱数据的唯一真源（ADR-002）。Web 渲染层只负责把返回结构拼成
HTML；MCP 工具只负责把返回结构转成 JSON 友好后回传 AI 客户端。
"""

from __future__ import annotations


def cockpit_snapshot(session, *, ledger_set_id: str, year: int, month: int, standard: str) -> dict:
    """聚合 AI 原生财务驾驶舱所需全部数据。

    参数：
        session：SQLAlchemy Session（由调用方持有，本函数只读不提交）；
        ledger_set_id / year / month：基准账套与期间；
        standard：会计口径（通常由账套设置解析后传入，非空）。

    返回（结构固定，供 Web 与 MCP 共用）：
        {
          "bs": balance_sheet(...),     # 含 assets/liabilities/equity.total（Decimal）
          "inc": income_statement(...), # 含 revenue / net_profit（Decimal）
          "cf": cash_flow(...),         # 含 operating（Decimal）
          "recv": subledger_gl_reconcile(customer),  # 含 ok/difference(字符串)/partner_count...
          "pay":  subledger_gl_reconcile(supplier),
          "jev_items": [ {...} ],       # sprite_push 中 type=="jev_decision" 的只读异常/提醒
          "forecast": what_if(...) | None,  # 6 预设杠杆推演；缺种子时安全降为 None
        }
    """
    from kernel.reporting.arap import subledger_gl_reconcile
    from kernel.reporting.statements import balance_sheet, cash_flow, income_statement
    from kernel.simulation import preset_lever_names, what_if
    from kernel.sprite_push import sprite_push_items

    bs = balance_sheet(session, ledger_set_id, year, month, standard)
    inc = income_statement(session, ledger_set_id, year, month, standard)
    cf = cash_flow(session, ledger_set_id, year, month, standard)

    # 零日结账核心：子账↔总账对账健康度（应收 / 应付）
    recv = subledger_gl_reconcile(
        session, ledger_set_id=ledger_set_id, dim_key="customer", as_of_date=None
    )
    pay = subledger_gl_reconcile(
        session, ledger_set_id=ledger_set_id, dim_key="supplier", as_of_date=None
    )

    # JEV 异常/提醒（只读草稿，绝不触发过账/支付/改账）
    sp = sprite_push_items(session, ledger_set_id, year, month, standard)
    jev_items = [it for it in sp["items"] if it.get("type") == "jev_decision"]

    # 持续预测：6 个预设杠杆全跑（horizon 默认 6 期）。缺种子时安全降级——
    # 零日结账区不受影响仍能渲染。
    fc = None
    try:
        fc = what_if(
            session,
            ledger_set_id=ledger_set_id,
            base_year=year,
            base_month=month,
            horizon=6,
            levers=list(preset_lever_names()),
            standard=standard,
        )
    except Exception:  # noqa: BLE001 预测缺种子/失败 → 降级为不可用（不影响零日结账区）
        fc = None

    return {
        "bs": bs,
        "inc": inc,
        "cf": cf,
        "recv": recv,
        "pay": pay,
        "jev_items": jev_items,
        "forecast": fc,
    }

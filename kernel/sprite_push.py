"""账本精灵 · 7×24 主动推送（O18 / 智能体层）。

单一推送源：把「账本精灵该主动提醒什么」收敛到这里。Web（/boss 提醒）、
MCP（sprite_push 工具）、CLI（install.py remind）、企微（wecom_send）全部消费
同一份结构化 items，口径永远一致——守 ADR-002：配平/对账只复用内核
statements / reconcile / month_end_guide，绝不复制。

铁律 · 推送 ≠ 执行：
    本模块只**读取**账套状态、生成建议卡片，**绝不做任何写动作**
    （不制单、不过账、不结账、不审批）。任何终态动作仍由 Boss 在 Web /
    对话里显式确认。这里产出的每条 item 都带 action_hint（建议人类做什么），
    而不是替人类做完。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select


def latest_open_period(s, ls_id: str) -> tuple[int, int] | None:
    """取账套最新 OPEN 期间；无 OPEN 则取最新期间（含已结账）。"""
    from kernel.db.models import Period

    per = s.scalars(
        select(Period)
        .where(Period.ledger_set_id == ls_id, Period.status == "OPEN")
        .order_by(Period.year.desc(), Period.month.desc())
    ).first()
    if per is None:
        per = s.scalars(
            select(Period)
            .where(Period.ledger_set_id == ls_id)
            .order_by(Period.year.desc(), Period.month.desc())
        ).first()
    return (per.year, per.month) if per else None


def _is_closed(s, ls_id: str, yr: int, mo: int) -> bool:
    """期间是否已期末结转（与 _boss_data 同一判定：存在 结转-YYYYMM- 凭证）。"""
    from kernel.db.models import Voucher

    return s.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ls_id,
            Voucher.voucher_no.like(f"结转-{yr}{mo:02d}-%"),
        )
    ).first() is not None


def sprite_push_items(
    s, ls_id: str, yr: int, mo: int, standard: str = "small_business"
) -> dict:
    """计算账本精灵对某账套某期间的主动推送清单（**只读、无副作用**）。

    返回：
        items:  list[dict] 每条 {type, severity, title, text, html, action_hint}
                type ∈ month_end | anomaly | report_card | health | credit | collections
                severity ∈ info | warn | alert
        summary: 一句话总览（供 IM / CLI 首行）
        period_status: 期间状态（none / OPEN / CLOSED / ...）

    不调用任何写接口；未过账/对账异常/财报数字全部来自内核只读函数。
    """
    from kernel.db.models import Period, Voucher
    from kernel.period_guide import month_end_guide
    from kernel.reconcile import reconcile_ledger
    from kernel.reporting.statements import balance_sheet, income_statement

    period = s.scalars(
        select(Period).where(
            Period.ledger_set_id == ls_id, Period.year == yr, Period.month == mo
        )
    ).first()

    items: list[dict[str, Any]] = []

    if period is None:
        items.append({
            "type": "month_end",
            "severity": "warn",
            "title": f"{yr}-{mo:02d} 期间未建立",
            "text": f"{yr}-{mo:02d} 会计期间尚未建立，无法记账与出表。",
            "html": f"{yr}-{mo:02d} 会计期间尚未建立，无法记账与出表。",
            "action_hint": "先初始化期间（首次建账）再开始记账。",
        })
        return {
            "items": items,
            "summary": f"{yr}-{mo:02d} 期间未建立，请先建账。",
            "period_status": "none",
        }

    period_status = period.status
    guide = month_end_guide(s, ledger_set_id=ls_id, year=yr, month=mo)
    rec = reconcile_ledger(s, ls_id, yr, mo, standard)

    counts = guide.get("counts") or {}
    total = int(counts.get("total", 0) or 0)
    posted = int(counts.get("posted", 0) or 0)
    unposted = max(0, total - posted)

    # 1) 月结提醒（month_end）——复用 guide 的 counts，不复制统计
    #    与 _boss_data 原有提示逐字一致：OPEN 且未结转时，未过账提示与
    #    「尚未结转」提示都会出现（两条独立 if，非互斥），保证 Web 口径不变。
    closed = _is_closed(s, ls_id, yr, mo)
    if period_status == "OPEN" and not closed:
        if unposted > 0:
            items.append({
                "type": "month_end",
                "severity": "warn",
                "title": f"{yr}-{mo:02d} 还有 {unposted} 笔待过账",
                "text": f"本月还有 {unposted} 笔凭证未过账，月结前请先完成对账与换人审批过账。",
                "html": f"还有 <b>{unposted}</b> 笔凭证未过账，建议尽快换人审批过账。",
                "action_hint": "在 Web 或对话里换人审批、过账。",
            })
        items.append({
            "type": "month_end",
            "severity": "info",
            "title": f"{yr}-{mo:02d} 尚未期末结转",
            "text": f"{yr}-{mo:02d} 尚未期末结转，月结前请先完成对账与过账。",
            "html": f"{yr}-{mo:02d} 尚未期末结转，月结前请先完成对账与过账。",
            "action_hint": "确认对账无误后，由 Boss 显式执行月结。",
        })

    # 2) 异常预警（anomaly）——复用 reconcile，不复制核对逻辑
    if not rec.get("ok"):
        n = len(rec.get("issues") or [])
        items.append({
            "type": "anomaly",
            "severity": "alert",
            "title": f"账账核对发现 {n} 项异常",
            "text": f"账账核对发现 {n} 项不一致，月结前须先处理。",
            "html": f"账账核对发现 <b>{n}</b> 项异常，请先处理再月结。",
            "action_hint": "查看明细：Web 对账页或让 AI 列出 issues。",
        })

    # 3) 月度财报卡片（report_card）——复用 statements，只读呈现
    inc = income_statement(s, ls_id, yr, mo, standard)
    bs = balance_sheet(s, ls_id, yr, mo, standard)
    rev = inc.get("revenue") or Decimal("0")
    np_ = inc.get("net_profit") or Decimal("0")
    assets = bs.get("assets", {}).get("total") or Decimal("0")
    items.append({
        "type": "report_card",
        "severity": "info",
        "title": f"{yr}-{mo:02d} 月度财报卡片",
        "text": (f"营业收入 {rev:,.2f} · 净利润 {np_:,.2f} · 资产总额 {assets:,.2f}"
                 f"（本月 {total} 笔业务）"),
        "html": (f"营业收入 {rev:,.2f} · 净利润 {np_:,.2f} · 资产总额 {assets:,.2f}"
                 f"（本月 {total} 笔业务）"),
        "action_hint": "点开 Web /card 看完整卡片，或转发团队。",
    })

    # 5) AI 风险预警（risk）——复用 risk_scan 只读分析，作为 anomaly 通道同源消费
    #    推送 ≠ 执行：只展示风险与建议，绝不替 Boss 改账（守 sprite_push 铁律）。
    from kernel.reporting.risk_scan import scan_risks

    risk = scan_risks(s, ls_id, yr, mo, standard)
    for f in risk.get("findings") or []:
        if f["code"] == "NO_PERIOD":
            continue
        items.append({
            "type": "anomaly",
            "severity": f["severity"],
            "title": f"风险·{f['title']}",
            "text": f["detail"],
            "html": f["detail"],
            "action_hint": f["suggestion"],
        })

    # 6) 信用管理（credit）——复用 credit_exposure 只读扫描，作为 anomaly 通道同源消费
    #    推送 ≠ 执行：只展示超额/临近，绝不替 Boss 改账或自动收紧授信。
    from kernel.reporting.credit import credit_exposure

    exposure = credit_exposure(s, ledger_set_id=ls_id, dim_key="customer")
    for b in exposure.get("breaches") or []:
        items.append({
            "type": "credit",
            "severity": "alert",
            "title": f"信用超额·{b['partner']}",
            "text": (f"{b['partner']} 应收敞口 {b['exposure']} 已超授信额度 "
                     f"{b['credit_limit']}，超额 {b['over_by']}。"),
            "html": (f"{b['partner']} 应收敞口 <b>{b['exposure']}</b> 已超授信额度 "
                     f"{b['credit_limit']}，超额 <b>{b['over_by']}</b>。"),
            "action_hint": f"联系 {b['partner']} 催收，或收紧其授信额度（仅 Boss 可设）。",
        })
    for r in exposure.get("rows") or []:
        if r.get("near_limit") and not r.get("breach"):
            items.append({
                "type": "credit",
                "severity": "warn",
                "title": f"信用临近额度·{r['partner']}",
                "text": (f"{r['partner']} 应收敞口 {r['exposure']}，授信额度 "
                         f"{r['credit_limit']}，利用率已达 {r['utilization']}。"),
                "html": (f"{r['partner']} 应收敞口 {r['exposure']}，授信额度 "
                         f"{r['credit_limit']}，利用率已达 <b>{r['utilization']}</b>。"),
                "action_hint": f"关注 {r['partner']} 后续赊销，必要时收紧额度。",
            })

    # 7) 智能催收（collections）——复用 collections_draft 只读草稿，作为 anomaly 通道同源消费
    #    推送 ≠ 执行：只展示逾期与催收话术草稿，绝不代发催款函/电话/法务。
    from kernel.reporting.credit import collections_draft

    coll = collections_draft(s, ledger_set_id=ls_id)
    for r in coll.get("rows") or []:
        level = r["level"]
        sev = "alert" if level == "L3" else "warn"
        verb = {"L1": "提醒", "L2": "跟进", "L3": "最后通牒"}.get(level, "提醒")
        items.append({
            "type": "collections",
            "severity": sev,
            "title": f"催收·{level}·{r['partner']}",
            "text": (f"{r['partner']} 逾期应收 {r['overdue_amount']}，最早一笔已 "
                     f"{r['oldest_days']} 天。{r['draft_message']}"),
            "html": (f"{r['partner']} 逾期应收 <b>{r['overdue_amount']}</b>，最早一笔已 "
                     f"<b>{r['oldest_days']}</b> 天，建议发送{level}催收。"),
            "action_hint": f"发送{verb}（仅 Boss 在 Web/IM 执行，XErp 不代发）",
        })

    # 4) 健康（health）——无任何待办时给正向反馈
    actionable = any(
        it["type"] in ("month_end", "anomaly", "credit", "collections") for it in items
    )
    if not actionable:
        items.append({
            "type": "health",
            "severity": "info",
            "title": "本月账目健康",
            "text": "所有凭证已入账、账账核对一致，随时可一键月结 ✅",
            "html": "本月账目健康，随时可一键月结 ✅",
            "action_hint": "保持即可，无需操作。",
        })

    return {
        "items": items,
        "summary": _summary(items, yr, mo),
        "period_status": period_status,
    }


def _summary(items: list[dict], yr: int, mo: int) -> str:
    """一句话总览：优先报风险，其次月结状态，最后健康。"""
    alerts = [it for it in items if it["severity"] == "alert"]
    warns = [it for it in items if it["severity"] == "warn"]
    if alerts:
        return f"⚠️ {yr}-{mo:02d} 账本精灵预警：{alerts[0]['title']}"
    if warns:
        return f"🔔 {yr}-{mo:02d} 账本精灵提醒：{warns[0]['title']}"
    return f"✅ {yr}-{mo:02d} 账本精灵：账目健康，可随时月结"


def format_wecom_card(payload: dict) -> str:
    """把推送清单渲染为企业微信 markdown 卡片（**只展示、不执行**）。"""
    summary = payload.get("summary", "")
    items = payload.get("items", [])
    lines = [f"## 💡 账本精灵 · 主动提醒", "", f"> {summary}", ""]
    icon = {"alert": "🔴", "warn": "🟠", "info": "🟢"}
    for it in items:
        lines.append(f"{icon.get(it['severity'], '•')} **{it['title']}**")
        lines.append(f"{it['text']}")
        lines.append(f"👉 {it['action_hint']}")
        lines.append("")
    lines.append("---")
    lines.append("数据自持 · AI 产建议 · **人是 Boss**，任何过账/结账需你确认")
    return "\n".join(lines)


def format_cli_text(payload: dict) -> str:
    """把推送清单渲染为 CLI 纯文本（install.py remind 用）。"""
    summary = payload.get("summary", "")
    items = payload.get("items", [])
    out = []
    out.append("=" * 54)
    out.append(" 账本精灵 · 主动提醒（推送 ≠ 执行，终态需你确认）")
    out.append("=" * 54)
    out.append(f" 总览：{summary}")
    out.append("")
    for it in items:
        tag = {"alert": "[异常]", "warn": "[提醒]", "info": "[信息]"}.get(it["severity"], "")
        out.append(f" {tag} {it['title']}")
        out.append(f"      {it['text']}")
        out.append(f"      建议：{it['action_hint']}")
    out.append("")
    return "\n".join(out)

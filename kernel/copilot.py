"""实时 Copilot（Phase E / E2）：确定性自然语言意图路由（离线、可审计、零 LLM 成本）。

设计铁律（项目「确定性优先、离线、可审计」）：
- 自然语言理解内核走**确定性路由**——规则/关键词意图匹配 + 复用各只读内核；
  完全离线、可审计、零 LLM 成本；未配置 LLM 也能跑（与 ADR 一致）。
- ``ask()`` 把中文问题路由到 operating / arap / credit / foreign / simulation / healing
  的只读内核，输出 {answer_zh, intent, tool_calls, evidence, followups, severity}。
- 每条数字都来自被调用的只读内核（ADR-002 单一真源），``tool_calls`` 逐条溯源。
- 严重项（信用超额 / 子账失配 / 异常自愈 critical）→ 经 ``operator.signal(ALERT)``
  联动算子（E5，复用跨进程信号桥，无需 websocket）；推送 ≠ 执行，不改账。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import Period
from kernel.operating import graph_metrics, partner_profile
from kernel.reporting.arap import (
    ArapError,
    subledger_gl_reconcile,
    unmatched_receipts,
)
from kernel.reporting.credit import collections_draft, credit_exposure
from kernel.reporting.foreign import foreign_trial_balance
from kernel.healing import healing_suggestions
from kernel.simulation import what_if as _what_if_simulation

ZERO = Decimal("0.00")


# ------------------------------------------------------------ 意图路由


_RECONCILE_KW = ("对账", "勾稽", "子账", "总账", "失配", "勾对", "漏挂", "勾稽")
_COLLECTIONS_KW = ("逾期", "催收", "该催", "谁欠", "欠款", "未收回", "催款", "overdue")
_UNMATCHED_KW = ("待匹配", "未匹配", "回款待", "没匹配", "回款没", "收款待", "待核销回款")
_FOREIGN_KW = ("外币", "汇率", "汇兑", "重估", "外汇", "fx", "外币户")
_OVERVIEW_KW = ("总览", "全貌", "健康", "指标", "集中度", "敞口", "一览", "概览",
                "全屏", "看板", "概貌", "全景", "授信", "超额", "风险", "体检", "汇总")

# 情景推演（E3）：识别「如果…会怎样」「加速回款」「毛利下降」等假设性提问
_WHATIF_KW = ("如果", "假如", "假设", "会怎样", "会如何", "影响", "测算", "情景",
              "推演", "模拟", "压力测试", "敏感性", "加速回款", "缩短应收", "延长付款",
              "拖延付款", "占用供应商", "毛利", "降价", "毛利率", "增长停滞", "不增长",
              "成本上升", "费用上升", "资本开支", "扩产", "capex", "what-if", "whatif")
# 异常自愈（E4）：识别「异常怎么处理」「整改建议」「风控建议」等
_HEALING_KW = ("异常", "自愈", "修复", "整改", "建议", "怎么处理", "怎么办",
               "healing", "heal", "风控建议", "风险建议", "排查", "风险点")


def _parse_whatif_levers(q: str) -> list[str]:
    """从问题中识别具体杠杆；未指定具体杠杆时返回默认敏感性组合。"""
    mapping = [
        (("加速回款", "缩短应收", "回款快", "加快回款"), "ar_acceleration"),
        (("延长付款", "拖延付款", "占用供应商", "晚付", "拖延"), "ap_extension"),
        (("毛利", "降价", "毛利率"), "margin_compression"),
        (("增长停滞", "不增长", "零增长", "增长放缓"), "growth_halt"),
        (("成本上升", "费用上升", "费用增加", "成本增加"), "cost_inflation"),
        (("资本开支", "扩产", "capex", "投资"), "capex_surge"),
    ]
    hit: list[str] = []
    for keys, name in mapping:
        if any(k in q for k in keys):
            hit.append(name)
    if not hit:
        # 未指定具体杠杆 → 默认跑一套典型压力情景（敏感性分析）
        return ["ar_acceleration", "ap_extension", "margin_compression", "growth_halt"]
    return hit


def _known_partners_with_dim(
    session, ledger_set_id: str, as_of_date: date | None
) -> dict[str, str]:
    """返回 {往来单位名: dim_key}，用于 Copilot 从问题中识别具体客户/供应商。"""
    names: dict[str, str] = {}
    for dk in ("customer", "supplier"):
        rep = subledger_gl_reconcile(
            session, ledger_set_id=ledger_set_id, dim_key=dk, as_of_date=as_of_date
        )
        for p in rep["partners"]:
            names.setdefault(p["partner"], dk)
    return names


def _route(
    session, ledger_set_id: str, question: str, as_of_date: date | None
) -> tuple[str, dict[str, Any]]:
    """确定性意图路由：返回 (intent, params)。

    优先级：① 命中具体往来单位名 → partner_profile（聚合敞口/账龄/未清/待匹配/催收/对账）；
            ② 关键词命中全局意图（对账/催收/待匹配/外币/总览）→ 对应只读内核；
            ③ 兜底 → overview（graph_metrics）。
    """
    # ① 具体往来单位名
    names = _known_partners_with_dim(session, ledger_set_id, as_of_date)
    hit = None
    for name in names:
        if name and name in question:
            if hit is None or len(name) > len(hit):
                hit = name
    if hit is not None:
        return "partner_profile", {"partner": hit, "dim_key": names[hit]}

    # ② 关键词全局意图
    q = question.lower()
    if any(k in q for k in _RECONCILE_KW):
        return "reconcile", {}
    if any(k in q for k in _COLLECTIONS_KW):
        return "collections", {}
    if any(k in q for k in _UNMATCHED_KW):
        return "unmatched", {}
    if any(k in q for k in _FOREIGN_KW):
        return "foreign", {}
    if any(k in q for k in _HEALING_KW):
        return "healing", {}
    if any(k in q for k in _WHATIF_KW):
        return "what_if", {"levers": _parse_whatif_levers(q)}
    if any(k in q for k in _OVERVIEW_KW):
        return "overview", {}
    return "overview", {}


def _latest_period(session, ledger_set_id: str):
    """取最新（year, month）期间（OPEN/CLOSED 均可），供外币口径定位。"""
    periods = session.scalars(
        select(Period).where(Period.ledger_set_id == ledger_set_id)
    ).all()
    if not periods:
        return None
    return max((p.year, p.month) for p in periods)


# ------------------------------------------------------------ 各意图作答


def _answer_partner(params, session, ledger_set_id, as_of_date) -> dict[str, Any]:
    dim_key = params["dim_key"]
    partner = params["partner"]
    prof = partner_profile(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key,
        partner=partner, as_of_date=as_of_date,
    )
    s = prof["summary"]
    parts = [f"【{partner}（{dim_key}）运营财务画像 · 截至 {prof['as_of_date']}】"]
    flag = "超额⚠️" if s["breach"] else ("临近额度" if s["near_limit"] else "额度内")
    parts.append(
        f"· 信用敞口 {s['exposure']}，授信额度 {s['credit_limit']}"
        f"（利用率 {s['utilization'] or '—'}）— {flag}"
    )
    oi = prof["open_items"]
    parts.append(f"· 未清项 {oi['count']} 笔，合计 {oi['totals']['balance']}")
    ag = prof["aging"]
    if ag["items"]:
        bk = ag["items"][0]["buckets"]
        d = ag["bucket_days"]
        b0 = bk.get(f"b0_{d[0]}", "0.00")
        b1 = bk.get(f"b{d[0]}_{d[1]}", "0.00")
        b2 = bk.get(f"b{d[1]}_{d[2]}", "0.00")
        b3 = bk.get(f"b{d[2]}_plus", "0.00")
        parts.append(
            f"· 账龄：0-{d[0]}天 {b0} / {d[0]}-{d[1]}天 {b1} / "
            f"{d[1]}-{d[2]}天 {b2} / {d[2]}天以上 {b3}"
        )
    um = prof["unmatched_receipts"]
    parts.append(f"· 待匹配回款 {um['count']} 笔，剩余 {um['remaining']}")
    col = prof["collections"]
    if col.get("available") and col["level"]:
        parts.append(
            f"· 逾期 {col['overdue_amount']}（最老 {col['oldest_days']} 天，级别 {col['level']}）"
        )
    rc = prof["reconcile"]
    parts.append(
        f"· 对账：单位已挂接往来维度={'是' if rc['partner_assigned'] else '否'}"
        f"；维度整体{'平衡' if rc['dimension_ok'] else '失配（差异 '+rc['dimension_difference']+'）'}"
    )
    severity = "ALERT" if (s["breach"] or not rc["dimension_ok"]) else "NORMAL"
    followups = [
        f"运行 子账总账对账 复查 {dim_key} 维度",
        "查看 逾期催收草稿" if dim_key == "customer" else "查看 应收应付账龄",
        "运行 收款自动匹配 清理待匹配回款",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": [{"tool": "operating_partner_profile",
                        "args": {"dim_key": dim_key, "partner": partner}}],
        "evidence": {"partner_profile": prof},
        "followups": followups,
        "severity": severity,
    }


def _answer_overview(params, session, ledger_set_id, as_of_date, dim_key) -> dict[str, Any]:
    gm = graph_metrics(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    parts = [f"【运营财务总览 · {dim_key} · 截至 {gm['as_of_date']}】"]
    parts.append(f"· 应收(AR)总额 {gm['ar_total']}，应付(AP)总额 {gm['ap_total']}")
    parts.append(
        f"· {dim_key} 敞口合计 {gm['exposure_total']}"
        f"（授信 {gm['exposure_credit_limit']}，利用率 {gm['exposure_utilization'] or '—'}）"
    )
    top = gm["exposure_topn"][:5]
    if top:
        lines = "、".join(f"{r['partner']} {r['exposure']}" for r in top)
        parts.append(f"· 敞口 Top{len(top)}：{lines}")
    h = gm["hhi"]
    parts.append(f"· 敞口集中度 HHI={h['value']}（{h['level']}，{h['partner_count']} 家）")
    rh = gm["reconcile_health"]
    parts.append(
        f"· 子账↔总账对账：{'平衡 ✓' if rh['ok'] else '失配 ✗（差异 '+rh['difference']+'，漏挂 '+str(rh['unassigned_count'])+' 笔）'}"
    )
    breaches = gm["breaches"]
    if breaches:
        bl = "、".join(f"{b['partner']} 超 {b['over_by']}" for b in breaches)
        parts.append(f"· ⚠️ 授信超额 {len(breaches)} 家：{bl}")
    severity = "ALERT" if (breaches or not rh["ok"]) else "NORMAL"
    followups = [
        "查看 某客户 全貌（说客户名）",
        "运行 逾期催收草稿",
        "运行 子账总账对账",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": [{"tool": "operating_graph_metrics", "args": {"dim_key": dim_key}}],
        "evidence": {"graph_metrics": gm},
        "followups": followups,
        "severity": severity,
    }


def _answer_reconcile(params, session, ledger_set_id, as_of_date, dim_key) -> dict[str, Any]:
    rec = subledger_gl_reconcile(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    parts = [f"【{dim_key} 子账↔总账对账 · 截至 {rec['as_of_date']}】"]
    parts.append(f"· 控制科目({', '.join(rec['accounts']) or '—'})总额 {rec['control_total']}")
    parts.append(f"· 往来单位明细合计 {rec['subledger_total']}")
    if rec["ok"]:
        parts.append(f"· 对账平衡 ✓（差异 {rec['difference']}，{rec['partner_count']} 家）")
    else:
        parts.append(
            f"· 对账失配 ✗：差异 {rec['difference']}，"
            f"漏挂往来单位 {rec['unassigned_count']} 笔（合计 {rec['unassigned_total']}）"
        )
        if rec["unassigned_lines"]:
            sample = rec["unassigned_lines"][0]
            parts.append(
                f"  示例：凭证 {sample['voucher_no']} {sample['account_code']} "
                f"{sample['debit']}/{sample['credit']} — {sample['summary']}"
            )
    severity = "ALERT" if not rec["ok"] else "NORMAL"
    followups = [
        "查看 运营财务总览",
        "查看 某客户 全貌（说客户名）",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": [{"tool": "reconcile_subledger_gl", "args": {"dim_key": dim_key}}],
        "evidence": {"reconcile": rec},
        "followups": followups,
        "severity": severity,
    }


def _answer_collections(params, session, ledger_set_id, as_of_date) -> dict[str, Any]:
    col = collections_draft(session, ledger_set_id=ledger_set_id, as_of_date=as_of_date)
    parts = [f"【逾期催收草稿 · 截至 {col['as_of_date']}】"]
    tot = col["totals"]
    parts.append(
        f"· 逾期应收合计 {tot['overdue_amount']}，涉及 {tot['customers']} 家客户"
    )
    parts.append(
        f"· 升级分布：L1（提醒）{col['counts']['L1']} 家 / "
        f"L2（跟进+电话）{col['counts']['L2']} 家 / L3（最后通牒）{col['counts']['L3']} 家"
    )
    for r in col["rows"][:5]:
        parts.append(
            f"  - {r['partner']} {r['overdue_amount']}（最老 {r['oldest_days']} 天，{r['level']}）"
        )
    severity = "ALERT" if tot["customers"] > 0 else "NORMAL"
    followups = [
        "查看 某客户 全貌（说客户名）",
        "运行 收款自动匹配 清理回款",
        "查看 运营财务总览",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": [{"tool": "arap_collections_draft", "args": {}}],
        "evidence": {"collections": col},
        "followups": followups,
        "severity": severity,
    }


def _answer_unmatched(params, session, ledger_set_id, as_of_date, dim_key) -> dict[str, Any]:
    ur = unmatched_receipts(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    parts = [f"【待匹配回款 · {dim_key} · 截至 {ur['as_of_date']}】"]
    parts.append(
        f"· 待匹配 {ur['totals']['count']} 笔，剩余可匹配额 {ur['totals']['remaining']}"
    )
    for it in ur["items"][:5]:
        parts.append(
            f"  - {it['partner']} 凭证 {it['voucher_no']} {it['date']} "
            f"剩余 {it['remaining']}（原额 {it['amount']}）"
        )
    severity = "NORMAL"
    followups = [
        "运行 收款自动匹配（arap_propose_receipt_match）",
        "查看 某客户 全貌（说客户名）",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": [{"tool": "arap_unmatched_receipts", "args": {"dim_key": dim_key}}],
        "evidence": {"unmatched_receipts": ur},
        "followups": followups,
        "severity": severity,
    }


def _answer_foreign(params, session, ledger_set_id, as_of_date) -> dict[str, Any]:
    lp = _latest_period(session, ledger_set_id)
    parts = ["【外币头寸 · 汇兑损益提示】"]
    if lp is None:
        parts.append("· 账套无期间，无法定位外币口径。")
        return {
            "answer_zh": "\n".join(parts),
            "tool_calls": [], "evidence": {},
            "followups": ["先确保账套存在 OPEN 期间"], "severity": "NORMAL",
        }
    tb = foreign_trial_balance(session, ledger_set_id=ledger_set_id, year=lp[0], month=lp[1])
    nets: dict[str, Decimal] = {}
    for r in tb["rows"]:
        net = Decimal(r["foreign_debit"]) - Decimal(r["foreign_credit"])
        if net != ZERO:
            nets[r["currency"]] = nets.get(r["currency"], ZERO) + net
    if not nets:
        parts.append(f"· {lp[0]}-{lp[1]:02d} 无未结算外币头寸，无需重估。")
    else:
        detail = "、".join(f"{c} {n:.2f}" for c, n in sorted(nets.items()))
        parts.append(f"· {lp[0]}-{lp[1]:02d} 累计外币净头寸：{detail}（原币）")
        parts.append("· 月结前请运行 汇兑损益重估（fx_revaluation_draft），需提供期末汇率 fx_rates。")
    severity = "NORMAL"
    followups = [
        "运行 汇兑损益重估（fx_revaluation_draft，需期末汇率）",
        "查看 外币试算（foreign_trial_balance）",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": [{"tool": "foreign_trial_balance",
                        "args": {"year": lp[0], "month": lp[1]}}],
        "evidence": {"foreign_trial_balance": tb},
        "followups": followups,
        "severity": severity,
    }


def _answer_whatif(params, session, ledger_set_id, as_of_date) -> dict[str, Any]:
    lp = _latest_period(session, ledger_set_id)
    parts = ["【情景推演 what-if · 基准 vs 杠杆（E3）】"]
    if lp is None:
        parts.append("· 账套无期间，无法定位基准种子（实际三表）。")
        return {
            "answer_zh": "\n".join(parts), "tool_calls": [], "evidence": {},
            "followups": ["先确保账套存在 OPEN 期间"], "severity": "NORMAL",
        }
    levers = params.get("levers", [])
    res = _what_if_simulation(
        session, ledger_set_id=ledger_set_id, base_year=lp[0], base_month=lp[1],
        horizon=12, levers=levers,
    )
    base_last = res["baseline"]["periods"][-1]
    base_cash = Decimal(str(base_last["balance_sheet"]["cash"]))
    base_np = Decimal(str(base_last["income_statement"]["net_profit"]))
    parts.append(
        f"· 基准（{res['base_period']['year']}-{res['base_period']['month']:02d} 起 12 期）："
        f"期末现金 {base_cash:.2f}，累计净利润 {base_np:.2f}"
    )
    for name, v in res["variants"].items():
        imp = v["impact"]
        dc = imp["closing_cash"]["delta"]
        dn = imp["net_profit"]["delta"]
        parts.append(
            f"· {v['description_zh']} → 期末现金 Δ{dc} ，累计净利润 Δ{dn}"
        )
    followups = [
        "查看 运营财务总览",
        "运行 应收应付账龄",
        "运行 异常自愈建议（healing）",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": res["tool_calls"],
        "evidence": {"what_if": res},
        "followups": followups,
        "severity": "NORMAL",
    }


def _answer_healing(params, session, ledger_set_id, as_of_date) -> dict[str, Any]:
    res = healing_suggestions(
        session, ledger_set_id=ledger_set_id, lookback_days=30
    )
    parts = [
        f"【异常自愈建议 · HITL 动作清单（E4）· 扫描 {res['scanned_vouchers']} 张凭证】"
    ]
    parts.append(
        f"· 检出规则命中 {res['findings_count']} 条；生成待人工确认建议 "
        f"{len(res['suggestions'])} 条（全部 human_approval_required=True，绝不自动执行）"
    )
    for s in res["suggestions"][:6]:
        if s.get("rule"):
            parts.append(
                f"  - [{s['severity']}] {s['rule']}（凭证 {s['source_voucher_no']}）："
                f"{s['suggested_action_zh']}"
            )
        else:
            parts.append(
                f"  - [{s['severity']}] 断路器 {s.get('subject_id', '')}："
                f"{s['suggested_action_zh']}"
            )
    if res["breaker_open_count"]:
        parts.append(
            f"· ⚠️ {res['breaker_open_count']} 个 Agent 断路器处于「开」状态，"
            f"需 admin 人工复核后 anomaly_release（O11：Agent 不能自解）"
        )
    rank = res["severity_rank"]
    severity = "ALERT" if rank in ("critical", "warn") else "NORMAL"
    followups = [
        "查看 运营财务总览",
        "运行 应收应付账龄",
        "运行 子账总账对账",
    ]
    return {
        "answer_zh": "\n".join(parts),
        "tool_calls": res["tool_calls"],
        "evidence": {"healing": res},
        "followups": followups,
        "severity": severity,
    }


# ------------------------------------------------------------ 主入口


def ask(
    session,
    *,
    ledger_set_id: str,
    question_zh: str,
    as_of_date: date | None = None,
    dim_key: str = "customer",
) -> dict[str, Any]:
    """确定性 Copilot 入口：中文问题 → 意图路由 → 只读内核 → 结构化作答。

    返回 {answer_zh, intent, tool_calls, evidence, followups, severity}。
    severity=ALERT 时联动算子信号桥（operator.signal(ALERT)），复用现有跨进程桥，
    无需 websocket；推送 ≠ 执行，绝不写账。
    """
    question = (question_zh or "").strip()
    if not question:
        return {
            "answer_zh": "请描述你想了解的运营财务问题，例如："
                         "「示例科技 全貌」「谁逾期了」「子账总账对账」「应收敞口集中度」"
                         "「如果加速回款会怎样」「异常怎么处理」。",
            "intent": "empty", "tool_calls": [], "evidence": {},
            "followups": ["查看 运营财务总览", "如果加速回款会怎样", "异常怎么处理"],
            "severity": "NORMAL",
        }
    if as_of_date is None:
        as_of_date = date.today()

    intent, params = _route(session, ledger_set_id, question, as_of_date)

    if intent == "partner_profile":
        out = _answer_partner(params, session, ledger_set_id, as_of_date)
    elif intent == "reconcile":
        out = _answer_reconcile(params, session, ledger_set_id, as_of_date, dim_key)
    elif intent == "collections":
        out = _answer_collections(params, session, ledger_set_id, as_of_date)
    elif intent == "unmatched":
        out = _answer_unmatched(params, session, ledger_set_id, as_of_date, dim_key)
    elif intent == "foreign":
        out = _answer_foreign(params, session, ledger_set_id, as_of_date)
    elif intent == "what_if":
        out = _answer_whatif(params, session, ledger_set_id, as_of_date)
    elif intent == "healing":
        out = _answer_healing(params, session, ledger_set_id, as_of_date)
    else:  # overview
        out = _answer_overview(params, session, ledger_set_id, as_of_date, dim_key)

    # 严重项联动算子（E5）：复用跨进程信号桥，纯增值、失败静默
    if out.get("severity") == "ALERT":
        try:
            from kernel.operator import OperatorState, signal as _op_signal

            _op_signal(OperatorState.ALERT, source="copilot")
        except Exception:  # noqa: BLE001
            pass

    return {
        "answer_zh": out["answer_zh"],
        "intent": intent,
        "tool_calls": out["tool_calls"],
        "evidence": out["evidence"],
        "followups": out["followups"],
        "severity": out["severity"],
        "question_zh": question,
        "ledger_set_id": ledger_set_id,
        "as_of_date": as_of_date.isoformat(),
    }

"""情景推演 / what-if（Phase E / E3）：复用 kernel/forecast.py 的纯函数预测内核，做「基准 vs 杠杆」情景对比。

设计铁律（与 forecast.py 一致）：
- 预测是「物化视图」不是事实源；what-if 是在基准假设之上叠加「杠杆」（假设覆盖），
  全部由 forecast_statements 单一纯函数确定性推导，可回放、可审计、无 LLM、无随机；
- **只读、不改账、不建投影**（ADR-002）：所有数字来自 extract_seed_from_actuals（实际三表种子）
  + forecast_statements（外推），本模块不碰数据库写、不新增任何账本投影；
- ``tool_calls`` 逐条溯源：哪些杠杆改变了哪些指标、方向如何；
- 与 Copilot 衔接：``ask("如果加速回款会怎样")`` 即路由到本模块。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from kernel.forecast import Assumptions, extract_seed_from_actuals, forecast_statements
from kernel.db.models import LedgerSet

ZERO = Decimal("0")


# ------------------------------------------------------------ 预设杠杆
#
# 每个预设是「相对/绝对假设覆盖」构造器：输入基准 Assumptions，输出覆盖字段字典。
# 这样预设既能表达绝对目标值（如 ar_days=15），也能表达相对偏移（如毛利 +5pct）。

def _preset_builders() -> dict[str, Callable[[Assumptions], dict[str, Any]]]:
    return {
        # 加速回款：应收周转天数压到 15 天
        "ar_acceleration": lambda a: {"ar_days": 15},
        # 延长付款：占用供应商资金，应付周转天数拉到 45 天
        "ap_extension": lambda a: {"ap_days": 45},
        # 毛利压缩：毛利率下降 5 个百分点（封顶 0.95，避免倒挂）
        "margin_compression": lambda a: {
            "gross_margin": min(Decimal("0.95"), a.gross_margin + Decimal("0.05"))
        },
        # 增长停滞：收入零增长
        "growth_halt": lambda a: {"rev_growth": Decimal("0")},
        # 成本上升：付现费用率 +5 个百分点
        "cost_inflation": lambda a: {"opex_ratio": a.opex_ratio + Decimal("0.05")},
        # 资本开支激增：capex 占收入比拉到 10%
        "capex_surge": lambda a: {"capex_pct": Decimal("0.10")},
    }


_PRESET_DESC: dict[str, str] = {
    "ar_acceleration": "加速回款：应收周转天数压至 15 天（缩短 credit-to-cash）",
    "ap_extension": "延长付款：应付周转天数拉至 45 天（占用供应商资金）",
    "margin_compression": "毛利压缩：毛利率下降 5 个百分点",
    "growth_halt": "增长停滞：月度收入零增长",
    "cost_inflation": "成本上升：付现费用率上升 5 个百分点",
    "capex_surge": "资本开支激增：capex 占收入比拉至 10%",
}


def preset_lever_names() -> list[str]:
    """可用的预设杠杆名（供 Copilot / MCP 枚举）。"""
    return sorted(_preset_builders())


# ------------------------------------------------------------ 取值辅助


def _std(session: Session, ledger_set_id: str) -> str:
    ls = session.get(LedgerSet, ledger_set_id)
    return (ls.accounting_standard if ls else None) or "small_business"


def _next_period(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month >= 12 else (year, month + 1)


def _resolve_levers(
    derived: Assumptions, levers: list[Any] | None
) -> list[tuple[str, str, dict[str, Any]]]:
    """把 levers（预设名 / 自定义 dict）解析成 [(name, desc_zh, overrides_dict)]。"""
    builders = _preset_builders()
    out: list[tuple[str, str, dict[str, Any]]] = []
    for lv in levers or []:
        if isinstance(lv, str):
            if lv not in builders:
                raise ValueError(
                    f"未知预设杠杆 {lv!r}，可选：{', '.join(preset_lever_names())}"
                )
            out.append((lv, _PRESET_DESC[lv], dict(builders[lv](derived))))
        elif isinstance(lv, dict):
            out.append(("custom", "自定义假设覆盖", dict(lv)))
        else:
            raise ValueError(f"levers 元素类型不支持：{type(lv).__name__}")
    return out


def _apply_overrides(base: Assumptions, overrides: dict[str, Any]) -> Assumptions:
    """把覆盖字典应用到基准假设（仅保留 Assumptions 已有字段）。"""
    clean = {k: v for k, v in overrides.items() if k in Assumptions.__dataclass_fields__}
    return replace(base, **clean)


# ------------------------------------------------------------ impact 度量


def _last_metrics(forecast: dict) -> dict[str, Decimal]:
    """取预测期末（最后一期）的关键指标。"""
    p = forecast["periods"][-1]
    return {
        "closing_cash": Decimal(str(p["balance_sheet"]["cash"])),
        "net_profit": Decimal(str(p["income_statement"]["net_profit"])),
        "ar": Decimal(str(p["balance_sheet"]["ar"])),
        "ap": Decimal(str(p["balance_sheet"]["ap"])),
        "operating_cash_flow": Decimal(str(p["cash_flow"]["operating"])),
        "total_assets": Decimal(str(p["balance_sheet"]["total_assets"])),
    }


_METRIC_LABELS: dict[str, str] = {
    "closing_cash": "期末现金",
    "net_profit": "期末净利润（累计）",
    "ar": "期末应收",
    "ap": "期末应付",
    "operating_cash_flow": "期末经营现金流",
    "total_assets": "期末总资产",
}


def _impact(baseline: dict, variant: dict) -> dict[str, Any]:
    """单杠杆相对基准的期末指标差值。"""
    b = _last_metrics(baseline)
    v = _last_metrics(variant)
    rows = {}
    for k in b:
        base_v = b[k]
        var_v = v[k]
        delta = var_v - base_v
        pct = (delta / base_v * Decimal("100")) if base_v != ZERO else None
        rows[k] = {
            "label": _METRIC_LABELS[k],
            "baseline": str(base_v),
            "variant": str(var_v),
            "delta": str(delta),
            "pct": (str(pct.quantize(Decimal("0.01"))) if pct is not None else None),
        }
    return rows


def _impact_summary(
    baseline: dict, variants: dict[str, dict]
) -> dict[str, Any]:
    """跨所有杠杆，给出每个指标的基准 / 最优 / 最差（相对基准的最小/最大 delta）。"""
    b = _last_metrics(baseline)
    summary: dict[str, Any] = {}
    for k in b:
        base_v = b[k]
        deltas = [
            (Decimal(str(v["impact"][k]["delta"])), name)
            for name, v in variants.items()
        ]
        if not deltas:
            continue
        best = max(deltas, key=lambda x: x[0])
        worst = min(deltas, key=lambda x: x[0])
        summary[k] = {
            "label": _METRIC_LABELS[k],
            "baseline": str(base_v),
            "best_case_delta": str(best[0]),
            "best_case_lever": best[1],
            "worst_case_delta": str(worst[0]),
            "worst_case_lever": worst[1],
        }
    return summary


# ------------------------------------------------------------ 主入口


def what_if(
    session: Session,
    *,
    ledger_set_id: str,
    base_year: int,
    base_month: int,
    horizon: int = 12,
    levers: list[Any] | None = None,
    standard: str | None = None,
) -> dict[str, Any]:
    """情景推演（只读、确定性、可溯源）。

    以 ``base_year-base_month`` 上期末实际三表为种子，跑一个基准情景，再在基准假设之上
    叠加每个杠杆各跑一个情景，对比期末关键指标差值。

    - ``levers``：预设名列表（如 ``["ar_acceleration","margin_compression"]``）或自定义
      覆盖字典 ``{"ar_days": 15}`` 的混合；不传 → 返回纯基准（仅 baseline）。
    - 返回 baseline / variants(每杠杆) / impact(逐指标 delta) / impact_summary / tool_calls。
    - 全程只读，绝不写账；每条数字来自 forecast_statements 单一纯函数。
    """
    std = standard or _std(session, ledger_set_id)
    seed, derived = extract_seed_from_actuals(
        session, ledger_set_id, base_year, base_month, std
    )
    ny, nm = _next_period(base_year, base_month)

    baseline = forecast_statements(seed, derived, horizon, ny, nm, "base")

    resolved = _resolve_levers(derived, levers)
    variants: dict[str, Any] = {}
    tool_calls: list[dict] = [
        {"tool": "forecast_statements", "args": {"scenario": "base", "horizon": horizon}}
    ]
    for name, desc, ov in resolved:
        asm = _apply_overrides(derived, ov)
        fc = forecast_statements(seed, asm, horizon, ny, nm, name)
        variants[name] = {
            "description_zh": desc,
            "assumption_overrides": {k: str(v) for k, v in ov.items()},
            "forecast": fc,
            "impact": _impact(baseline, fc),
        }
        tool_calls.append(
            {
                "tool": "forecast_statements",
                "args": {"scenario": name, "assumption_overrides": {k: str(v) for k, v in ov.items()}},
            }
        )

    return {
        "base_period": {"year": base_year, "month": base_month},
        "horizon": horizon,
        "standard": std,
        "baseline": baseline,
        "variants": variants,
        "impact_summary": _impact_summary(baseline, variants),
        "tool_calls": tool_calls,
        "basis": (
            "extract_seed_from_actuals（实际三表种子）→ forecast_statements（纯函数外推）；"
            "不改账、不建投影（ADR-002）"
        ),
    }

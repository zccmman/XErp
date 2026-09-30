"""TypeSafe Jev 云端决策适配器（可选 · 默认关闭）。

XErp JEV 决策引擎默认使用本地 100% 确定性内核（kernel.decide）。本适配器是
**可插拔云端后端**：仅当用户在 Web 显式开启「云端模式」并确认数据出境授权、
且已配置 TYPESAFE_API_KEY 时，才把决策的「聚合指标」发送至 TypeSafe 云
（api.typesafe.ai）获取校准置信度与独立第二意见；本地判定始终为权威值/severity，
云端仅做置信度叠加与分歧标记。云端不可用时安全回退本地。

铁律（继承 XErp 内核约束 + D8 边界契约）：
- 绝不写账 / 不落 Balance 投影 / 不裸改凭证状态（守 D8 边界）。
- 发送的是**决策聚合指标**（state=本地 evidence），绝非原始凭证明细或对手方 PII。
- 无任何密钥硬编码；密钥仅来自环境变量 TYPESAFE_API_KEY（本地 .env，不入库）。
- 本文件位于 kernel/adapters/，由外围（webapp / MCP）调用；核心 kernel.decide 不依赖本文件。
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any

from kernel.decide import Decision, Severity, _dispatch_local, set_cloud_backend
from kernel.db.models import JevCloudSetting

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MODEL = "jev-latest"
_TIMEOUT = 15

# 测试可 patch 此引用以模拟网络调用
_urlopen = urllib.request.urlopen


class TypeSafeError(RuntimeError):
    """TypeSafe 云端调用错误：code / message_zh / details 可直接被上层消费。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


# ----------------------------------------------------------------- 配置/网络


def is_configured() -> bool:
    """是否已配置 TYPESAFE_API_KEY（来自环境变量，本地 .env）。"""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    return bool(key)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (datetime,)):
        return o.isoformat()
    return str(o)


def _post(state: Any, questions: dict, api_key: str, model: str = _MODEL) -> dict:
    """执行 TypeSafe System One HTTP POST（stdlib urllib，零第三方依赖）。

    任何网络/解析异常 → TypeSafeError。供 call_system_one 与测试 mock 使用。
    """
    body = json.dumps(
        {"state": state, "model": model, "questions": questions},
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 —— 网络/超时统一归并为云端错误
        raise TypeSafeError("NETWORK_ERROR", f"调用 TypeSafe 失败：{e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise TypeSafeError("BAD_RESPONSE", f"TypeSafe 返回非 JSON：{e}") from e


def call_system_one(
    state: Any, questions: dict, api_key: str | None = None, model: str = _MODEL
) -> dict:
    """调用 TypeSafe System One 的对外入口（含 key 校验）。

    POST https://api.typesafe.ai/v1/systemone；Authorization: Bearer <API_KEY>。
    返回解析后的 JSON（含 answers）。任何网络/解析异常 → TypeSafeError。
    """
    key = (api_key or os.environ.get("TYPESAFE_API_KEY", "")).strip()
    if not key:
        raise TypeSafeError("NOT_CONFIGURED", "未配置 TYPESAFE_API_KEY，无法调用 TypeSafe")
    return _post(state, questions, key, model)


# --------------------------------------------------- 云端问题构造 + 答案映射


# 每个决策对应的 Jev 问题 id
_QID = {
    "budget_variance": "grade",
    "risk_severity": "grade",
    "ap_open_health": "grade",
    "duplicate_voucher": "likely_dup",
    "expense_compliance": "compliant",
    "approval_route": "route",
}

# 云端 choice 选项 → 本地 Severity
_OPT_SEV = {
    "green": "low", "yellow": "medium", "red": "high",
    "none": "info", "low": "low", "medium": "medium", "high": "high",
    "healthy": "low", "watch": "medium", "risk": "high",
    "line_manager": "low", "finance_manager": "medium", "general_manager": "high",
}


def build_cloud_question(name: str, base: Decision) -> tuple[Any, dict]:
    """由本地基线 Decision 的 evidence 构造 TypeSafe state + typed question。

    只发送聚合指标，不含原始凭证明细/对手方 PII。无对应云端问题 → 返回 ({}, {})。
    """
    ev = base.evidence
    if name == "budget_variance":
        state = {"totals": ev.get("totals"), "rows": ev.get("rows"), "worst_local": base.value}
        questions = {
            "grade": {
                "type": "choice",
                "instructions": ("基于下列预算 v.s. 实际差异（variance_pct 为偏差率），"
                                "判定整体预算差异等级：正常/关注/预警。仅依据数据，不臆测。"),
                "criteria": {
                    "green": "偏差率整体 ≤5%，正常",
                    "yellow": "存在科目偏差率 5%-15%，关注",
                    "red": "存在科目偏差率 >15%，预警",
                },
            }
        }
        return state, questions
    if name == "risk_severity":
        state = {
            "counts": ev.get("counts"), "raw_score": ev.get("raw_score"),
            "findings": ev.get("findings"),
        }
        questions = {
            "grade": {
                "type": "choice",
                "instructions": "基于下列规则命中统计，评估该凭证风险严重度等级。",
                "criteria": {
                    "none": "无风险", "low": "低风险",
                    "medium": "中风险", "high": "高风险",
                },
            }
        }
        return state, questions
    if name == "duplicate_voucher":
        state = {
            "target_total": ev.get("target_total"),
            "target_summary": ev.get("target_summary"),
            "duplicates": ev.get("duplicates"),
            "window_days": ev.get("window_days"),
        }
        questions = {
            "likely_dup": {
                "type": "noul",
                "instructions": ("给定同创建人、同金额、同摘要、时间窗内的其他已入账凭证清单，"
                                "判断该凭证是否疑似重复报销/重复记账。"),
            }
        }
        return state, questions
    if name == "ap_open_health":
        state = {
            "totals": ev.get("totals"),
            "overdue": ev.get("overdue"),
            "watch": ev.get("watch"),
        }
        questions = {
            "grade": {
                "type": "choice",
                "instructions": "基于应付未清项账龄分布（overdue=逾期>90天, watch=61-90天），评估健康度。",
                "criteria": {
                    "healthy": "无逾期/关注，健康",
                    "watch": "存在61-90天关注项",
                    "risk": "存在>90天逾期，风险",
                },
            }
        }
        return state, questions
    if name == "expense_compliance":
        state = {"flags": ev.get("flags")}
        questions = {
            "compliant": {
                "type": "noul",
                "instructions": "给定该费用凭证的合规标记（超标/非工作时段/重复摘要），判断是否合规。",
            }
        }
        return state, questions
    if name == "approval_route":
        state = {"options": ev.get("options"), "ranked": ev.get("ranked")}
        questions = {
            "route": {
                "type": "choice",
                "instructions": "基于凭证总额阈值与审批层级优先序，判定应路由到哪级审批。",
                "criteria": {
                    "line_manager": "直属主管(≤5000)",
                    "finance_manager": "财务经理(≤50000)",
                    "general_manager": "总经理(>50000)",
                },
            }
        }
        return state, questions
    return None, {}


def map_cloud_answer(name: str, answers: dict, base: Decision) -> dict:
    """把 TypeSafe 答案映射回 JEV 结构（本地 severity 仍权威；云端只供置信度/分歧）。"""
    qid = _QID.get(name, "grade")
    a = answers.get(qid, {}) or {}
    sev = base.severity
    jev_value: Any = None
    jev_conf: Any = None
    atype = a.get("type")
    if atype == "choice":
        choice = a.get("choice")
        jev_value = choice
        jev_conf = a.get("confidence")
        if choice in _OPT_SEV:
            sev = Severity(_OPT_SEV[choice])
    elif atype == "noul":
        noul = float(a.get("noul", 0.0))
        jev_conf = noul
        if name == "duplicate_voucher":
            jev_value = "疑似重复" if noul >= 0.5 else "唯一"
            sev = Severity.HIGH if noul >= 0.5 else Severity.LOW
        else:  # expense_compliance
            jev_value = "合规" if noul < 0.5 else "不合规"
            if noul < 0.5:
                sev = Severity.LOW
            else:
                flags = base.evidence.get("flags", []) or []
                sev = (
                    Severity.HIGH
                    if any(f in ("重复摘要", "超标") for f in flags)
                    else Severity.MEDIUM
                )
    elif atype == "score":
        jev_value = a.get("score")
        jev_conf = a.get("confidence")
    return {
        "jev_value": jev_value,
        "jev_severity": sev,
        "jev_confidence": jev_conf,
        "raw": a,
    }


# ------------------------------------------------------- 云端 / 路由 决策入口


def run_decision_cloud(name: str, session, **params) -> Decision:
    """云端决策：本地先算基线（权威值/severity），再向 TypeSafe 取校准置信度 + 第二意见。

    本地判定始终为权威；云端仅叠加校准 confidence 并标记「本地↔云端分歧≥2档」供人工复核。
    非云端支持的决策 → 直接返回本地基线（evidence.backend=local）。
    """
    if not is_configured():
        raise TypeSafeError("NOT_CONFIGURED", "未配置 TYPESAFE_API_KEY，无法使用云端模式")
    base = _dispatch_local(name, session, **params)
    state, questions = build_cloud_question(name, base)
    if not questions:
        base.evidence["backend"] = "local"
        base.evidence["cloud_skipped"] = "unsupported_decision"
        return base
    key = os.environ["TYPESAFE_API_KEY"].strip()
    resp = call_system_one(state, questions, api_key=key)
    answers = (resp or {}).get("answers", {})
    mapped = map_cloud_answer(name, answers, base)
    if mapped["jev_confidence"] is not None:
        try:
            base.confidence = Decimal(str(mapped["jev_confidence"]))
        except Exception:  # noqa: BLE001
            pass
    base.evidence["backend"] = "typesafe"
    base.evidence["jev"] = {
        "value": mapped["jev_value"],
        "severity": mapped["jev_severity"].value,
        "confidence": mapped["jev_confidence"],
        "raw": mapped["raw"],
    }
    if abs(mapped["jev_severity"].rank - base.severity.rank) >= 2:
        base.human_review_required = True
        base.basis = list(base.basis) + ["TypeSafe 云端与本地判定分歧≥2档，建议人工复核"]
    return base


def _cloud_dispatch(name: str, session, *, ledger_set_id: str, **params) -> Decision:
    """注册到核心 run_decision 的云端后端（由 set_cloud_backend 注入）。

    门控：账套已开启云端模式 + 已数据出境授权 + TYPESAFE_API_KEY 已配置，才走云端；
    否则/云端异常 → 返回本地基线（evidence.backend=local，异常时 cloud_fallback=True）。
    本地判定始终为权威，云端仅叠加校准置信度与分歧标记。
    """
    params.pop("ledger_set_id", None)
    setting = session.get(JevCloudSetting, ledger_set_id)
    use_cloud = (
        setting is not None
        and setting.backend == "typesafe"
        and bool(setting.cloud_consent)
        and is_configured()
    )
    if use_cloud:
        try:
            d = run_decision_cloud(name, session, ledger_set_id=ledger_set_id, **params)
            d.evidence["backend"] = "typesafe"
            return d
        except TypeSafeError:
            d = _dispatch_local(name, session, ledger_set_id=ledger_set_id, **params)
            d.evidence["backend"] = "local"
            d.evidence["cloud_fallback"] = True
            return d
    d = _dispatch_local(name, session, ledger_set_id=ledger_set_id, **params)
    d.evidence["backend"] = "local"
    return d


def resolve_decision(name: str, session, *, ledger_set_id: str, **params) -> tuple[Decision, str]:
    """JEV 决策分发便利包装：返回 (Decision, backend)。

    直接复用核心 run_decision（其已按注册后端/账套授权自动路由本地或云端）。
    backend ∈ {"local","typesafe"}，取自 Decision.evidence.backend。
    """
    params.pop("ledger_set_id", None)
    d = run_decision(name, session, ledger_set_id=ledger_set_id, **params)
    return d, d.evidence.get("backend", "local")


# 运行时注入云端后端到核心（核心层不静态依赖本适配器，满足 D8 边界契约）。
# 仅当本适配器被外围（webapp / MCP）导入时注册；核心单独使用时 CLOUD_BACKEND=None → 纯本地。
set_cloud_backend(_cloud_dispatch)

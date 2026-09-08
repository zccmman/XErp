"""解释层（超级AI总账 · 阶段0 第三步）：把机器结论"讲给人听"。

单一职责：消费内核已有输出（异常侦测 findings、抽检池、一键回放时间线），
补一层**确定性的中文翻译**——规则中文名、等级中文名、每条建议动作、
整体结论。不做任何二次推断：所有事实都来自内核原始字段，本模块只做
「码 → 人话」的映射与汇总口径，改账逻辑零涉及。

设计约束
--------
- **只增不改**：作为 ``guide`` 块附在既有 MCP 工具返回里，原始字段原样保留
  （旧客户端/测试不受影响）；不新增工具、不动 profiles 档位计数。
- **断路器结论以状态表为准**：是否"已冻结 Agent"由 server 层读
  ``breaker_is_open`` 传入，本模块不自行查库——避免第二个真源。
"""

from __future__ import annotations

# ---------- 异常侦测 ----------

#: 规则 → 中文名 + 人工建议动作。新加规则必须同步补这两条，
#: 否则 explain 会把未知规则原样透出（fallback 已兜底，不炸）。
ANOMALY_RULES: dict[str, dict[str, str]] = {
    "large_amount": {
        "zh": "大额凭证",
        "action": "人工复核该凭证的真实性与审批链，确认业务背景后正常流转",
    },
    "off_hours": {
        "zh": "非常规时间操作",
        "action": "向制单主体确认是否本人操作，排除账号冒用",
    },
    "rare_account": {
        "zh": "罕见科目",
        "action": "确认科目选用是否正确，防止串户或误用一级科目",
    },
    "freq_spike": {
        "zh": "当日频率激增",
        "action": "检查是否存在批量误操作或重复提交，必要时暂停该主体制单",
    },
    "llm_suspicious": {
        "zh": "AI 判定可疑",
        "action": "结合 AI 给出的理由人工复核凭证内容",
    },
}

#: severity → 中文等级
SEVERITY_ZH: dict[str, str] = {
    "info": "提示",
    "warn": "警告",
    "critical": "严重",
}

#: 抽检状态 → 中文
AUDIT_STATUS_ZH: dict[str, str] = {
    "pending": "待抽检",
    "passed": "抽检通过",
    "reversed": "已推翻冲销",
}


def _finding_zh(f: dict) -> dict:
    rule = f.get("rule", "")
    meta = ANOMALY_RULES.get(rule)
    return {
        "rule": rule,
        "rule_zh": meta["zh"] if meta else rule,
        "severity": f.get("severity", ""),
        "severity_zh": SEVERITY_ZH.get(f.get("severity", ""), f.get("severity", "")),
        "message": f.get("message", ""),
        "action": meta["action"] if meta else "请人工复核该凭证",
    }


def explain_findings(findings: list[dict], *,
                     breaker_tripped: bool | None = None) -> dict:
    """把 anomaly_scan 的 findings 翻成整体结论 + 逐条人话。

    verdict 判定口径（确定性）：
        无命中            → clean     未检出异常
        命中且断路器跳闸  → frozen    涉事 Agent 自治已被冻结，需人工复核后解除
        命中且无跳闸      → attention 建议关注，业务可继续
    """
    items = [_finding_zh(f) for f in findings]
    if not items:
        return {
            "verdict": "clean",
            "verdict_zh": "未检出异常",
            "summary_zh": "规则与 AI 双通道均未发现可疑点，凭证可正常流转。",
            "items": [],
        }
    if breaker_tripped:
        return {
            "verdict": "frozen",
            "verdict_zh": "需要人工处理（已冻结）",
            "summary_zh": (
                "命中跳闸规则，涉事 Agent 的自治过账已被断路器冻结；"
                "请逐条复核下列异常，人工确认后用 anomaly_release 解除。"
                "人类用户的操作不受影响。"
            ),
            "items": items,
        }
    return {
        "verdict": "attention",
        "verdict_zh": "建议关注",
        "summary_zh": "发现以下提示/警告级异常，请逐条确认；业务可继续办理。",
        "items": items,
    }


# ---------- 一键回放 ----------

#: 事件类型 → 中文（键 = kernel.events.E 枚举的落链字符串；未知码原样透出）
EVENT_TYPE_ZH: dict[str, str] = {
    "VOUCHER_CREATED": "创建凭证",
    "VOUCHER_PUSHED": "提交审核",
    "VOUCHER_APPROVED": "审批通过",
    "VOUCHER_REJECTED": "审批驳回",
    "VOUCHER_WITHDRAWN": "撤回重填",
    "VOUCHER_POSTED": "过账入账",
    "VOUCHER_CANCELLED": "作废",
    "VOUCHER_SIGNED": "签字",
    "OPENING_BALANCE_IMPORTED": "导入期初余额",
    "OPENING_BALANCE_REVERSED": "红字冲销旧期初",
    "CLOSING_EXECUTED": "期末结转",
    "AUTONOMOUS_POSTED": "Agent 自治过账（额度内直接入账）",
    "AUTONOMOUS_REVIEWED": "人工抽检裁决",
    "AUTONOMOUS_REVERSED": "抽检推翻（红字冲销）",
    "AGENT_DECISION": "AI 决策留痕",
    "AGENT_ANOMALY_DETECTED": "异常侦测命中",
    "BREAKER_TRIPPED": "断路器跳闸",
    "BREAKER_RELEASED": "断路器解除",
}


def explain_replay(replay_res: dict) -> dict:
    """给回放时间线逐行加中文名，并给出一段"这一笔的来龙去脉"摘要。"""
    steps = []
    for ev in replay_res.get("timeline", []):
        et = ev.get("event_type", "")
        steps.append({**ev, "event_zh": EVENT_TYPE_ZH.get(et, et)})
    actor_chain = []
    for s in steps:
        who = s.get("actor") or "（系统）"
        label = s["event_zh"]
        if who not in actor_chain:
            actor_chain.append(who)
    narr = (
        f"凭证 {replay_res.get('voucher_no', '?')}（{replay_res.get('status', '?')}）"
        f"共 {replay_res.get('event_count', len(steps))} 条留痕，"
        f"涉及主体 {len(actor_chain)} 个。逐行解读见 steps。"
    )
    return {"narrative_zh": narr, "steps": steps}


# ---------- 抽检池 ----------


def explain_audit_pool(pool_res: dict) -> dict:
    """给抽检池加中文摘要：还有几张待抽检、该先看哪张。"""
    pool = pool_res.get("pool", [])
    pending = pool_res.get("pending", 0)
    if not pool:
        summary = "暂无自治过账凭证，抽检池为空——Agent 额度内自主入账后会自动进入这里。"
    elif pending:
        latest = pool[0]
        summary = (
            f"抽检池共 {len(pool)} 张，其中 {pending} 张待人工抽检。"
            f"建议从最新的 {latest.get('voucher_no', '?')} 开始看。"
        )
    else:
        summary = f"抽检池共 {len(pool)} 张，已全部裁决完毕，无待办。"
    items = [{**x, "status_zh": AUDIT_STATUS_ZH.get(x.get("audit_status", ""),
                                                   x.get("audit_status", ""))}
             for x in pool]
    return {"summary_zh": summary, "items": items}

"""工具分层（tool profiles）：同一套内核，三种暴露面。

为什么分层：48 个工具全量暴露给 AI，上下文成本和误调用概率都高；而个人/代账
用户真正用到的不到三分之一。裁剪通过 mcp.json 的 `disabledTools` 完成——
**改配置不改代码，内核一行不动**，因此三档共用同一个 MCP Server、同一份数据。

三档：

    minimal  极简  15 个：建账 → 制单 → 审核 → 记账 → 查账 → 两张主表
    standard 标准  29 个：+ 驳回/撤回/反记账/多级签字/月结/往来/银行/OCR
    pro      专业  49 个：全量（自治过账、异常扫描、适配器、转账模板、双审批通道）

归类铁律（由 tests/test_tool_profiles.py 钉死）：

    1. 三个集合**互不相交**且并集 == 运行时内省到的工具全集；
    2. minimal ⊆ standard ⊆ pro；
    3. 新增工具必须显式写进下面三个元组之一，否则并集对不上、测试红。

第 3 条是重点：它把"忘了归类"变成一次可捕获的失败，而不是上线后 AI 看不到
新工具却没人知道为什么。

用法（脚本层）：
    from xerp_mcp.profiles import disabled_for
    disabled_for("minimal", all_tool_names)  → ["adapter_ingest", ...]
"""

from __future__ import annotations

# ------------------------------------------------------------ 极简：主链路


MINIMAL: tuple[str, ...] = (
    # 会话与建账
    "get_session_context",
    "init_ledger_set",
    "ensure_period",
    "list_accounts",
    "import_opening_balances",
    # 制单主链路（草稿 → 待审 → 已审 → 已记账）
    "create_voucher",
    "push_voucher",
    "approve_voucher",
    "post_voucher",
    "get_voucher",
    # 查账与报表
    "query_balances",
    "report_balance_sheet",
    "report_income_statement",
    # 怀旧层：结账体检 + 常用摘要（高频、只读、零风险）
    "precheck_close",
    "suggest_summaries",
)


# ------------------------------------------- 标准：中小企业日常全链路的补齐项


STANDARD_EXTRA: tuple[str, ...] = (
    # 凭证逆向动作
    "reject_voucher",
    "withdraw_voucher",
    "cancel_post_voucher",
    # 多级签字（D7）：出纳/主管等签字位，全部签完自动审批通过
    "sign_voucher",
    # 明细与勾稽
    "ledger_detail",
    "reconcile_ledger",
    # 月结
    "report_cash_flow",
    "close_period",
    "open_next_period",
    "monthend_run",
    # 往来与资金
    "partner_balances",
    "bank_import_csv",
    "bank_reconcile",
    # 票据
    "ocr_ingest_invoice",
)


# ------------------------------------------- 专业：仅专业档（风险/集成/实验性）


PRO_ONLY: tuple[str, ...] = (
    # 废弃别名（仅作已发出交付包的兜底，不该被 AI 主动调用）
    "get_workspace",
    # 审批通道
    "feishu_send_approval",
    "wecom_send_approval",
    "wecom_send",
    "wecom_finish_card",
    # 外部系统适配器
    "adapter_list",
    "adapter_preview",
    "adapter_ingest",
    "adapter_register",
    # 转账模板
    "transfer_define",
    "transfer_list",
    "transfer_run",
    # 自治与风控（会自行过账，属"授权后才开"的能力）
    "autonomy_post",
    "autonomy_audit_list",
    "autonomy_audit_review",
    "autonomy_replay",
    "anomaly_scan",
    "anomaly_release",
    "log_agent_decision",
    # 质量度量
    "ocr_accuracy_report",
)


#: 档位 → 说明。顺序即"由小到大"。
PROFILES: dict[str, str] = {
    "minimal": "极简：建账、制单、审核、记账、查账、两张主表",
    "standard": "标准：极简 + 驳回撤回、多级签字、月结、往来、银行对账、票据识别",
    "pro": "专业：全量，含自治过账、风控、适配器、转账模板、双审批通道",
}

#: 档位 → 该档包含哪些集合。pro 为 None 表示"不裁剪"。
_TIERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "minimal": (MINIMAL,),
    "standard": (MINIMAL, STANDARD_EXTRA),
    "pro": (MINIMAL, STANDARD_EXTRA, PRO_ONLY),
}


def known_tools() -> tuple[str, ...]:
    """按档位顺序返回全部已知工具名（不依赖 MCP 运行时）。"""
    return MINIMAL + STANDARD_EXTRA + PRO_ONLY


def enabled_for(profile: str) -> tuple[str, ...]:
    """某档启用的工具名（升序返回，便于比较与落盘稳定）。"""
    if profile not in _TIERS:
        raise ValueError(
            f"未知档位 {profile!r}，可选：{', '.join(PROFILES)}"
        )
    names: list[str] = []
    for tier in _TIERS[profile]:
        names.extend(tier)
    return tuple(sorted(names))


def disabled_for(profile: str, all_tools=None) -> list[str]:
    """某档需要写进 mcp.json `disabledTools` 的工具名。

    all_tools 为运行时内省到的工具全集；不传则用本模块的静态清单。
    取差集而非直接存黑名单——内核加了新工具时，未归类的工具会先被
    测试拦下，而不是悄悄出现在极简档里。
    """
    enabled = set(enabled_for(profile))
    universe = set(all_tools) if all_tools else set(known_tools())
    return sorted(universe - enabled)

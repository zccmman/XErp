"""工具分层（tool profiles）：同一套内核，三种暴露面。

为什么分层：91 个工具全量暴露给 AI，上下文成本和误调用概率都高；而个人/代账
用户真正用到的不到三分之一。裁剪通过 mcp.json 的 `disabledTools` 完成——
**改配置不改代码，内核一行不动**，因此三档共用同一个 MCP Server、同一份数据。

三档：

    minimal  极简      19 个：建账 → 制单 → 审核 → 记账 → 查账 → 两张主表 + 状态引导 + 科目本体 + 算子状态
    standard 标准  61 个：+ 驳回/撤回/反记账/多级签字/月结/往来/银行/OCR/三表预测/专项核算/账本精灵主动推送/AI风险预警/报税准备/审计追踪/票据入账预览门禁/应收应付对账单/应收应付账龄/应收应付未清项/核销草稿/授信额度设置/信用敞口扫描/逾期催收草稿/收款自动匹配/汇兑损益重估/子账总账对账/运营财务画像/图谱指标/实时Copilot/情景推演/异常自愈建议/WB审批通道/WB成员绑定
    pro      专业  91 个：全量（自治授权/过账、异常扫描、适配器、转账模板、双审批通道、多主体合并报表、合并血缘下钻、合并层级标注、内部往来配对草稿、长投权益抵销草稿、合并现金流量表、合并现金流抵消草稿、应收应付核销落地、汇兑损益重估落库）

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
    # 怀旧层：结账体检 + 常用摘要 + 账套状态引导（高频、只读、零风险）
    "precheck_close",
    "preview_closing",
    "suggest_summaries",
    "month_end_guide",
    # 本体层（阶段1）：科目语义查询（高频、只读、零风险，AI 引导的依据）
    "subject_semantics",
    # AI Runtime 具象（ADR-007 迭代3）：算子状态读写（只动状态信号，不碰账）
    "ai_runtime_state",
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
    # GB/T 24589.1-2024 审计数据接口导出（合规护城河，只读、零风险）
    "export_gbt24589",
    # 月结
    "report_cash_flow",
    "close_period",
    "open_next_period",
    "monthend_run",
    # 往来与资金
    "partner_balances",
    # 应收应付深化（对账单 + 账龄 + 未清项 + 核销草稿 + 信用敞口 + 催收草稿 + 授信额度）：
    # 与 partner_balances / open_items 同一取数口径，单一真源；只读/配置写、零风险
    "arap_statement",
    "arap_aging",
    "arap_open_items",
    "arap_propose_clearing",
    # 信用管理（G2）：授信额度配置（写，仅 Boss 设置）+ 信用敞口扫描（只读，派生自
    # open_items，非账本投影，守 ADR-002）+ 逾期催收草稿（只读，生成 L1/L2/L3 话术不代发）
    "arap_set_credit_limit",
    "arap_credit_exposure",
    "arap_collections_draft",
    # AI 收款自动匹配（Phase C / G4）：承接 open_items/record_clearing，多信号匹配 +
    # 可解释置信度（只读草稿，确认后经 arap_apply_clearing 落库，HITL）
    "arap_propose_receipt_match",
    "bank_import_csv",
    "bank_reconcile",
    # 票据
    "ocr_ingest_invoice",
    # 票据入账预览门禁（O9 统一预览-确认-执行）：与 ocr_ingest_invoice 共用
    # 同一决策与取数（build_lines 单一真源），预览所见即所入账
    "ocr_preview",
    # 三表前向预测（P1-01 预测）：以实际数为种子外推未来，best/base/worst 多情景
    "forecast_statements",
    # 业务语言向导（S1）：自然语言场景 → 候选分录 + 逐行解释（只读、零风险）
    "wizard_scenarios",
    "wizard_propose",
    # ② 专项核算（外币/辅助）：外币试算 + 按维度透视余额（只读、零风险）
    "report_aux",
    "foreign_trial_balance",
    # 月结自动化 · 子账↔总账对账（Phase D / G9）：应收/应付控制科目 vs 客户/供应商
    # 明细余额之和，差异即漏挂往来单位的失配；只读、复用 arap 单一真源
    "reconcile_subledger_gl",
    # 运营财务本体 + 实时 Copilot（Phase E / E1·E2）：运营财务图谱（往来单位一站式
    # 画像 + 图谱指标）+ 确定性自然语言 Copilot；全部只读、复用 arap/credit 单一真源，
    # 不改账、不建投影；Copilot 严重项经算子信号桥置 ALERT（推送≠执行）
    "operating_partner_profile",
    "operating_graph_metrics",
    "copilot_ask",
    # 情景推演 + 异常自愈建议（Phase E / E3·E4）：复用 forecast 纯函数做基准 vs 杠杆
    # 对比（只读、不改账）；基于 anomaly.rule_scan 出 HITL 整改动作清单（只读不跳闸、
    # 绝不自动修复）；二者均不改账、不建投影（ADR-002）
    "what_if_simulation",
    "anomaly_healing_suggestions",
    # 月结自动化 · 外币重估（Phase D / G7）：期末汇兑损益重估只读草稿，落库由
    # fx_revaluation_create（PRO_ONLY, HITL）执行；对标 SAP/Oracle 未实现汇兑损益
    "fx_revaluation_draft",
    # 账本精灵 7×24 主动推送（O18，智能体层）：统一取数、主动提醒、只读零风险
    "sprite_push",
    # AI 风险预警（v2.1 / B1）：只读扫描 SMB 财务风险，只告警不执行
    "risk_scan",
    # 小规模纳税人增值税及附加税费季报准备（v2.1 / B3）：只读生成申报草稿
    "tax_vat_prep",
    # 审计追踪（v2.1 / B2）：不可篡改事件账本 → 人类可读、可证明的审计报告
    "audit_trail",
    # WB 原生审批闭环（P0-4）：与飞书/企微并列的第三审批通道，WB 是主入口故置于
    # 标准档；仅做通知 + 身份解析 + 路由留痕（写 VOUCHER_ROUTED），不改凭证状态；
    # 审批终态由审批人在 WB 内回复后经 transition 回流内核，红线全部复用
    "workbuddy_send_approval",
    # WB 成员身份绑定（P0-3）：WB user id ↔ 内核人主体映射键，绑定后该成员可作审批人
    "workbuddy_bind_member",
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
    "autonomy_authorize",
    "autonomy_post",
    "autonomy_audit_list",
    "autonomy_audit_review",
    "autonomy_replay",
    "anomaly_scan",
    "anomaly_release",
    "log_agent_decision",
    # 质量度量
    "ocr_accuracy_report",
    # 应收应付核销落地（写动作，HITL）：仅专业档暴露，未清项/核销草稿只读在 standard
    "arap_apply_clearing",
    # 月结自动化 · 外币重估落库（Phase D / G7）：把重估草稿落成 PUSHED 凭证，HITL 写动作
    # （绝不自动过账）；只读草稿 fx_revaluation_draft 在 standard
    "fx_revaluation_create",
    # 多主体合并报表（v2.0）：集团层只读聚合，参数化、不建表
    "consolidate_reports",
    # 合并血缘下钻（阶段0 / P0-1）：合并数 → 主体 → 源凭证，端到端可追溯
    "consolidate_lineage",
    # 合并分录层级标注（阶段0 / P0-2）：SAP posting level 透明化（PL00/PL20）
    "consolidate_posting_levels",
    # 内部往来自动配对草稿（阶段1 / Oracle ICP）：集团级净额配对，Boss 确认后抵销
    "consolidate_propose_icp",
    # 长投-权益抵销草稿（阶段1 / SAP COI）：配比长投与子公司权益，推商誉/少股
    "consolidate_propose_coi",
    # 合并现金流量表（阶段2）：汇总各账套现金流 + 折算 + 勾稽，Boss 显式抵消
    "consolidate_cash_flow",
    # 合并现金流内部往来抵消草稿（阶段2）：权益性投资镜像配对，Boss 确认后抵销
    "consolidate_propose_cash_flow",
)


#: 档位 → 说明。顺序即"由小到大"。
PROFILES: dict[str, str] = {
    "minimal": "极简：建账、制单、审核、记账、查账、两张主表",
    "standard": "标准：极简 + 驳回撤回、多级签字、月结、往来、银行对账、票据识别",
    "pro": "专业：全量，含自治过账、风控、适配器、转账模板、双审批通道、合并报表增强、合并现金流量表",
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

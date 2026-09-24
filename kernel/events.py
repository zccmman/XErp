"""领域事件类型注册表（复盘 D1）：事件名的单一事实来源。

背景：事件类型此前以字符串字面量散落各处，落链与查询各写一份，
前缀错位（voucher.autonomous.posted vs agent.autonomous.posted）
曾导致额度统计恒为 0——单测抓出后建立本注册表。

约定：
1. 落链与查询**只允许引用本模块常量**，禁止裸字符串；
2. 命名 `<域>.<名词>.<动词过去式>`，域前缀即限界上下文名；
3. 新增事件先在这里登记（含中文说明），再写代码。
"""

from __future__ import annotations

from enum import StrEnum


class E(StrEnum):
    """领域事件类型。值即落链字符串，可直接用于比较与查询。"""

    # ---------- 凭证生命周期（Ledger Core） ----------
    VOUCHER_CREATED = "VOUCHER_CREATED"
    VOUCHER_PUSHED = "VOUCHER_PUSHED"
    VOUCHER_APPROVED = "VOUCHER_APPROVED"
    VOUCHER_POSTED = "VOUCHER_POSTED"
    VOUCHER_CANCELLED = "VOUCHER_CANCELLED"
    # PUSHED → DRAFT 有两个业务语义，按执行人区分（G1 制单闭环）：
    # - VOUCHER_REJECTED：审批人驳回（他人执行，须填原因）
    # - VOUCHER_WITHDRAWN：制单人主动撤回（本人执行，原因可选）
    # 二者审计含义不同：驳回说明单据有问题被退回；撤回只是制单人反悔。
    VOUCHER_REJECTED = "VOUCHER_REJECTED"
    VOUCHER_WITHDRAWN = "VOUCHER_WITHDRAWN"
    # D7 多级签字：每个签字位的签署事件（Subject×角色×凭证）。
    # 全部 required_signers 签署 approved 后，由 kernel.signing 触发 VOUCHER_APPROVED。
    VOUCHER_SIGNED = "VOUCHER_SIGNED"
    # P0-4：审批请求经某条外部通道（WorkBuddy / 飞书 / 企微）送达审批人。
    # 仅记录「经哪条通道送达」，不改 voucher.status（终态仍由 transition 守护）。
    VOUCHER_ROUTED = "VOUCHER_ROUTED"

    # ---------- 账务规则执行 ----------
    OPENING_BALANCE_IMPORTED = "OPENING_BALANCE_IMPORTED"
    OPENING_BALANCE_REVERSED = "OPENING_BALANCE_REVERSED"
    CLOSING_EXECUTED = "CLOSING_EXECUTED"

    # ---------- Agent 治理（Autonomy/Audit） ----------
    AGENT_DECISION = "AGENT_DECISION"
    AGENT_MONTHEND_RUN = "AGENT_MONTHEND_RUN"
    AGENT_ANOMALY_DETECTED = "AGENT_ANOMALY_DETECTED"
    BREAKER_TRIPPED = "BREAKER_TRIPPED"
    BREAKER_RELEASED = "BREAKER_RELEASED"
    AUTONOMOUS_POSTED = "AUTONOMOUS_POSTED"
    AUTONOMOUS_REVIEWED = "AUTONOMOUS_REVIEWED"
    AUTONOMOUS_REVERSED = "AUTONOMOUS_REVERSED"
    # O11 红线：自治授权令牌（每会话显式授权，人是 Boss 硬门禁）
    AUTONOMY_GRANT_ISSUED = "AUTONOMY_GRANT_ISSUED"
    AUTONOMY_GRANT_REVOKED = "AUTONOMY_GRANT_REVOKED"
    AGENT_AUTONOMY_PAUSED = "AGENT_AUTONOMY_PAUSED"

    # ---------- 接入（Integration） ----------
    ADAPTER_EVENT_CONSUMED = "ADAPTER_EVENT_CONSUMED"
    INVOICE_RECORDED = "INVOICE_RECORDED"
    INVOICE_FLAGGED = "INVOICE_FLAGGED"
    BANK_TXN_IMPORTED = "BANK_TXN_IMPORTED"
    BANK_TXN_MATCHED = "BANK_TXN_MATCHED"

    def __str__(self) -> str:  # 落链时直接得到字符串值
        return self.value


# 中文说明（事件目录，供 UI/文档生成与 AI 理解）
DESCRIPTIONS: dict[E, str] = {
    E.VOUCHER_CREATED: "凭证创建（草稿）",
    E.VOUCHER_PUSHED: "凭证推送待审",
    E.VOUCHER_APPROVED: "凭证审批通过",
    E.VOUCHER_POSTED: "凭证过账",
    E.VOUCHER_CANCELLED: "过账凭证撤销（窗口内）",
    E.VOUCHER_REJECTED: "凭证审批驳回（退回制单人修改）",
    E.VOUCHER_WITHDRAWN: "凭证制单人撤回（审批前自行收回）",
    E.VOUCHER_SIGNED: "凭证签字位签署（多级签字）",
    E.VOUCHER_ROUTED: "凭证审批请求经外部通道送达（WorkBuddy/飞书/企微）",
    E.OPENING_BALANCE_IMPORTED: "期初余额导入",
    E.OPENING_BALANCE_REVERSED: "期初余额红字冲销（force 重导前置）",
    E.CLOSING_EXECUTED: "期末损益结转执行",
    E.AGENT_DECISION: "AI 决策留痕",
    E.AGENT_MONTHEND_RUN: "关账 Agent 执行",
    E.AGENT_ANOMALY_DETECTED: "异常侦测命中",
    E.BREAKER_TRIPPED: "断路器跳闸（冻结 Agent 自治）",
    E.BREAKER_RELEASED: "断路器人工解除",
    E.AUTONOMOUS_POSTED: "L3 自治过账",
    E.AUTONOMOUS_REVIEWED: "自治凭证抽检裁决",
    E.AUTONOMOUS_REVERSED: "自治凭证红字冲销",
    E.AUTONOMY_GRANT_ISSUED: "自治授权令牌签发（人类 admin 显式授权）",
    E.AUTONOMY_GRANT_REVOKED: "自治授权令牌吊销",
    E.AGENT_AUTONOMY_PAUSED: "自治额度用尽·Agent 自动暂停（通知人类）",
    E.ADAPTER_EVENT_CONSUMED: "适配器消费外部事件",
    E.INVOICE_RECORDED: "发票入账登记",
    E.INVOICE_FLAGGED: "发票进入人工复核队列",
    E.BANK_TXN_IMPORTED: "银行流水导入",
    E.BANK_TXN_MATCHED: "银行流水勾对",
}

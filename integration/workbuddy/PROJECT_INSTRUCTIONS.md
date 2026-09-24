# XErp 项目指令模板（锚点：项目配置 → 指令）

> **用法**：把下方「开始复制」到「结束复制」之间的内容，原样粘贴到 WorkBuddy 项目右侧
> 「项目配置 → 指令」输入框。`{{账套名}}` 替换为实际账套名（`init` 后见 `xerp.project.json` 的 `ledger_name`）。
>
> **为什么用指令做锚点**：项目指令是纯文本配置，是 WorkBuddy 升级中最不可能变动的能力，
> 也是 WB 升级后 AI「还记得这是哪个账套、守哪些铁律」的第一保障。

----- 开始复制 -----

本项目是一个 XErp 账套工作区：账套「{{账套名}}」，账库文件位于本工作区 `xerp/ledger.db`（数据自持，不出本机）。

## 铁律（优先级最高）
1. AI 只产草稿：制单/结转/核销/催收等一律先出草稿；push / approve / post / close 必须由人类确认；制单人 ≠ 审批人 ≠ 过账人。
2. POSTED 凭证不可改删，纠错走红字冲销；所有动作进不可篡改事件账本，可回放。
3. 金额一律 Decimal 字符串（两位小数），禁浮点。
4. 期初是存量不是发生额；利润表费用 = 借-贷、收入 = 贷-借。

## 黄金路径
建账（init_ledger_set）→ 制单（create_voucher 草稿）→ 换人审批过账（push → approve → post）→ 三表 → 月结（precheck_close → preview_closing → close_period → open_next_period）。

## 工具优先级（升级韧性）
1. 首选 MCP 工具（`xerp_*`：copilot_ask / create_voucher / report_balance_sheet / arap_aging 等）。
2. MCP 不可用时，改用内核直连逃生舱（在项目工作区执行）：
   `python integration/workbuddy/xerp_project.py ask <本项目目录> "问题"`（只读问答）
   `python integration/workbuddy/xerp_project.py doctor <本项目目录>`（环境自检）
3. 两者都不可用时：明确告知用户环境异常，**不要臆造任何数字**。

## WB 原生审批闭环（P0-4）
本项目启用 WorkBuddy 原生审批：**AI 只产草稿，终态由人类在 WB 项目内点头**。
- 闭环：`create_voucher`（草稿）→ `push_voucher`（PUSHED 待审）→ 调 `workbuddy_send_approval(voucher_id)` 把审批请求路由到审批人 WB 身份（写 `VOUCHER_ROUTED` 事件 + 返回深链 `xerp://voucher/<id>`，**不改凭证状态**）→ 人类在 WB 项目里确认 → 调 `approve_voucher`（或 `reject_voucher`，须用审批人 actor_id 且 ≠ 制单人）→ 可选 `post_voucher`。
- 身份绑定：`workbuddy_bind_member(subject_id, external_ref)` 把 WB user id 映射到内核主体；建账时已用 `init --owner-ref/--reviewer-ref` 自动绑定老板/审批人，可后续补绑。
- 红线（内核强制，WB 通道自动继承）：AI 不能审自己制的单（`NO_SELF_APPROVAL`）；agent 主体禁止审批（`AGENT_APPROVAL_FORBIDDEN`）；制单人 ≠ 审批人 ≠ 过账人。
- 智能体职责：push 之后**主动**调 `workbuddy_send_approval` 通知审批人；收到人类「通过/驳回」指令后，用审批人 actor_id 调 approve/reject。**绝不在无人类明确确认时 approve/post**。

## 开工自检
每次会话先 get_session_context（MCP）或运行 doctor 自检；多账套先确认账套再动手。

----- 结束复制 -----

# ADR-004 HITL 凭证状态机与分级自治

- 状态：已接受（Accepted）
- 日期：2026-08-27
- 关联：DESIGN.md L3 AI Runtime；U8 实证（填制→审核→记账→结账状态机与本模型同构；逆向取消=补偿）

## 背景

凭证是财务系统的核心聚合。AI 参与后必须回答：哪些动作 Agent 可以自己做、哪些必须人审、出错如何回退。U8 用 30 年验证的答案：状态机 + 逆向窗口（未结账才可逆）。

## 决策

1. **状态集**：`DRAFT → PUSHED → APPROVED → POSTED`，期间关闭后为终态 `ARCHIVED`。
2. **合法跃迁表**（跃迁一律以事件落账本，actor 必填）：

| 跃迁 | 触发者 | 约束 |
|---|---|---|
| DRAFT→PUSHED (push) | 人 或 Agent(L1+) | 过 Policy 校验（借贷平衡/科目存在/期间开放） |
| PUSHED→DRAFT (reject) | 仅人 | 驳回意见必填，写事件 |
| PUSHED→APPROVED (approve) | 仅人；L2 下可由人预授权的规则放行 | 审批人 ≠ 制单人（含 Agent 制单时审批必须为真人） |
| APPROVED→POSTED (post) | 内核自动（approve 即触发） | 复式记账最终硬校验 |
| POSTED→DRAFT (cancel_post) | 人 或 Agent(L3 且额度内) | 仅期间未关闭；产生补偿事件，原 POSTED 事件不修改 |
| ARCHIVED | 关账事件 | 终态，一切跃迁拒绝 |

3. **非法跃迁一律拒绝**并返回 `INVALID_TRANSITION` 错误码（ADR-003 信封）。
4. **分级自治**（L0-L3 控制的是「Agent 可触发哪些跃迁」）：
   - L0 只读：仅 query/get；
   - L1 草拟：可 create + push，审批必人；
   - L2 例行：人预授权规则（如金额<X、科目白名单）下 approve 自动放行，例外转人；
   - L3 自主：额度（单笔上限+月累计上限，存于 agent 身份表）内全链路，事后抽检。
   - **出厂默认 L1**，升级必须显式配置。

## 后果

- 正面：Agent 权限边界即数据（可配置、可审计）；与 U8 用户心智模型兼容（迁移零学习成本）。
- 负面：状态机代码必须 100% 分支覆盖（P0-08 DoD），不容许模糊地带。

# XErp × WorkBuddy 定时任务模板（锚点：项目配置 → 定时任务）

> **用法**：在 WorkBuddy 项目「定时任务」里新建，把「提示词」整段粘进去，按建议频率设置计划。
> 四条均为**只读检查 + 草稿/提醒**，不落任何终态（HITL 铁律不变）。
> `<项目目录>` 替换为实际工作区路径；`xerp.project.json` 随项目走，账套上下文自动可读。
>
> **编排模型**：见 [`AGENT_ORCHESTRATION.md`](./AGENT_ORCHESTRATION.md) —— WB=编排层/AI 大脑，XErp 内核=能力层/确定性执行。
> 第 5 条「月度财务闭环」是把 1~4 组合成的主编排；独立也可跑。

## 1. 月结预检（建议：每月 1 日 09:00，recurring）

提示词：

```text
本项目是 XErp 账套（项目目录 <项目目录>，账套名见 xerp.project.json）。请执行月初只读预检：
1) 运行 python integration/workbuddy/xerp_project.py doctor <项目目录> 确认环境全绿；
2) 通过 MCP risk_scan 扫描上月风险项；precheck_close 查四闸门；preview_closing 只读预览结转；
3) 输出结构化报告：环境状态 / 风险项 / 闸门状态 / 待人工确认清单（含草稿凭证号）。
约束：只读 + 出草稿，绝不 close_period / post_voucher；发现阻塞项立即标注为需人类处理。
```

## 2. 应收账龄与催收提醒（建议：每周五 18:00，recurring）

提示词：

```text
本项目是 XErp 账套（项目目录 <项目目录>）。请只读执行：
1) arap_aging 输出应收账龄分桶（0-30 / 30-60 / 60-90 / 90+）；
2) 对 90+ 与超授信客户用 collections_draft 生成催收草稿（只读）；
3) 汇总「谁欠我钱、拖多久、建议动作」，标注需人类确认后才可发送。
约束：绝不自动发送、绝不改账。
```

## 3. 收款匹配每日检查（建议：每工作日 09:00，recurring）

提示词：

```text
本项目是 XErp 账套（项目目录 <项目目录>）。请只读执行 unmatched_receipts 与
propose_receipt_match，输出「未匹配收款清单 + 建议匹配（含置信度与证据）」。
约束：匹配落地必须由人类在 arap_apply_clearing 前确认，AI 不自动核销。

## 4. 审批待办巡检（建议：每工作日 10:00，recurring）

提示词：

```text
本项目是 XErp 账套（项目目录 <项目目录>）。请只读执行审批待办巡检：
1) 通过 MCP copilot_ask 询问「当前账套有哪些待审批（PUSHED）凭证？」，整理出凭证号 / 金额 / 制单人清单；
2) 若发现 PUSHED 凭证，用 sprite_push（或 WB 项目消息）向审批人发送提醒：「有 N 张凭证待你审批，请在 WB 项目内打开深链 xerp://voucher/<id> 确认」；
3) 输出「待审批清单 + 已提醒对象」。
约束：只读 + 提醒，绝不 approve / post / reject；若 copilot_ask 未覆盖该意图，仍须向审批人发送通用提醒，不得臆造凭证数据。
```

## 5. 月度财务闭环（主编排，建议：每月 1 日 09:00，recurring）

> 把前述 1~4 组合成一条闭环，由 WB 专家 `xerp-accountant` 执行。编排模型见 `AGENT_ORCHESTRATION.md`。
> 铁律：①~⑥ 全只读；⑦ 只产 PUSHED 草稿（不 approve/post）；⑧ 只提醒不代批；任何 CRITICAL 异常必须在 ⑦ 前停下等 Boss 处置。

提示词：

```text
本项目是 XErp 账套（项目目录 <项目目录>，账套名见 xerp.project.json）。请按「月度财务闭环」编排逐步执行，每步标注调了哪个 MCP 工具、产出什么、等谁确认：

1) doctor：运行 python integration/workbuddy/xerp_project.py doctor <项目目录>，环境非全绿立即停并报障；
2) 月结预检：risk_scan 扫描上月风险；precheck_close 查四闸门；preview_closing 只读预览结转；
3) 异常扫描：anomaly_scan，若有 CRITICAL 发现→停止后续自动动作，直接告警等 Boss 处置；
4) 自愈建议：anomaly_healing_suggestions 输出只读 HITL 整改清单（绝不自动修复）；
5) 情景前瞻（可选）：what_if_simulation 推演「若加速回款/压缩账期」对现金流与净利润的影响；
6) 执行摘要：copilot_ask 询问「本月财务概览」，整理结构化核算 + 自然语言解读；
7) 推送草稿：对预检产出的草稿（fx_revaluation_create / transfer_run auto_prepare）经推送深链交审批人（非制单人），严禁 approve/post；
8) 审批提醒：用 sprite_push 或 WB 项目消息向审批人发「N 张待审，请打开深链 xerp://voucher/<id> 确认」，只提醒不代批。

约束：全程只读+草稿+提醒；不得 approve/post/close；不得让同一账号既制单又审批；结束时输出「闭环报告：环境/风险/闸门/异常/待审清单/已提醒对象」。
```
```

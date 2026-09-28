# XErp Agent 编排（寄生 WB 架构 · 向量 D）

> 状态：**设计落地** · 基线：ledgeros Phase1 六向量 · 关联：`README.md`（五锚点）、`AUTOMATIONS.md`（定时任务模板）、`PROJECT_INSTRUCTIONS.md`（指令）

## 0. 一句话

**WorkBuddy = 编排层 / 唯一 AI 大脑；XErp 内核 = 能力层 / 确定性执行。**
编排层只调度，能力层只算；终态永远由 Boss（人类）确认。

```
        ┌──────────────────────── WorkBuddy（编排层 · AI 大脑）────────────────────────┐
        │  定时任务 ──┐                                                            │
        │  专家      ├──→ 调度 ──→ 连接器 MCP（XErp 工具面）──→ 内核智能体（能力层）    │
        │  技能      ─┘                                                            │
        └──────────────────────────────────────────────────────────────────────────┘
                                          │ 只读 + 草稿
                                          ▼
                               Boss 确认（审批 / 过账 / 结账）
```

## 1. 能力映射：XErp 内核智能体 → WB 原生四件套

| XErp 内核智能体 | 职责（确定性·只读或草稿） | 暴露的 MCP 工具 | WB 编排原语 |
|---|---|---|---|
| **Copilot**（`kernel/copilot.py`） | 中文自然语言 → 路由到只读内核，出 `answer_zh/evidence/followups/severity` | `copilot_ask` | 专家 `xerp-accountant`（包装 Copilot 的「会计专家」）；技能 `xerp`（SOP） |
| **算子 Operator**（`kernel/operator.py`） | AI Runtime 可视化镜像（六态机 + 跨进程信号桥） | `ai_runtime_state` | 信号桥（MCP→Web 单向）；不入编排主流程，纯增值信息 |
| **精灵推送 Sprite**（`kernel/sprite_push.py`） | 只读主动推送（月结/异常/健康/信用/催收/收款待匹配） | `sprite_push` | 定时任务巡检的输出通道（提醒 Boss） |
| **异常扫描 Anomaly**（`kernel/anomaly.py`） | 规则扫描只读 + 断路器冻结自治 | `anomaly_scan` / `anomaly_release` | 定时任务 + 月结闭环的「健康检查」节点 |
| **自治授权 Autonomy**（`kernel/autonomy.py`） | 授权令牌 + 断路器状态（人类授权后才可自治过账） | `autonomy_*` | 仅人类授权；编排层不得自动授予 |
| **自愈建议 Healing**（`kernel/healing.py`） | 把异常 Findings 映射为 HITL 整改建议（绝不自动修复） | `anomaly_healing_suggestions` | 月结闭环的「建议」节点（只读） |
| **情景推演 Simulation**（`kernel/simulation.py`） | what-if 杠杆推演（只读，复用 forecast） | `what_if_simulation` | 月结闭环的「前瞻」节点（只读） |

> **关键**：内核智能体**不自带调度循环**——它们被 WB 定时任务按需调用。WB 升级 / 下线时，内核仍可由 L0（`xerp_project.py` 直连）驱动，能力不碎。

## 2. 月度财务闭环编排（主编排流）

把 `AUTOMATIONS.md` 里分散的 4 条只读检查**编排成一条闭环**。建议作为一个**月度 recurring 定时任务**（每月 1 日 09:00）落地，由 WB 专家 `xerp-accountant` 执行：

```
① doctor        → 环境自检全绿（L0 保底，失败即停）
② 月结预检       → risk_scan + precheck_close（四闸门）+ preview_closing（只读预览结转）
③ 异常扫描       → anomaly_scan（有发现→算子 ALERT + 阻断后续自动动作）
④ 自愈建议       → anomaly_healing_suggestions（只读 HITL 清单）
⑤ 情景前瞻       → what_if_simulation（可选：若加速回款/压缩账期，现金流/净利润影响）
⑥ 执行摘要       → copilot_ask「本月财务概览」→ 结构化核算 + 自然语言解读
⑦ 推送草稿       → 对预检产出的草稿（fx_revaluation_create / transfer_run auto_prepare）
                   经推送深链交审批人（非制单人）
⑧ 审批提醒       → sprite_push / WB 项目消息 向审批人发「N 张待审，深链 xerp://voucher/<id>」
```

**闭环铁律**：①~⑥ 全只读；⑦ 只产 PUSHED 草稿（不 approve/post）；⑧ 只提醒不代批。任何 `CRITICAL` 异常都必须在 ⑦ 前停下，等 Boss 处置。

## 3. 红线（编排层不得越界）

- ❌ 编排层（WB 自动化 prompt）不得写「自动 approve / post / close」——终态只 Boss 点；
- ❌ 不得让同一 WB 账号既制单又审批（联邦 A 向量已平台层隔离；内核 R4 二次校验）；
- ❌ 不得绕过内核智能体直连内部符号（R1）；
- ✅ 编排层每步都要能「说清楚调了哪个 MCP 工具、产出了什么草稿、等谁确认」。

## 4. 与既有资产的关系

- `AUTOMATIONS.md` 第 1~4 条 = 本闭环的**叶子节点**（独立也可跑）；
- `AUTOMATIONS.md` 第 5 条「月度财务闭环」= 本闭环的**主编排**（组合 1~4 + ⑥执行摘要 + ⑦⑧推送提醒）；
- `PROJECT_INSTRUCTIONS.md` 已固化 HITL 铁律，编排层 prompt 引用即可，不重复定义。

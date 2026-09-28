# 边界纪律（寄生 WB 架构 · 向量 F）

> 状态：**已接受** · 基线：ledgeros Phase1 六向量收口 · 关联：ADR-001~ADR-007、`tests/test_integration_acl.py`、`tests/test_boundary_discipline.py`

## 0. 为什么要这份文档

XErp 的价值收敛为「**内核深度 + MCP·技能表面积**」，运行时（身份 / 入口 / 自动化 / 存储 / 分发）全部寄生 WorkBuddy。内核必须保持**零外部依赖、可独立跑（逃生舱）**，否则一旦 WB 升级或下线，XErp 就碎。

本文件把「寄生架构」的**红线**落成机器可验证断言——任何一笔改动若触碰红线，CI / `pytest` 直接红，不靠人肉 code review。

## 1. 五条红线（破坏任意一条即回退）

| # | 红线 | 守则 | 机检断言（见 `tests/test_boundary_discipline.py` / `test_integration_acl.py`） |
|---|---|---|---|
| **R1** | 内核核心层不得反向依赖适配器 | 依赖方向只能是 **外围 → 核心**（adapters / webapp / ocr / biz_wizard 可 import 核心；核心不得 import `kernel.adapters`） | `test_core_ledger_does_not_import_adapters`（T1） |
| **R2** | 单一真源（ADR-002） | 报表 / 预览 / 审计不得复制配平或投影；改 `_accumulate_balances` 须同时落 `VoucherLine`，绝不另算一份 | `test_adapters_do_not_call_projection_internals`（T2）、`test_adapters_do_not_write_balance_projection`（T3）、审计复用 `verify_chain` 而非重算哈希（`test_audit_reuses_chain`） |
| **R3** | AI 只产草稿，终态须人类点头 | 审计 / 模拟 / 自愈 / Copilot 等只读智能体**绝不**写 `Voucher` / `Balance`、绝不走 `transition` / `post_voucher` / `approve_voucher` 等终态入口 | `test_readonly_agents_never_mutate_state` |
| **R4** | 制单 ≠ 审批 ≠ 过账 | 同一凭证，制单人不能审批自己凭证；Agent 不能审批或驳回；内核 `_apply_transition` 二次校验 | `test_webapp_enforces_maker_not_approver`（守卫文本存在性） |
| **R5** | XErp 纯 WB 原生，不建自有运行时 | 不独立做 SaaS、不做 Docker 容器化部署、不建自有计费 / 独立 auth / 独立 SSO；Web + Docker 仅开发者本地逃生舱 | 架构断言（设计态，不机检；见 §3） |

## 2. R3 细化：哪些模块是「只读智能体」，禁止写账

下列模块是**确定性只读**智能体，本文件钉死它们不得出现任何终态写入入口：

- `kernel/reporting/audit_trail.py` — 审计追踪（只读报告）
- `kernel/simulation.py` — what-if 情景推演（只读）
- `kernel/healing.py` — 异常自愈建议（只读 HITL 建议，绝不自动修复）
- `kernel/copilot.py` — 确定性自然语言路由（只读聚合）

**禁止出现的符号**（静态扫描）：`Voucher(`、`Balance(`、`transition(`、`post_voucher`、`create_voucher`、`approve_voucher`、`reject_voucher`、`cancel_post_voucher`、`withdraw_voucher`、`sign_voucher`、`push_voucher`。

> 例外：读过滤（`AssetCard.status == "active"` 这类 `==` 比较）**不**算写入，扫描用「单等号赋值」语义排除。

## 3. R5 架构断言（设计态，需人肉确认）

- WorkBuddy = 唯一 AI 大脑 + 运行时 / 分发 / 协作 / 存储容器；
- XErp 交付物 = WB 原生四件套（**技能 + 专家 + 连接器 MCP + 定时任务**）+ 项目指令 + 资料库；
- 商业化走 WB 应用商店 / 私有交付包 / 平台 license，计费由 WB 平台承担；
- 数据不出用户本机（无云可出），`kernel/` 内核零外部依赖、可 `python -m kernel.webapp` 独立起。

## 4. 拒绝的反模式

- ❌ 在 `kernel/` 核心层 `import kernel.adapters`（违反 R1）
- ❌ 报表模块自己重算余额 / 重算事件哈希（违反 R2）
- ❌ 让 Copilot / 异常扫描「顺手」把凭证过账、把建议自动应用（违反 R3）
- ❌ 让制单人自己审批、让 Agent 点头落账（违反 R4）
- ❌ 给 XErp 加一套独立登录 / 计费 / 多租户（违反 R5）

## 5. 验证

```bash
cd ledgeros
python -m pytest tests/test_integration_acl.py tests/test_boundary_discipline.py -q
```

- `test_integration_acl.py`：R1/R2 结构契约 + 适配器 ingest 不落 Balance 投影的运行时证明（T1–T5）
- `test_boundary_discipline.py`：R3 只读智能体无写入 + R2 审计复用 `verify_chain` + R4 内核守卫文本存在

# XErp

AI Native 智能体 ERP 内核 —— 确定性记账内核 + 概率性 AI 外壳，总账（GL）优先。

> 设计：[docs/DESIGN.md](docs/DESIGN.md) ｜ 开发计划：[docs/DEVPLAN.md](docs/DEVPLAN.md)

## 铁律

1. **确定性内核 + 概率性外壳**：借贷平衡、过账、结转由引擎硬校验；LLM 只理解/编排/解释，永不算钱。
2. **审计即架构**：append-only 事件账本 + hash 链，从第一行代码开始。
3. **单点纵深**：总账没到 GA 之前，不做其他模块。

## 目录

```
kernel/       Python 记账内核（FastAPI + SQLAlchemy + PostgreSQL）
mcp-server/   MCP Tools/Skills/Resources 暴露层
web/          A2UI 前端（P0 为 HTML 兜底）
skills/       SKILL.md 分发单元（WorkBuddy / Agent 客户端）
deploy/       docker-compose 一键交付
docs/         ADR 决策记录 + 设计文档 + 计划
tests/        内核规则测试（不变量 100% 分支覆盖）
```

## AI 财务驾驶舱（XErp Copilot）

XErp 不只出「上月报表」，而是把财务变成**实时决策界面**：每天告诉你「哪个决策会改变结果」。

驾驶舱由三大确定性件构成（AI 只产草稿，落账终态必须人类点头）：

1. **零日结账对账**：每天自动跑试算平衡 + 银行/往来勾对，错误在发生的当天就被发现，而不是月结时才爆雷。
2. **JEV 决策引擎（Journal Entry Verification）**：本地 100% 确定性内核，对高频小决策做判断 / 打分 / 选择（预算差异分级、凭证风险严重度、重复凭证标记、应付未清项健康度、费用合规判定、审批路由等）。**只读草稿、绝不触发过账 / 支付 / 改账**；可选的云端校准默认关闭，数据不出本机。
3. **持续预测（6 杠杆）**：延长付款 / 加速回款 / 资本开支激增 / 成本上升 / 增长停滞 / 毛利压缩，实时给出对「期末现金、净利润、应收、应付、经营现金流、总资产」的 delta，让你在签字前就看见后果。

> 在线 Demo（口令 `demo123`）：<https://xerp-demo.app.workbuddy.host>
> 开源地址：<https://github.com/zccmman/XErp>

## 快速开始（Docker）

见 [deploy/quickstart.md](deploy/quickstart.md)：`docker compose up -d --build` → WorkBuddy 添加 `http://localhost:8000/mcp`。

## 开发

```bash
pip install ruff pytest
ruff check .   # Lint
pytest         # 测试
```

License: Apache-2.0（草案，见 docs/DESIGN.md 第 8 节）

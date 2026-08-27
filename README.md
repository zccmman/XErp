# LedgerOS

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

## 快速开始（Docker）

见 [deploy/quickstart.md](deploy/quickstart.md)：`docker compose up -d --build` → WorkBuddy 添加 `http://localhost:8000/mcp`。

## 开发

```bash
pip install ruff pytest
ruff check .   # Lint
pytest         # 测试
```

License: Apache-2.0（草案，见 docs/DESIGN.md 第 8 节）

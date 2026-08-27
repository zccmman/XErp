# ADR 索引

| 编号 | 标题 | 状态 |
|---|---|---|
| [ADR-001](ADR-001-kernel-stack.md) | 内核技术选型：Python 3.12 + FastAPI + SQLAlchemy + PostgreSQL | 已接受 |
| [ADR-002](ADR-002-event-ledger.md) | 事件账本：append-only 表 + sha256 hash 链 | 已接受 |
| [ADR-003](ADR-003-mcp-contract.md) | MCP 工具命名规范与数据契约（金额 Decimal-string、错误信封、幂等） | 已接受 |
| [ADR-004](ADR-004-hitl-state-machine.md) | HITL 凭证状态机与分级自治（L0-L3，出厂默认 L1） | 已接受 |
| [ADR-005](ADR-005-permission-model.md) | 权限模型：RBAC + 数据范围 + Agent 一等身份 | 已接受 |

新增 ADR 规则：复制任一文件作模板；状态流转 Proposed → Accepted / Superseded；重大决策先写 ADR 再写码。

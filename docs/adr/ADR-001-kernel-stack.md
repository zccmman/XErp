# ADR-001 内核技术选型：Python 3.12 + FastAPI + SQLAlchemy + PostgreSQL

- 状态：已接受（Accepted）
- 日期：2026-08-27
- 关联：DESIGN.md 第 5 节

## 背景

XErp 内核需要承载：确定性记账引擎（强一致性约束）、事件账本（append-only）、MCP 能力层（Python SDK 最成熟）、单人+AI 结对的开发节奏（P0 目标 4 周出 PoC）。

## 候选方案

| 方案 | 优势 | 劣势 |
|---|---|---|
| **Python + FastAPI + SQLAlchemy + PG（选定）** | AI/MCP 生态最全（fastmcp 官方 SDK）；开发速度最快；Decimal 原生支持金额 | 运行时性能低于编译语言；并发模型弱 |
| Go + pgx | 高并发、单二进制交付 | AI 生态薄弱（MCP SDK 次选）；开发速度慢 2-3 倍，威胁 4 周 Gate |
| Node/TS | 与 Web 前端同语言 | ORM/账务成熟度弱；Decimal 需绕行 |

## 决策

1. **Python ≥3.12**（享受 3.12 性能提升与 tomllib），金额一律 `decimal.Decimal`，禁止 float。
2. **FastAPI** 提供 HTTP/管理面；**SQLAlchemy 2.0 + Alembic** 管理 schema 迁移。
3. **PostgreSQL ≥16** 单库承载：Ontology 表 + `events` 事件账本 + 余额物化投影（JSONB 承载辅助核算维度）。
4. 性能策略：P0-P1 单机单库不做优化；出现瓶颈先加投影缓存，而非换语言。

## 补充条款（2026-08-27，P0-03）

**测试数据库回退**：模型维度字段使用 `sa.JSON().with_variant(postgresql.JSONB, "postgresql")` 双方言；
单测默认 SQLite 内存库（零基础设施），金额在 SQLite 下为近似值、断言用数值等价。
PG 专属保障（JSONB 精确性、append-only 触发器、并发成链）由 `@postgres` 标记测试承载，
在 `TEST_DATABASE_URL` 指向 PG16 时运行（CI/本地 Docker）。P0-04 触发器测试必须在 PG 上通过。

## 后果

- 正面：4 周 PoC 可达；MCP 工具层与内核同语言零序列化损耗。
- 负面：放弃单二进制分发（deploy 走 docker compose 补偿）；未来若出现高并发月结场景，仅投影层需要重写（事件账本不动——这正是 ADR-002 的价值）。

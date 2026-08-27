# LedgerOS 快速开始（Docker Compose · 目标：30 分钟内完成首张凭证）

## 前置

- Docker Desktop（或任意 Docker Engine 20+），已启动
- WorkBuddy / 任意支持 MCP 的 Agent 客户端

## 三步启动

```bash
cd ledgeros/deploy
docker compose up -d --build        # 首次构建约 3-5 分钟（拉取镜像）
docker compose logs -f init         # 看到 "完成: 1 个账套…" 即初始化成功（Ctrl+C 退出日志）
```

服务拓扑：
- `db`：PostgreSQL 16（数据持久化在命名卷 `pgdata`；append-only 触发器在此生效）
- `init`：一次性执行 alembic 迁移 0001-0003 + 演示账套 + 144 科目模板 + 制单/审批双身份
- `mcp`：HTTP MCP 服务，端点 `http://localhost:8000/mcp`

## 接入 WorkBuddy

连接器管理 → 添加自定义连接器（URL 方式）：

```
http://localhost:8000/mcp
```

信任并重开会话，然后对话：

> 帮我建个新账套，公司叫「演示科技」，所有者写我名字

向导会引导：`init_ledger_set`（144 科目+当月期间）→ 报期初余额 → `import_opening_balances`
（试算平衡自动校验）→ 直接开始日常记账（报销/付款/收入…）。

## 常用运维

```bash
docker compose down            # 停止（数据保留在 pgdata 卷）
docker compose down -v         # 停止并清空数据
docker compose logs -f mcp     # 看 MCP 服务日志
docker compose exec db psql -U ledgeros -d ledgeros -c "select count(*) from events;"
```

## 排障

| 现象 | 处理 |
|---|---|
| `init` 退出非 0 | `docker compose logs init` 看迁移报错；常见为 db 未就绪（healthcheck 已缓解） |
| WorkBuddy 连不上 8000 | 确认 `docker compose ps` 中 mcp 为 Up；端口被占用时改 compose 端口映射 |
| 想换 PG 密码 | 同时改 db.environment 与两个服务的 LEDGEROS_DB |

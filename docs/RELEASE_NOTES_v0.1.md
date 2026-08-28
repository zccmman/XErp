# XErp v0.1.0 发布说明

**发布日期**：2026-08-28
**代号**：XErp（AI Native 智能体 ERP 内核）
**定位**：总账（GL）优先的确定性记账内核 + 概率性 AI 外壳；不是"又一个 ERP 界面"，而是**给 AI Agent 用的账务内核**。

---

## 一句话

> 对话即记账，规则引擎硬拒一切非法操作，全程 append-only 审计链可验证。

---

## v0.1 交付内容

### 内核（确定性层）
- **事件账本**：append-only + `sha256(prev_hash + canonical_json(...))` 账套内成链；PG/SQLite 双方言触发器禁止 UPDATE/DELETE
- **记账引擎**：七类硬校验（行数/借贷平衡/单侧记/金额为正/科目存在/期间未结账/期间匹配），`posting.py` 分支覆盖 100%
- **HITL 状态机**：`DRAFT → PUSHED → APPROVED → POSTED`，含补偿事务 `POSTED → DRAFT`（仅未结账期间，追加事件不改历史）
- **门禁三件套**：`NO_SELF_APPROVAL`（制单≠审批）/ `AGENT_APPROVAL_FORBIDDEN`（Agent 永不审批）/ `AUTONOMY_DENIED`（Agent 撤销须 L3）
- **科目模板**：小企业会计准则 144 科目（P0-04/06），幂等导入
- **审计回放 CLI**：`python -m kernel.audit export|verify`，退出码 0=链完整 / 2=链异常

### 能力层（MCP，13 个工具）
`get_workspace` · `init_ledger_set` · `ensure_period` · `import_opening_balances` · `list_accounts` ·
`create_voucher` · `push_voucher` · `approve_voucher` · `post_voucher` · `cancel_post_voucher` ·
`get_voucher` · `query_balances` · `feishu_send_approval`

契约：金额一律 decimal-string；错误信封 `{ok, error:{code, message_zh, details}}`；幂等键防重；写操作强制 actor。

### 集成与交付
- **Drill 建账向导**：两步到可记账（建账套+期初导入，试算平衡自动校验）
- **飞书审批**：WebSocket 长连接 + 回复式审批（`同意 凭证号` / `驳回 凭证号 意见`），驳回意见入事件流
- **Web 最小界面**：FastAPI 服务端渲染 4 页面（工作区/账套仪表盘/凭证详情/建账向导），8001 端口
- **Docker Compose 一键交付**：PG16 + init（迁移+种子）+ MCP(8000/mcp) + Web(8001)

### 质量门禁
- **65 个自动化测试全绿**，ruff 0 error
- 关键补充：MCP stdio **管道级握手冒烟**（按客户端方式直跑 server.py + initialize），制度化防回归

---

## 真机验证记录（不是 demo，是跑通的事实）

| 场景 | 结果 |
|---|---|
| WorkBuddy 对话记账 | 「报销招待费 800 元现金」→ 记-0001 全链路 POSTED，Agent 自选明细科目 660204 |
| 飞书真机审批 | 记-0002 推卡 → 飞书回复「同意 记-0002」→ APPROVED（审批人=飞书 open_id）→ POSTED |
| 真实 dogfood | OPC 一人公司建套 + 期初 170,000 + 10 笔业务；独立重算对账**零差异** |
| 审计链 | dogfood 账套 41 条事件，`verify` 退出码 0，`chain_ok: true` |

---

## 已知局限（诚实清单）

1. **期末损益结转未实现**（P1-02），利润为手工测算；固定资产折旧未计提
2. 工资简化直发，未走「计提→发放」两笔；增值税未做月末转出
3. Web 为 HTML 兜底视图，A2UI 正式渲染器在 P1-05
4. 单机 SQLite 演示库为主，PG 路径已在 compose 中就绪（首次实跑待 Docker 环境）
5. 多币种、辅助核算维度报表、发票 OCR 均未开始（P1-P2）

---

## 快速开始

```bash
# 方式一：Docker
cd deploy && docker compose up -d --build     # MCP:8000/mcp  Web:8001

# 方式二：本地
python -m venv .venv && pip install sqlalchemy alembic fastmcp fastapi httpx uvicorn
python scripts/init_demo.py                   # 初始化演示库
python -m kernel.webapp                       # Web: http://127.0.0.1:8001
```

Agent 客户端接入：WorkBuddy/Claude 添加 MCP 连接器
（`python mcp-server/xerp_mcp/server.py`，或 HTTP `http://localhost:8000/mcp`）。

---

## 致谢与方法论

本项目从逆向分析用友 U8+ 总账（只读元数据、clean-room）出发，把三十年 ERP 的治理机制翻译为 Agent 原语。
全程采用 **ADR 先行 + TDD + Gate 制 + 计划勾销制**，完整方法论见 `docs/LECTURE_SERIES.md`。

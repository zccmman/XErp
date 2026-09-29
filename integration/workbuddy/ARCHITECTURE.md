# XErp 架构白皮书（寄生 WorkBuddy · AI Native ERP）

> 本文档定义 XErp 的**寄生 WorkBuddy 架构模式**与**双引擎运行模型**，是部署、二次开发、审计合规的权威依据。
> 配套：《README.md》（项目即账套模式包）、《DEPLOYMENT.md》（极简部署与故障排查）、《AGENT_ORCHESTRATION.md》（月度财务闭环编排）、《AUTOMATIONS.md》（定时任务模板）。

---

## 一、定位铁律（不可妥协）

XErp = **纯粹 WorkBuddy 原生**的 AI ERP。

- **不独立做 SaaS**：不自建 Web 服务、不做 Docker 容器化部署、不建自有计费 / 订阅 / SSO / 独立 auth。
- **WorkBuddy 即运行时 + 分发 + 协作 + AI 大脑 + 存储容器**：XErp 的交付物是 WB 原生四件套（技能 Skill + 专家 Expert + 连接器 MCP/Connector + 定时任务 Automation）+ 项目指令 + 资料库。
- **商业化 = WB 平台级 license**：计费由 WB 平台承担，XErp 不碰；数据不出用户本机 / 工作区（无云可出，反 NetSuite 锁定卖点）。
- **两条红线（守不住就不是 XErp）**：
  - ① **内核零 WorkBuddy 依赖**：`kernel/**` 核心层不得 `import workbuddy` / `import adapters`（白名单仅 `adapters/ocr`/`webapp`/`biz_wizard` 等可插拔入口）；内核可独立 `python -m kernel.webapp` 逃生。
  - ② **数据不出本机**：账库 = 本地 SQLite 文件，复制目录即复制账套；无任何对外数据通道。

---

## 二、三层寄生架构

```
┌──────────────────────────────────────────────────────────────┐
│  宿主层（WorkBuddy 原生能力，可插拔）                            │
│  Skill ── 专家 Expert ── 连接器 MCP/Connector ── 定时任务 Automation │
│  （AI 大脑 / 自然语义入口 / 人类协作面 / 定时只读+草稿）            │
└───────────────────────────┬──────────────────────────────────┘
                            │  薄 MCP 接缝（19 / 70 / 100 三档，读写分离 · HITL 闸门）
                            │  minimal=19 标准档=70 pro=100（test_tool_profiles 钉死）
┌───────────────────────────┴──────────────────────────────────┐
│  内核层 JEV（Just-Events Vault，事件溯源确定性内核）              │
│  ├─ 会计引擎：借贷配平 · 期间 · 结账 · 三表 · 合并               │
│  ├─ 审计链：不可篡改 append-only 事件 + verify_chain            │
│  ├─ 双引擎：JEV（确定性，算得对） + LLM（语义层，说得清）        │
│  └─ 内置迁移器 kernel/migrate.py（零依赖、抗漂移、打开即升级）    │
└──────────────────────────────────────────────────────────────┘
```

- **宿主层**只经**薄 MCP 接缝**接触内核，且接缝是**读写分离 + HITL 闸门**：只读工具（查询 / 报表 / Copilot 草稿）可随时调用；写入工具（制单 / 审批 / 过账 / 结账）终态必须人类点头。
- **内核层 JEV** 是单一真源（ADR-002）：报表 / 预览 / Web 全部复用同一取数，绝不复制配平，杜绝口径漂移。

---

## 三、双引擎职责（JEV + LLM）

XErp 把"算得对"与"说得清"解耦，两条引擎各司其职：

| 维度 | JEV（确定性内核） | LLM（语义层，按需调用） |
|---|---|---|
| 角色 | 事件溯源 + 借贷配平 + 审计链 + 三表 | 自然语言理解 + 草案生成 + 解释 |
| 保证 | 数学正确、不可篡改、可审计 | 语义贴合、对话自然 |
| 何时跑 | 每一次写账 / 结账 / 查询 | 仅 Copilot 意图路由后、需要自然语言输出时 |
| 成本 | 零 LLM 成本（纯函数 + 确定性） | 仅在确需语义时计费 |
| 落账 | 唯一能改账的实体 | **绝不**直接写账，只产出草稿 |
| 离线 | 全离线可用 | 断网不影响内核与既有草稿 |

- **Copilot 是确定性意图路由**（`kernel/copilot.py`）：`ask()` 离线可审计、零 LLM 成本，仅在确需自然语言润色时调用 LLM；严重项经 `operator.signal(ALERT)` 联动 Web 跨进程信号桥，无需 websocket。
- **AI Runtime**：LLM 是"语义外设"，不是"控制核心"。JEV 永远掌握终态权威。

---

## 四、HITL 闭环（AI 只产草稿，落账必人类点头）

```
LLM/Copilot 出草稿 ──► 人类核对（制单≠审批）──► 审批人换人复核 ──► 过账（终态）
      ▲                                                      │
      └──────── 审计链 append-only 收口 ◄─────────────────────┘
                 verify_chain 每次取数都验真，绝不重算配平
```

- **三权分立**：制单人 ≠ 审批人 ≠ 过账人（ADR-001 角色派生 + Casbin 多账套 ACL）。
- **AI 只产草稿**：所有写入动作进入不可篡改审计链；审批 / 过账终态硬编码换人校验（`test_approval`、`test_integration_acl` 钉死）。
- **撤回而非删除**：红字冲销 / 撤回凭证，历史永远可追。

---

## 五、分层韧性（WB 升级 / 断连不影响核心能力）

| 层 | 载体 | WB 升级 / 断连影响 |
|---|---|---|
| **L0 内核直连** | `xerp_project.py` + `kernel/**`（纯 python+sqlalchemy） | **零影响**——WB 拿不走的能力 |
| L1 项目指令 | `PROJECT_INSTRUCTIONS.md`（纯文本） | 几乎为零 |
| L2 项目技能 | `xerp` skill | 低（L0 兜底） |
| L3 项目专家 | `experts/xerp-accountant/` | 低（L0 兜底） |
| L4 MCP 连接器 | `~/.workbuddy/mcp.json` | 唯一真正集成点；挂了 L0-L3 仍可用 |

`doctor` 永远验证 L0（含"零 workbuddy import"红线自检），这是整套模式的保底。

---

## 六、并发与容灾（团队模式）

- **SQLite WAL + Busy-Retry**（P0-2，`kernel/webapp.py` `build_app`）：`journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL`，根治 `database is locked`，支撑 NAS / SMB 共享账套多人协同审批 / 查询并发；网络文件系统不支持 WAL 时静默降级为默认模式。
- **内置迁移器**（P1-1，`kernel/migrate.py` `ensure_schema_current`）：打开任意账套 DB 自动按 ORM 元数据补齐缺失表 / 列，抗漂移、幂等、`engine.dispose()` 释放句柄；根治"老库进账套 500 / no such column"事故类。
- **本地每日备份**：生产库每日本地备份（如 ITAMS 同款 `backup_itams.py` 思路）。

---

## 七、演进路线

| 阶段 | 状态 | 内容 |
|---|---|---|
| P0 整饬 | ✅ 完成 | Apple 设计回内核 · WAL 并发 · 移动端响应式 · 内置迁移器 |
| P1 内核 & UI 统一 | ✅ 完成 | 内置迁移器 · MCP 分层统一文档 |
| P2 离线交付包 | ✅ 完成 | 两份本文档 + 私有交付包 + **一键开通个人账套（产品化，见 `PROVISIONING.md`）** |
| P3 市场生态 | 🔜 规划 | WB 应用商店 / 平台级 license / 多租户集团合并 |

### P2 收口要点：一键开通个人账套产品化

- **单一真源**：`kernel/provisioning.py` 的 `provision_personal_ledger` 取代此前分散在
  `xerp-web-demo` 部署分支的 `provisioning.py / IdentityBinding`，成为 CLI（`xerp_project.py init`）
  与 Web（`/init` 向导 + `/api/identity/*` + `/login/wb`）唯一建账原语，消除内核↔部署分支分叉。
- **两种语义**：① 外部身份首次接入幂等开通（external_ref）；② 已登录用户主动新建账套
  （owner_subject_id，一个主体可多账套，不复用既有）。
- **起步模板**：`small_business / individual / sole_proprietor_ltd / nonprofit` 四档开账档案，
  统一复用小企业准则 144 科目，差异在命名/准则/引导清单。
- **HITL 结构性保证**：建账即创建独立「审批人」身份并授予 accountant+reviewer，owner 仅 admin，
  制单≠审批靠结构而非约定。`kernel/authz.list_role_members` 改为按角色动作集合反查 `p` 策略
  （XErp 不依赖 Casbin `g` 角色继承），修复了 reviewer/admin 反查恒空的老 bug。
- **零宿主依赖**：建账内核零 WorkBuddy 依赖，`tests/test_provisioning.py` 钉死红线。

---

## 八、合规与审计要点

- **GB/T 24589.1-2024**：`export_gbt24589` 导出，审计可直接对接。
- **口径单一真源**：报表 / 预览复用 `amounts_by_code` / `balance_sheet` / `ending_balance` / `partner_balances`，绝复制配平。
- **期初是存量非发生额**：红字冲销同理；利润表净额口径（费用=借-贷、收入=贷-借）。
- **审计索引**：云端镜像降级 + `audit_search` 只读检索（E 阶段）。

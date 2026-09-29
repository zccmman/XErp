# XErp 部署手册（极简 · 极易部署 · 数据自持）

> 基于"寄生 WorkBuddy"模式，XErp 的部署目标：**三步复制验证、复制目录即复制账套、打开即升级、零配置并发**。
> 本手册覆盖本地逃生舱与 WB 项目两种形态；配套《ARCHITECTURE.md》（架构白皮书）、《README.md》（模式包说明）。

---

## 一、两种运行形态

| 形态 | 命令 / 入口 | 用途 |
|---|---|---|
| **L0 内核逃生舱**（零 WorkBuddy 依赖） | `python -m kernel.webapp`（默认 `:8001`，读 `XERP_DB`） | 断网 / WB 不可用时的保底 Web；也是 CI 与离线审计入口 |
| **WB 项目即账套**（推荐交付形态） | `xerp_project.py init/ask/doctor <项目目录>` | 复制目录即复制账套；经薄 MCP 接缝接宿主 AI |

> 两种形态共享同一内核 `kernel/**` 与同一账库 SQLite 文件，**数据自持、绝不外发**。

---

## 二、极简部署三步（新电脑 / 新客户）

```bash
# 0) 前置：python 3.11+ 与 sqlalchemy（managed venv 已含；无需 alembic）
git clone <本仓库> && cd ledgeros          # 或直接复制整个仓库目录

# 1) 初始化：任意空目录 → 一个 XErp 账套项目（幂等，可重复跑）
python integration/workbuddy/xerp_project.py init  <项目目录> \
        --name 我的账套 --owner 老板 [--owner-ref WB用户ID] [--reviewer-ref 审批人ID]

# 2) 只读问答（确定性 Copilot，零 MCP 依赖）
python integration/workbuddy/xerp_project.py ask   <项目目录> "总体经营情况如何"

# 3) 自检：全 [PASS] = 复制成功，可开工
python integration/workbuddy/xerp_project.py doctor <项目目录>
```

产出结构（**全部在项目目录内，数据自持**）：

```
<项目目录>/
├── xerp/ledger.db          # 账库（事件溯源 + 余额）：复制目录 = 复制账套
└── xerp.project.json       # 项目↔账套映射（ledger_set_id / 双身份 / 科目统计）
```

---

## 三、复制目录 = 复制账套（核心卖点）

- **迁移一张账套** = 复制 `<项目目录>/` 整个文件夹到新机器（U 盘 / 网盘 / NAS 均可）。
- 新机器上**直接 `doctor`**，无需任何 `alembic upgrade` / 数据库迁移命令。

### 内置迁移器（P1-1，零配置升级）

`doctor` / `init` / `ask`（经 `open_session`）打开账套时自动调用 `kernel.migrate.ensure_schema_current(db_url)`：

- **零依赖**：纯 `sqlalchemy` + sqlite `PRAGMA`，不依赖 alembic CLI，随内核分发。
- **抗漂移**：按 ORM 元数据补齐缺失表（`create_all`，幂等）+ 缺失列（`ALTER TABLE ADD COLUMN`，幂等）；老库经旧 `create_all` 预建了部分表 / 列、版本落后也能安全补齐，**根除"老库进账套 500 / no such column"事故类**。
- **NOT NULL 无默认列兜底**：对已填充旧表补列时自动给 `DEFAULT 0 / ''`，ALTER 不崩。
- **版本记录**：写 `xerp_meta.schema_version`，`doctor` 首步 `00_schema_migrate` 报告 `升级 <旧>→<新> 已是最新 / 补齐N列`。
- **SQLite 只增不改**：XErp 演进历史仅 `ADD COLUMN` / `CREATE TABLE`，绝不做 `DROP` / `ALTER TYPE`，补齐策略天然安全。

> 设计取舍：保留既有 alembic 迁移（`migrations/versions/0001-0012`）用于开发期历史回溯；**运行时升级路径统一走内置迁移器**，不再要求终端用户安装 / 执行 alembic。

---

## 四、并发韧性（团队模式）

- **SQLite WAL + Busy-Retry**（P0-2）：`kernel/webapp.py` `build_app` 在 SQLite 引擎上设置
  `journal_mode=WAL` + `busy_timeout=5000` + `synchronous=NORMAL`，根治 `database is locked`，
  支撑 NAS / SMB 共享账套多人协同审批 / 查询并发。
- **静默降级**：只读库 / 部分网络文件系统不支持 `-wal/-shm` 时，`PRAGMA` 失败被吞掉，回退默认模式，不影响打开。
- **`.gitignore` 已忽略** `*.db-wal` / `*.db-shm` 运行时产物，避免误提交。

---

## 五、`doctor` 红线守护（全 PASS = 就绪）

`doctor` 自检项（顺序即启动动作）：

| 项 | 含义 |
|---|---|
| `00_schema_migrate` | 打开即零感知升级 Schema（复制旧账套也安全） |
| `01_manifest` | 项目清单存在且可解析 |
| `02_db_file` | 账库文件存在 |
| `03_ledger_set` | 账套实体存在 |
| `04_chart_of_accounts` | 科目模板已导入（146 个） |
| `05_open_period` | 当期 OPEN |
| `06_dual_identity` | 老板 / 审批人双身份齐备 |
| `07_copilot_smoke` | 只读 Copilot 冒烟（intent + answer） |
| `08_readonly_guarantee` | 只读保证：ask 前后凭证数不变（HITL 铁律可执行证明） |
| `09_wb_approval_channel` | 审批路由基板就绪（内核级，不依赖 MCP/WB） |
| `10_no_workbuddy_import` | 零 WorkBuddy 依赖，L0 逃生舱成立 |

> 任一项 `[FAIL]` 都意味着"复制不完整 / 账套损坏"，须按明细排查后再开工。

---

## 六、发布到 WorkBuddy 应用（线上演示 / 客户托管）

- **XErp 云演示**已发布：`https://xerp-demo.app.workbuddy.host/`（Apple 极简风 + 移动端响应式，链接不变、覆盖发布）。
- 发布载体：`xerp-web-demo/`（脱敏云部署副本，含 `main.py` 硬编码 `0.0.0.0` + `XERP_WEB_HOST` + 同目录 SQLite，依赖已剔除 PG/psycopg/fastmcp 以过沙箱预检）。
- 重新发布：调用 WorkBuddy 发布能力，`directory=xerp-web-demo`、`startCmd=python main.py`、`installCmd=pip install -r requirements.txt`，复用既有 `xerp-demo` 应用覆盖。

> 注意：`xerp-web-demo` 与内核 `webapp.py` 存在部署专属差异（cloud_config / provisioning / IdentityBinding），**Apple 风等设计层改动已回内核**，但部署分支不回写内核（避免冲掉云端逻辑）。

---

## 七、私有交付包内容（P2 目标形态）

```
交付包/
├── kernel/                 # 内核（JEV + 双引擎 + 内置迁移器），零 WB 依赖
├── integration/workbuddy/  # 模式包：xerp_project.py + 四件套 + 本文档
│   ├── README.md
│   ├── ARCHITECTURE.md     # 架构白皮书
│   ├── DEPLOYMENT.md       # 本手册
│   ├── PROJECT_INSTRUCTIONS.md
│   ├── AGENT_ORCHESTRATION.md
│   ├── AUTOMATIONS.md
│   └── experts/xerp-accountant/
├── migrations/versions/    # alembic 历史（开发期回溯）
└── tests/                  # 回归钉死（test_embedded_migrate / test_migrations / test_tool_profiles …）
```

---

## 八、故障排查

| 现象 | 根因 | 处置 |
|---|---|---|
| 进账套 500 `no such column` | 旧账套缺新列（未走迁移器） | 跑 `doctor` → `00_schema_migrate` 自动补齐；或 `xerp_project.py` 任意命令打开即升级 |
| `database is locked` | 并发写冲突 | 确认 WAL 已开（`PRAGMA journal_mode` 应为 `wal`）；共享盘不支持则降级默认模式，错峰写 |
| 老库 `alembic_version` 落后但表已建，`alembic upgrade head` 报 `create_table` 冲突 | 迁移漂移 | 不再用手工 alembic；统一走内置迁移器（自动跳过已建表/列） |
| 提交时 `.db-wal` / `.db-shm` 被跟踪 | 未忽略 WAL 产物 | `.gitignore` 已加 `*.db-wal` / `*.db-shm`；`git rm --cached` 移除已跟踪项 |
| `git push` 报密钥 | 真实密钥入 diff | 仅对本次 diff 做密钥扫描（`git diff base head \| grep`），`.env` 已被 `.gitignore` 覆盖且未跟踪 |

---

## 九、提交门槛（贡献内核必须遵守）

1. **密钥扫描**：仅针对本次 diff（`git diff base head | grep -niE "..."`），**严禁 `grep -r` 无文件参数**扫全仓暴露 `.env` 真实密钥。
2. **.gitignore 核查**：`.env` 已覆盖未跟踪；WAL 产物已忽略；`git status` 确认无密钥 / 大库文件。
3. **简体中文提交**：commit message 用简体中文，描述"为什么"而非"改了什么"。
4. **回归全绿**：`test_tool_profiles` / `test_embedded_migrate` / `test_migrations` / web 套件 / 边界纪律 + 集成 ACL 全绿后再推送。
5. **推送复核**：`git push` 后 `git fetch origin` 复核远端与本地一致。

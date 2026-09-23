# XErp × WorkBuddy：项目即账套 · 可复制模式包

> **一句话**：一个 WorkBuddy 项目 = 一个 XErp 账套；**复制项目目录 = 复制账套**。
> 本目录是把「寄生在 WorkBuddy 上」做成产品级模式的落地包，可在任何一台电脑上 3 步复制验证。

## 一、五锚点映射（全部用 WorkBuddy 项目原生能力，不依赖易碎集成）

| WorkBuddy 项目配置 | 本包对应物 | 作用 |
|---|---|---|
| 指令 | `PROJECT_INSTRUCTIONS.md` | 每次会话自动带入账套上下文 + HITL 铁律（纯文本，升级最稳） |
| 专家 | `experts/xerp-accountant/` | 「会计专家」一等协作者（包装 copilot_ask 确定性 Copilot） |
| 技能 | 既有 `xerp` skill（`~/.workbuddy/skills/xerp/`） | 记账 SOP 与工具地图 |
| 定时任务 | `AUTOMATIONS.md` | 月结预检 / 账龄催收 / 收款匹配，定时只读+草稿 |
| 资产 | 三表/审计/GB-T 导出 + 本包自身 | 团队共享档案；模式包随项目走 |

## 二、分层韧性（回答「WB 升级会不会限制我的能力」）

| 层 | 载体 | WB 升级影响 |
|---|---|---|
| **L0 内核直连** | `xerp_project.py`（只 import kernel，python+sqlalchemy 即可跑） | **零影响**——WB 拿不走的能力 |
| L1 项目指令 | 纯文本配置 | 几乎为零 |
| L2 项目技能 | xerp skill | 低（有 L0 兜底） |
| L3 项目专家 | expert 包 | 低（有 L0 兜底） |
| L4 MCP 连接器 | `~/.workbuddy/mcp.json` | 唯一真正集成点；挂了 L0-L3 仍可用 |

`doctor` 永远验证 L0（含「零 workbuddy import」红线自检），这是整套模式的保底。

## 三、新电脑 3 步复制验证

```bash
# 0) 前置：python 3.11+ 与 sqlalchemy（本机 managed venv 已含）
git clone <本仓库> && cd ledgeros        # 或直接复制整个仓库目录

# 1) 初始化：任意空目录变成一个 XErp 账套项目（幂等，可重复跑）
python integration/workbuddy/xerp_project.py init  <项目目录> --name 我的账套 --owner 老板

# 2) 只读问答（确定性 Copilot，零 MCP 依赖）
python integration/workbuddy/xerp_project.py ask   <项目目录> "总体经营情况如何"

# 3) 自检：全 [PASS] = 复制成功，可开工
python integration/workbuddy/xerp_project.py doctor <项目目录>
```

产出结构（全部在项目目录内，数据自持）：

```
<项目目录>/
├── xerp/ledger.db          # 账库（事件溯源 + 余额），复制目录即复制账套
└── xerp.project.json       # 项目↔账套映射（ledger_set_id / 双身份 / 科目统计）
```

接着（可选增强，挂了也有 L0 兜底）：
1. 项目配置 → 指令：粘贴 `PROJECT_INSTRUCTIONS.md` 内容；
2. 项目配置 → 专家：注册 `experts/xerp-accountant/`（本机专家目录
   `~/.workbuddy/plugins/marketplaces/my-experts/plugins`，用 expert-manager 的
   `validate_expert.py` / `register_expert.py` 校验注册；头像可用 ImageGen 生成放 `avatars/`）；
3. 项目配置 → 技能：挂既有 `xerp` skill；
4. 项目配置 → 定时任务：按 `AUTOMATIONS.md` 配置；
5. 连接器：MCP `xerp`（`XERP_DB` 指向 `<项目目录>/xerp/ledger.db`）。

## 四、边界与约定

- **HITL**：本包只做「建账 + 只读问答 + 自检 + 文档」，绝不触碰 push/approve/post/close 终态。
- **多账套**：一个项目一个账套；集团合并仍走内核 `consolidate_*`（pro 档）。
- **老账迁移**：旧 `ledgeros_dev.db` 的账套不自动搬；新项目用 `import_opening_balances` 导期初起步。
- **测试钉死**：`tests/test_workbuddy_project.py`（幂等建账 / 只读问答 / doctor 全绿 / 零宿主依赖红线）。

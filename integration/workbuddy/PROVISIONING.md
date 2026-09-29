# 一键开通个人账套 · 产品化（PROVISIONING）

> 配套：`ARCHITECTURE.md`（架构白皮书）· `DEPLOYMENT.md`（部署手册）
> 内核真源：`kernel/provisioning.py` · 集成层：`integration/workbuddy/xerp_project.py` · Web：`kernel/webapp.py`

## 一、这是什么

「一键开通个人账套」是 XErp **产品化的第一道入口**：一个外部身份（WorkBuddy uid /
飞书 open_id / 本地 Web 会话 / CLI `--owner-ref`）首次接入，即**确定性、幂等**地开好一套
可记账的个人账套，并复用既有内核路径（单一真源，ADR-002），产物 **HITL 就绪**
（双身份 + Casbin 角色 + 外部键绑定）。

它是「寄生 WB 平台」的应用模板层（C），把此前分布在 `xerp-web-demo` 部署分支里的
`provisioning.py / IdentityBinding / cloud_config` 收敛回内核，消除内核与部署分支的分叉，
成为唯一建账原语——`init_project`（CLI）与 Web `/init` 向导都收敛其上，绝不复制配平/取数。

## 二、两种语义（同一原语，单一真源）

`kernel.provisioning.provision_personal_ledger(session, *, display_name,
external_ref=None, owner_subject_id=None, ledger_name=None, template=None,
reviewer_name="审批人")`：

| 语义 | 触发条件 | 行为 | 幂等性 |
|---|---|---|---|
| ① 身份联邦首次接入 | 给定 `external_ref` 且 `owner_subject_id` 为空 | 首次建账套 + 绑定外部身份键；之后同一身份直接返回既有（`is_new=False`） | **幂等**（同一 external_ref 不重复建账套） |
| ② 已登录用户主动新建 | 给定 `owner_subject_id`（如 Web `/init` 向导） | 始终为该现有主体**新建一套**账套并授予 admin（一个主体可有多个账套） | **不复用既有**（每次都是新账套） |

> 关键区分：Web `/init` 向导是「已登录用户再开一套账套」的显式动作，**不应**按当前登录主体做幂等——
> 否则同一用户多次开账会反复返回第一套，丢失新建语义（这是早期实现踩过的坑，已修）。

## 三、建账产物（两种语义共同）

1. **个人账套** `LedgerSet`（按起步模板命名 COA，空账套）。
2. **科目表**：`import_chart_of_accounts(load_template_rows())` 导入小企业会计准则 144 科目。
3. **当期 OPEN 期间**：`Period(year, month, status="OPEN")`。
4. **双身份（HITL 结构性保证）**：
   - 所有者主体 `owner`（语义①新建设定 `external_ref`；语义②复用现有主体），授予 **admin**。
   - 默认审批人主体 `reviewer`（display_name="审批人"），授予 **accountant + reviewer**。
   - `owner ≠ reviewer` 由内核强制，制单≠审批不靠约定靠结构。
5. **起步模板（开账档案）**：见第四节，决定命名/准则/引导清单，科目表当前统一复用小企业准则。

## 四、起步模板（产品化核心）

不同个人场景选不同「开账档案」，复用同一套小企业准则科目，但给出贴合的命名、准则与
开账后引导清单：

| key | 标签 | 适用 | 引导重点 |
|---|---|---|---|
| `small_business` | 小微企业（会计准则） | 有限责任公司 / 小微企业 | 录期初 → 确认审批人 → 日常记账 → 月末结账 |
| `individual` | 个体工商户（简易账） | 个体户 / 个人工作室 | 可跳过期初 → 收支流水 → 季度经营 |
| `sole_proprietor_ltd` | 一人有限公司 | 一人有限责任公司 | 公私账分离 → 报销走制单审批 → 设审批人 |
| `nonprofit` | 非营利 / 社团 | 社团 / 非营利组织 | 限定性资产单独核算 → 理事审批 → 按项目归集 |

- 默认模板 `DEFAULT_TEMPLATE = "small_business"`；未知/空 key 自动兜底默认（产品化要兜底，绝不抛错）。
- 通过 `kernel.provisioning.list_templates()` / `get_template(key)` 供 CLI / Web / Skill 展示与解析。

## 五、身份联邦端点（A · 寄生 WB 平台）

Web 层（`kernel/webapp.py`）在公开路径上暴露三个端点，首次接入即「一键开通」：

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/identity/bind` | POST (`external_ref`, `display_name`) | 外部身份首次接入：幂等开通/绑定个人账套 + 主体，返回 ids |
| `/api/identity/status` | GET | 当前 XERP 会话主体是否已联邦绑定个人账套（解析会话 Cookie） |
| `/login/wb` | POST (`external_ref`, `display_name`, `next`) | 联邦 SSO 桥接：确保外部身份已开通（首次即一键开通），并以该主体置 XERP 会话 |

> 这三个端点在 `PUBLIC_PATHS` 中，无需先登录即可首次接入；内核级零 WorkBuddy 依赖，
> `external_ref` 由调用方传入，平台层只做确定性建账。平台层结构性保证：不同外部身份映射到
> 不同账套、制单与审批在不同身份下天然分离。

## 六、入口矩阵（全部收敛到 `provision_personal_ledger`）

| 入口 | 调用方式 | 语义 |
|---|---|---|
| CLI `xerp_project.py init` | `external_ref = owner_ref or f"project:{项目路径}"` | ① 幂等（一个项目=一个账套） |
| CLI `xerp_project.py templates` | `list_templates()` | 列出可用起步模板 |
| Web `/init` 向导（未登录） | `external_ref = f"web:{owner_name}"` | ① 幂等（同人重复提交不双建） |
| Web `/init` 向导（已登录） | `owner_subject_id = 当前登录主体` | ② 新建（一个主体可多账套） |
| Web `/api/identity/bind` / `/login/wb` | `external_ref = 外部身份键` | ① 幂等（身份联邦） |

## 七、零宿主依赖红线（L0 逃生舱）

`kernel/provisioning.py` **不得** `import workbuddy` 或 `from kernel.adapters`，
建账逻辑完全落在内核（SQLAlchemy + Casbin + COA 导入）。WB / 飞书 / 本地 Web 只是传入
`external_ref` 的调用方——WB 升级、MCP 断连、技能下架都不影响建账能力。
`tests/test_provisioning.py::test_no_workbuddy_dependency` 钉死此红线。

## 八、回归钉死

- `tests/test_provisioning.py`：幂等开通 / 多租户隔离 / 双身份 + HITL / 模板兜底 / 同名后缀 /
  缺失 external_ref 拒绝 / `list_role_members` 按动作反查修复 / 零宿主依赖。
- 关联：`tests/test_workbuddy_project.py`（项目即账套）、`tests/test_webapp.py`（Web 向导）、
  `tests/test_authz.py`（Casbin 角色反查）。

# 从逆向用友总账到造一个 XErp
## AI Native ERP 实战系列 · 讲义总纲（v1.0）

> 素材来源：2026-08-27 单日实战全记录（9 个任务、50 测试、2 轮真机排障、Gate G2 达成）
> 用途：公众号「AI前沿技术分享」连载 + BDC 内部 AI 培训 + FDE 方法论展示
> 受众：工程师 / AI 爱好者 / 财务数字化从业者；每讲 15-25 分钟
> 特色：**所有结论都有一手证据**——提交哈希、测试输出、翻车现场截图，不讲空对空

---

## 系列主线（一句话）

> 把一个 30 年历史、4.1GB、90 个安装包的传统 ERP 总账模块，逆向成一张治理机制映射表，
> 再用 AI 结对开发，在一天之内造出「对话即记账」的开源智能体 ERP 内核，并在 WorkBuddy 里真机跑通。

```
逆向 U8 ──► 提炼治理原语 ──► 设计 XErp ──► TDD 实现 ──► 真机验证 ──► 开源路线
 (第1讲)      (第2讲)         (第3讲)        (第4-6讲)     (第7讲)      (第9讲)
```

---

## 第 0 讲 · 导论：传统 ERP 的「三难」与 Agent 的机会

**学习目标**：理解为什么「再造一个传统 ERP」没有意义，而「AI Native 重构」有。

- 现场证据开场：U8+ V18.0 完整安装介质 4.1GB / 90 个 MSI / 仅总账一个模块就有 2070 个文件
- 三难病灶与一手证据对照：
  - 实施难 → InstallShield 引导 + 90 MSI 依赖拓扑 + .NET/VC++/SQLCLR 前置链
  - 维护难 → CustomAction 文件日期比对 + UFBAK 备份回滚的增量补丁机制
  - 上手难 → 仅总账就有 1968 个 .rep 打印模板、三代冗余并存的科目组件
- 核心论点：**传统 ERP 的复杂度大半来自「为每种人预先画好每种界面」**；
  Agent ERP 把界面生成推迟到交互时刻（A2UI）、把规则收敛为本体（Ontology）、
  把流程交给 AI Runtime——复杂度从百万行代码转移到模型理解力，而模型理解力是免费升级的
- 互动：你所在公司上一套 ERP 花了多久？其中「画界面」和「配流程」占多少？

---

## 第 1 讲 · 逆向工程：不写一行代码读懂一个 ERP 模块

**学习目标**：掌握只读解析商业软件元数据的方法论（合法、clean-room）。

**方法工具箱**（全部免安装逆向工具）：
1. WindowsInstaller COM（PowerShell）查询 MSI 数据库：Property / Component / File /
   CustomAction / Binary / Directory 表——`dump_gl_msi.ps1` 讲解
2. 部署描述 XML（u8erp-cw-gl.xml）：client/app/db/common 四层依赖 → 模块关系图
3. 文件名考古学：语义命名组件（U8GLAPI / ZWSQL / USPZLB / GLReconc）
   vs 混淆命名（ovqx1lqj.dll，同批时间戳暴露同源打包）
4. 交叉验证：解析结论回查官方 PDF 文档（1968 个 .rep=账表打印模板，Setup 手册第 971 行）

**关键发现**：总账 = U8 的「凭证汇聚总线」——18 个业务模块经固定 DLL 接口汇入凭证，
预算控制（BPM-BM 17 组件）在凭证保存时生效。

**合规红线**（必讲）：只读元数据、只借鉴业务逻辑概念；反编译/复制代码与模板是法律禁区。

互动：给你一个 24MB 的 MSI，你能列出它往系统里装了什么、注册了什么吗？

---

## 第 2 讲 · 设计的核心资产：治理机制 → Agent 原语映射表

**学习目标**：学会把「旧系统的隐性知识」翻译成「新系统的显式设计」。

这是整个系列最有含金量的一张表（逐行讲）：

| U8 机制（实证） | 本质 | XErp 对应物 |
|---|---|---|
| 总账=凭证汇聚总线 | 事件汇聚 | 业务事实→领域事件→账务化管道 |
| 凭证状态机（填制→审核→记账→结账+逆向取消） | 有界自治 | HITL：Draft→Push→Review→Commit，**与手稿完全同构** |
| BPM-BM 预算 17 组件凭证时生效 | 护栏 | Policy as Code |
| code 科目表+辅助核算 | 语义骨架 | Ontology（对齐 Palantir） |
| gl_accvouch/gl_accsum 事实与汇总分离 | 事件溯源 | Event Ledger + 物化投影 |
| 1968 个 .rep 模板 | 静态 UI | A2UI 动态生成+模板兜底 |
| ZWSQL 数据通道 | 受控访问 | MCP Resource 网关 |
| UFBAK 备份回滚 | 挽回 | 事件回放（天然能力） |

金句：**传统 ERP 早就用 30 年验证了 HITL 该怎么门控——我们不是发明，是翻译。**

---

## 第 3 讲 · 架构：八层栈与五条铁律

**学习目标**：能独立画出 XErp 分层架构并解释每条铁律的「为什么」。

- 八层栈：L0 审计权限 → L1 Ontology/事件账本 → L2 MCP 能力层 → L3 AI Runtime(HITL)
  → L4 编排(可插拔) → L5 A2UI → L6 应用层 → L7 部署
- 五条铁律逐一论证：
  1. **确定性内核 + 概率性外壳**——LLM 永不手写金额（财务场景生死线）
  2. **审计即架构**——append-only 不能后补
  3. **权限先于功能**——Agent 是一等公民但受三重门禁
  4. **HITL 分级自治 L0-L3**——出厂默认 L1
  5. **单点纵深**——总账没到 GA 不碰其他模块
- 决策记录制度：5 份 ADR 先于代码（ADR-001~005 速览）

---

## 第 4 讲 · 事件账本：让「不可篡改」成为被测试证明的事实

**学习目标**：理解 append-only + hash 链的实现与验证方法。

- 设计：`events` 表信封结构 / `hash = sha256(prev_hash + canonical_json(...))` 账套内成链
- **canonical_json 规范**：键排序、无空白、金额 str(Decimal)、
  `occurred_at` 统一 UTC naive isoformat——为什么？因为 SQLite 往返会剥 tzinfo（真 bug 复盘）
- 双数据库触发器：PG plpgsql / SQLite RAISE(ABORT)，方言分支迁移
- `verify_chain` 双检查：linkage_broken + hash_mismatch
- 测试矩阵：篡改 payload → 检出；删中间行 → 断链检出；多账套链隔离
- **本讲翻车实录**（讲课最出彩部分）：
  ① BigInteger 让 SQLite rowid 自增静默失效 → 方言变体 Integer+BIGINT
  ② 测试种子没 commit，独立连接读空表——「空链永远说真话」的假绿
  ③ 测试规范由此沉淀：**凡跨连接必显式 commit**

---

## 第 5 讲 · 确定性记账内核与 HITL 状态机

**学习目标**：掌握「规则引擎如何硬拒 AI/人的一切非法操作」。

- `validate_voucher` 七类分支硬拒（NO_LINES / VOUCHER_UNBALANCED / LINE_BOTH_SIDES /
  AMOUNT_INVALID / ACCOUNT_NOT_FOUND / PERIOD_NOT_OPEN / PERIOD_MISMATCH）
- `post_voucher`：仅 APPROVED→POSTED，写事件 + balances 投影累计
- `cancel_post_voucher` 补偿事务：仅未结账期间；**追加事件不改历史**；
  投影回冲、零行删除；撤销后可完整重放生命周期
- 门禁三件套：AGENT_APPROVAL_FORBIDDEN / AUTONOMY_DENIED(L3) / NO_SELF_APPROVAL
- 质量手段：pytest-cov 分支覆盖 **100%**（72 语句 26 分支）作为任务 DoD
- 子代理开发模式：实现代理产出 → 主会话规格+质量双评审 → 独立复跑核实

---

## 第 6 讲 · MCP 工具层：让任何 Agent 客户端即插即用

**学习目标**：掌握面向 LLM 的工具契约设计。

- 9 工具全景（含后来实战补的 get_workspace——「Agent 无法自举」的真实缺口）
- ADR-003 契约四件套：
  - 金额一律 decimal-string（**任何地方禁止 float**）
  - 错误信封 `{ok, error:{code, message_zh, details}}`——中文消息可直接展示
  - 幂等键防重（重放返回 replayed=true）
  - actor 强制透传（审计前置）
- 测试策略：fastmcp 内存客户端自动化替代 inspector 手测（8 用例含全链路断言）

---

## 第 7 讲 · 真机联调：两次翻车与 Gate G2 的达成

**学习目标**：理解「演示成功」背后必须排掉的隐性陷阱。

- **翻车一**：新会话说「报销招待费800」→ AI 去生成了报销单 DOCX 而非记账
  - 根因1：SKILL.md 只在仓库 ≠ 装进客户端（~/.workbuddy/skills/ 才生效）
  - 根因2：缺「第零步守卫」——工具缺失时应停止+引导连接，禁止替代方案
- **翻车二**：`MCP error -32000: Connection closed`
  - 根因：`_REPO_ROOT` 向上取层少一层 → ModuleNotFoundError → 进程秒退
  - 排障三步法：管道级握手冒烟（echo initialize | python server.py）→ 看 stderr 定位
  - 深层教训：**进程内单测测不到启动路径与协议层**——必须管道冒烟入 CI
- **达成时刻**：真机凭证 记-0001 全链路 POSTED；Agent 自选明细科目 660204；
  事件链四事件齐全，verify_chain 完整
- 亮点分析：AI 主动选了 660204「业务招待费」明细而非 6602——科目模板 + 辅助维度
  设计（往来走维度不建子科目）真正被模型用起来了

---

## 第 8 讲 · 方法论沉淀：AI 结对开发的一天怎么干

**学习目标**：可复制的工程节拍，独立于 ERP 领域。

1. **计划勾销制**：任务带唯一 ID / 产出物 / 可验证 DoD / 依赖 / 估时；完成即勾销并记实际人日
2. **TDD 先红后绿**：当日真 bug 全部由测试先行暴露（tzinfo 漂移 / BigInteger 自增 /
   relationship 漏定义 / GBK 编码崩配置 / 未 commit 跨连接 ×3）——逐个复盘
3. **Gate 制**：G1 pytest 绿 → G2 真机通 → G3 30 分钟交付 → G4 v0.1
4. **子代理三评审**：实现代理 → 规格评审 → 质量评审 → 独立复跑
5. **知识双写**：仓库文档（ADR/DEVPLAN）+ 客户端技能（SKILL.md）缺一不可
6. 效率对比：9 任务实际 ~4.7 人日 vs 预算 10.5 人日

---

## 第 9 讲 · 路线图与开源商业化（FDE 视角）

- W3/W4：Drill 对话建账向导 → 飞书审批卡片 → docker compose → Web 界面 → 审计回放 CLI → v0.1
- P1-P4：三表投影/期末结转 → 应收应付/发票 OCR → 无人值守月结（3天→2小时）→ 开源生态
- 开源策略：Apache-2.0；WorkBuddy/飞书技能包分发；dogfood 自己公司真实账套
- 商业闭环：开源内核引流 → FDE 交付咨询 → 公众号内容放大
- 待决：D4 代号 XErp / D3 GitHub 私有仓

---

## 附录

- A. 环境速查：Python 3.12+ / FastAPI / SQLAlchemy 2.0 / alembic / fastmcp / pytest-cov / ruff
- B. ADR 索引：kernel 选型 / 事件账本 / MCP 契约 / HITL 状态机 / 权限模型
- C. 今日时间线：reverse-skill 安装 → U8 介质解析 → 知识库语料 → GL 逆向 → XErp 立项 → P0-01~09
- D. 互动题库：每讲 1-2 问已内嵌，合计 12 问
- E. 仓库：`ledgeros/`（50 测试全绿）；讲义对应文档：`docs/DESIGN.md` + `docs/DEVPLAN.md` + `docs/adr/`

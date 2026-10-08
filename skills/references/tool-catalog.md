# XErp MCP 工具清单（108 个 · 三档分层）

> 本文件是 XErp 智能记账技能的「工具地图」。AI 在对话中驱动 XErp 时，
> 据此判断该调用哪个工具、属于哪个档位、有什么约束。
> 工具定义以 `mcp-server/xerp_mcp/server.py` 为唯一真源；分档以 `profiles.py` 为唯一真源。
> 本清单与 `profiles.py`（MINIMAL 19 / STANDARD_EXTRA 58 / PRO_ONLY 31）严格一致，
> 由 `test_tool_profiles.py` 钉死并集 == 运行时工具全集。

## 分层原则

同一套确定性内核，三种暴露面（通过 `mcp.json` 的 `disabledTools` 裁剪，**改配置不改代码**）：

| 档位 | 数量 | 面向 | 说明 |
|---|---|---|---|
| `minimal` 极简 | 19 | 个人/小微 | 建账→制单→审→记→查账→两张主表+引导+科目本体+算子状态 |
| `standard` 标准 | 77 | 中小企业 | 极简 + 驳回/撤回/反记账/多级签字/月结全套（含子账总账对账·外币重估只读草稿）/往来资金/银行对账/OCR/三表预测/情景推演/专项核算/账本精灵主动推送/AI风险预警/报税准备/审计追踪/审计索引检索/票据入账预览门禁/应收应付（对账单+账龄+未清项+核销草稿+信用管理+智能催收+收款自动匹配）/GB/T 24589 审计导出 + WB 原生审批通道(WB 主入口)/WB 成员绑定 + 存货固定资产成本（8）/预算编制对比（5）/JEV 确定性决策（判断/打分/选择，只读）/运营财务画像·图谱指标·实时Copilot/AI 驾驶舱快照 cockpit_snapshot（只读） |
| `pro` 专业 | 108 | 代账/专业 | 全量：自治授权/过账、异常扫描、适配器、转账模板、飞书/企微审批通道、多主体合并报表、合并工作底稿、合并血缘下钻、合并层级标注、内部往来配对草稿、长投权益抵销草稿、合并现金流量表、合并现金流抵消草稿、应收应付核销落地 |

**铁律**：`minimal ⊆ standard ⊆ pro`；三集合互不相交且并集 = 运行时全部工具。
新增工具必须显式归类，否则 `test_tool_profiles.py` 红（把「忘了归类」变成可捕获失败）。

---

## 一、MINIMAL 极简（19）

主链路：会话自举 → 建账 → 制单 → 审核 → 记账 → 查账 → 报表 → 引导。

| 工具 | 中文描述 | 关键约束 / 触发 |
|---|---|---|
| `get_session_context` | 会话自举：账套列表（含 id 与会计口径）、操作者身份（制单人/审批人及主体 id）、各账套开放期间 | 任何操作前先调用，确定 `ledger_set_id` 与操作者 `actor_id` |
| `init_ledger_set` | 【建账向导第 1 步】创建新账套：导入准则科目模板 + 建当月 OPEN 期间 + 注册所有者身份 | 新建公司/账套时触发 |
| `ensure_period` | 确保某期间存在且为 OPEN（跨月记账前置）；已存在则原样返回 | 制单前确认期间存在 |
| `list_accounts` | 列出账套科目（编码/名称/方向/类别/是否叶子/辅助维度定义）；`keyword` 过滤编码或名称 | 制单前找科目、查科目真名 |
| `import_opening_balances` | 【建账向导第 2 步】导入期初余额（试算平衡自动校验 + 防重复导入） | 建账后导入存量 |
| `create_voucher` | 创建草稿凭证并即时硬校验 | 一句话制单的落点；金额须 Decimal 字符串 |
| `push_voucher` | 提交待审：DRAFT → PUSHED | 制单人提交 |
| `approve_voucher` | 审批通过：PUSHED → APPROVED。**制单人与审批人不能相同（NO_SELF_APPROVAL）** | 换人审批，AI 只产草稿 |
| `post_voucher` | 记账：APPROVED → POSTED。写 posted 事件并累计余额投影 | 终态前最后一道，仍需非制单人 |
| `get_voucher` | 按 id 取凭证全量（状态机当前态 + 分录明细） | 查看/联查 |
| `query_balances` | 查期间发生额投影（过账后可见）；`account_prefix` 过滤科目编码前缀 | 查某科目/某类发生额 |
| `report_balance_sheet` | 资产负债表：按准则模板聚合资产/负债/权益，返回是否平衡与差额校验 | 出表、核对平衡 |
| `report_income_statement` | 利润表：营业收入/成本/费用分项 + 净利润（**净额口径**：收入=贷-借、费用=借-贷） | 出表、看盈利 |
| `precheck_close` | 结账前体检：一次查完四道闸门，返回「还差什么」清单 | 月结前必调 |
| `preview_closing` | 期末结转预览（只读）：结账前先看清楚「点下去会发生什么」 | 月结前确认 |
| `suggest_summaries` | 常用摘要推荐：某科目历史上用得最多的摘要，按次数降序 | 制单填摘要时 |
| `month_end_guide` | 账套状态引导：把「这个月我该怎么走」讲给财务新手听 | 月初/迷茫时 |
| `subject_semantics` | 科目本体语义查询：制单前先问一句「这个科目有什么讲究」 | 防科目误用 |
| `ai_runtime_state` | 读/写算子（AI Runtime 具象）状态（只动状态信号，不碰账） | 查自治/算子状态 |

---

## 二、STANDARD_EXTRA 标准（58，叠加在极简之上）

凭证逆向动作、明细勾稽、月结全套、往来资金、票据、预测、业务向导、专项核算、账本精灵主动推送、只读合规三件套（风险/税务/审计）、票据入账预览门禁、应收应付（对账单+账龄+未清项+核销草稿+信用管理+智能催收）、国标审计导出。

| 工具 | 中文描述 | 关键约束 / 触发 |
|---|---|---|
| `reject_voucher` | 审批驳回：PUSHED → DRAFT，退回制单人修改后可重新提交（reason 必填） | 审批人驳回 |
| `withdraw_voucher` | 制单人撤回：PUSHED → DRAFT，审批前自行收回（reason 选填） | 制单人反悔 |
| `cancel_post_voucher` | 撤销记账：POSTED → DRAFT（补偿事务） | 红冲前的反向 |
| `sign_voucher` | 多级签字：签署一个签字位（出纳 cashier / 主管 manager 等，D7）；全部签完自动审批通过 | 需多级签章的场景 |
| `ledger_detail` | 科目明细账：期初余额 + 逐笔分录（滚动余额）+ 期末合计，可联查凭证 | 查某科目流水 |
| `reconcile_ledger` | 账账核对：逐凭证平衡、投影 vs 凭证明细重算、试算平衡、现金流勾稽 | 自检/审计前 |
| `export_gbt24589` | **GB/T 24589.1-2024 审计数据接口导出**（合规护城河）：从不可篡改事件链派生国标账表——电子账簿（含事件总数+链尾哈希 provenance）、会计期间、会计科目、币种、科目余额及发生额、记账凭证、记账凭证分录；JSON（2024 版附录 E）或 XML（附录 C）。仅 POSTED 凭证参与，严格继承「期初是存量」语义，记账人/审核人从事件链追溯（兼容历史小写事件串） | 审计/税务/监管采集；需落盘为文件时把 content 写入 .json/.xml |
| `report_cash_flow` | 现金流量表（直接法）：经营/投资/筹资三类净额 + 期初-净增加-期末勾稽 | 出第三张表 |
| `close_period` | 期末结转：损益类科目余额结转至本年利润（3103），生成「结转-YYYYMM-NNN」凭证并**锁期 CLOSED** | 月末结转损益 |
| `open_next_period` | 期初结转：把本期间资产负债类期末余额滚入下一期间 | 开下月 |
| `monthend_run` | 关账 Agent：检查未审→催办→结转→试算→报表草稿→开下期 | 一键月结（编排） |
| `reconcile_subledger_gl` | **子账↔总账对账（Phase D/G9）**：应收(1122)/应付(2202)控制科目余额 vs 客户/供应商明细余额之和，差异即漏挂往来单位的失配；只读、复用 `arap` 单一真源（与 `arap_statement`/`arap_open_items` 同口径），不改账 | 月结前/实时核对子账总账勾稽，差一分钱当场知 |
| `fx_revaluation_draft` | **外币重估只读草稿（Phase D/G7）**：期末汇兑损益重估（has_foreign_exposure 自动判定），产出 PUSHED 凭证草稿，落库由 `fx_revaluation_create`（PRO_ONLY, HITL）执行；对标 SAP/Oracle 未实现汇兑损益。只读、绝不制单 | 外币账套月末重估预览，确认后经 PRO_ONLY 工具落库 |
| `partner_balances` | 往来余额表：按客户/供应商聚合应收与应付，回答「谁欠我、我欠谁」 | 查往来 |
| `bank_import_csv` | 导入银行流水 CSV（表头：date,amount,counterparty,summary,txn_id） | 银行对账前置 |
| `bank_reconcile` | 自动勾对并输出未达账项报告 | 银企对账 |
| `ocr_ingest_invoice` | 一张发票的完整入账流程：提取→校验→查重→凭证草稿 | 发票拍照入账 |
| `forecast_statements` | 三表前向预测（P1-01）：以 base 期末实际三表为种子，按驱动假设外推未来 horizon 期（best/base/worst） | 经营预测 |
| `wizard_scenarios` | 列出业务语言向导的全部场景（自然语言 → 候选分录） | 向导入口 |
| `wizard_propose` | 只读预览：给定业务场景 + 关键参数，返回候选分录、科目真名、逐行解释与借贷是否平衡 | 制单前先预览 |
| `report_aux` | 辅助核算报表：按维度（客户/供应商/部门/项目/其他）透视余额 | 专项核算 |
| `foreign_trial_balance` | 外币试算平衡：按（科目 × 币种）汇总本月 POSTED 凭证的本币与原币借/贷 | 外币账套 |
| `sprite_push` | 账本精灵 7×24 主动推送（O18）：把该账套该期间的主动提醒（月结/异常/财报卡片/健康）推给 Boss；**只读生成、绝不执行**，只发建议不落账。channel: wecom / console / web | 主动触达老板；推送≠执行 |
| `risk_scan` | **AI 风险预警（v2.1/B1）**：只读扫描 SMB 财务风险——表不平 / 负现金 / 应收贷方余额·应付借方余额 / 异常大额 / 历史未结账；只告警不执行。返回 findings（code/severity/title/detail/suggestion，severity ∈ alert\|warn\|info） | 出表/月结前的财务健康体检 |
| `tax_vat_prep` | **小规模纳税人增值税及附加税费季报准备（v2.1/B3）**：只读生成申报草稿——季应税销售额（6001+6051 净额跨 3 月聚合）/ 应纳增值税（征收率 1%，季≤30万免征）/ 附加税费（城建7%·教育费附加3%·地方教育附加2%）。前置检查复用 risk_scan，alert 级阻断申报。绝不替 Boss 报税 | 季报期生成申报草稿，最终申报由 Boss 在税局端确认 |
| `audit_trail` | **审计追踪（v2.1/B2）**：复用 `chain.verify_chain` 做完整性密码学证明 + 中文事件名，生成 tamper_proof / by_type / timeline / summary（✅完整 / ⚠️被篡改 / ℹ️暂无事件）。**只读、绝不改事件链**——审计追踪本身也必须可审计 | 对账本完整性与事件历史做可证明审计 |
| `audit_search` | **审计索引检索（E·审计索引 Cloud DB）**：在 `audit_trail` 之上提供全文/结构化检索——关键词（命中 payload/摘要/事件名）、按执行人、按事件类型、按发生年月，可组合。本地 SQLite FTS5 索引 + 可镜像到 WB 云端 DB 做持久化跨运行时索引（`XERP_AUDIT_CLOUD_URL` 配置启用，失败自动降级本地）。**只读、零副作用** | 从"可证明审计"升级为"可检索审计"，快速定位某凭证/某人/某类事件的完整轨迹 |
| `ocr_preview` | **发票入账只读预览（O9 统一预览-确认-执行）**：与 `ocr_ingest_invoice` 共用同一决策矩阵与 `build_lines` 取数，预览分录=真入账分录、处置（ingested/flagged/duplicate）与真实一致。人审前置门禁——「先看清楚再点入账」 | 发票入账前先预览后再 ocr_ingest_invoice |
| `arap_statement` | **往来对账单**：某客户/供应商的期初·逐笔流水·运行余额·期末（应收正数=客户欠我，应付正数=我欠供应商）。与 `partner_balances` 同一取数口径（单一真源），期末可逐客户复核 | 给客户/供应商发对账单；dim_key=customer/supplier |
| `arap_aging` | **账龄分析**：按客户/供应商 FIFO 配比，未结清欠款按逾期天数分桶 0-30/30-60/60-90/90+；未结清之和==`partner_balances` 同口径（单一真源）。余额为负=预付 | 应收催款/应付排期；dim_key=customer/supplier |
| `arap_open_items` | **未清项清单（open-item 核销基础）**：列出每张未核销完的发票行=发票金额−已核销额，按账龄分桶。与 SAP open-item 管理对齐——单据级核销，不依赖 FIFO 近似；未清项由「凭证明细行 + arap_clearing 记录」完全重建（ADR-002 单一真源），不新增会漂移的余额投影。未清项之和==往来余额同口径（可逐客户复核） | 看每张发票还欠多少；dim_key=customer/supplier，partner 可空=全部 |
| `arap_propose_clearing` | **核销草稿（只读、不落库）**：为未核销的回款按「金额优先 + 最旧优先(FIFO 兜底)」匹配未清发票，输出建议 assignments，供 Boss 确认后再调 `arap_apply_clearing`。只产草稿不改账——守「AI 只产草稿，落账终态必须人类点头」红线；是 Phase C 收款自动匹配的种子 | 批量核销前先出方案；dim_key=customer/supplier |
| `arap_set_credit_limit` | **授信额度设置（配置写，仅 Boss）**：为某客户（按名）设置/更新赊销授信额度，存 `Party.credit_limit`（客户属性配置，**不是账本余额投影**）。客户无 Party 行时自动建行。是「客户信用管理」的配置入口；不落任何凭证 | 给客户设赊销上限；Boss 显式调，AI 不擅设 |
| `arap_credit_exposure` | **信用敞口扫描（只读）**：按未清应收聚合每个客户的敞口、额度、利用率、超额（敞口>额度）。完全由 `open_items` 派生（ADR-002 单一真源），不新增投影。超额/临近由 `sprite_push` 主动提醒（推送≠执行） | 查谁超额赊销；dim_key=customer |
| `arap_collections_draft` | **逾期催收草稿（只读、不代发）**：逾期未清应收，按账龄升级 L1(提醒)/L2(跟进)/L3(最后通牒) + 生成催收话术草稿。话术仅为文本，XErp 不代发——由 Boss 在 Web/IM 人工执行 | 看谁逾期、催到哪一级；as_of_date 可空=今天 |
| `arap_propose_receipt_match` | **AI 收款自动匹配（只读草稿，不落库）**：把一笔回款智能匹配到未清发票——承接 open_items/record_clearing，是 arap_propose_clearing（FIFO 兜底）的「智能升级」。多信号匹配：备注发票号命中（最高置信 0.99）/ 金额精确（单张 0.95 或多张合计 0.9）/ 付款方名称模糊收敛候选 / 部分核销与多付预警 / 退化 FIFO 兜底（0.6）。每条匹配带 confidence 与中文 rationale（可解释）。输入二选一：已入账回款行 `payment_line_id`（推荐）或自由文本 `receipt`{amount,date,reference,payer}（银行导入/AI 解析场景，需先入账再回填）。确认后经 arap_apply_clearing 落库（HITL）；推送 ≠ 执行 | 一笔回款该冲哪几张发票、信不信得过；待匹配回款由 sprite_push 主动提醒 |
| `workbuddy_send_approval` | **WB 原生审批通道（P0-4）**：仅 PUSHED 凭证可用，把审批请求路由到审批人 WB 身份（写 `VOUCHER_ROUTED` 事件 + 返回深链 `xerp://voucher/<id>`），只读通知不改状态。审批人解析优先级：`wb_member_ref`→账套 reviewer 角色主体→admin 兜底，都不中报 `NO_REVIEWER` | WorkBuddy 项目内审批（WB 是主入口故置于 standard 档） |
| `workbuddy_bind_member` | **WB 成员身份绑定（P0-3）**：把 WB user id 绑定到内核主体（`external_ref` 身份映射键，不参与授权判定）。建账时 `init_ledger_set` 的 `--owner-ref/--reviewer-ref` 已自动绑定；缺失可补绑 | 让某 WB 成员成为可审批人 |
| `inventory_item_register` | **存货档案登记（P0-1，写主数据）**：action=list/create/update/get，登记货品编码/名称/计价方法(weighted_avg·moving_avg·fifo)/默认存货科目(1405/1403)。只写主数据表，**不碰账本与余额** | 启用存货核算前先建货品档案 |
| `asset_register` | **固定资产卡片登记（P0-1，写主数据）**：action=list/create/update/get/dispose，登记资产编号/原值/残值率/年限/开始折旧日；折旧真源仍是 1602 凭证明细（ADR-002） | 启用固定资产核算前先建卡片 |
| `inventory_stockcard` | **存货收发存台账（P0-1，只读）**：由 POSTED 凭证明细重建（ADR-002）——库存科目(1405/1403)行带 `aux_dims["inventory_item"]` + `quantity`。返回期初/本期收/本期发/期末的数量与金额（借=收、贷=发） | 查某货品收发存；item_code 必填 |
| `inventory_valuation_draft` | **存货期末计价 + 结转成本草稿（P0-1，只读）**：weighted_avg(月末一次加权平均)/moving_avg/fifo，产出结转成本 lines（借 6401/贷库存商品；传 physical_count_qty 额外产出盘盈盘亏 1901）。绝不制单；落库经 create_voucher HITL | 月末存货计价与成本结转预览 |
| `depreciation_schedule_draft` | **固定资产直线法折旧 + 折旧凭证草稿（P0-1，只读）**：月折旧=原值×(1−残值率)/年限；每卡片一对「借 6602/贷 1602」带 asset_no（落库后可由 1602 凭证明细重建累计折旧）。绝不制单 | 月末计提折旧预览 |
| `asset_dispose_draft` | **固定资产处置凭证草稿（P0-1，只读）**：转入清理→收款→处置损益（小企业准则收益 6301/损失 6711）；累计折旧由 1602 凭证明细重建。绝不制单 | 固定资产报废/出售预览 |
| `cost_allocation_draft` | **制造费用分摊 + 草稿（P0-1，只读，零新表）**：5101 当月发生额按直接材料/直接人工占比分摊到各 5001 成本对象（project/department 辅助维度）。绝不制单 | 月末制造费用分摊预览 |
| `cost_settlement_draft` | **完工产品成本结转草稿（P0-1，只读，零新表）**：借 1405/贷 5001，完工=期初在产+本期投入−期末在产（WIP 由用户/AI 输入，默认 0 全部完工）。绝不制单 | 月末完工入库预览 |
| `budget_create` | **预算编制（写草稿，HITL 升版前唯一写入口）**：在一个账套内编制某会计年度的预算明细（科目 code × 期间(0=年度均摊,1~12=月) × 金额），存 `budgets`/`budget_lines`，status=DRAFT。**绝不写凭证、绝不进余额投影**——预算是计划数据，与三表口径严格隔离 | 编制年度/月度预算 |
| `budget_copy` | **预算克隆升版（DRAFT）**：基于既有预算克隆为同(账套,年度)的下一版本 DRAFT（version 自增），不覆盖原版本；修订预算走升版而非改历史 | 在旧版基础上做预算修订 |
| `budget_activate` | **预算激活**：置某版本 ACTIVE，同(账套,年度)其余版本自动 SUPERSEDED；激活后即成为该年度预算 v.s. 实际对比的基准 | 启用某版预算 |
| `budget_list` | **预算列表**：列出某账套全部预算版本（版本/年度/状态/编制人） | 看预算版本 |
| `budget_vs_actual` | **预算 v.s. 实际（只读）**：实际数来自 `amounts_by_code`（与三表同口径单一真源），预算净额与 `ending_balance` 同符号（资产/费用借正、负债/权益/收入贷正）；逐科目出 预算/实际/差异 + 合计；年度行按 `amount/12` 月度均摊并标注「年度均摊」；可显式指定 budget_id 覆盖 ACTIVE | 看预算执行偏差 |
| `jev_decide` | **JEV 确定性决策（只读，零外部依赖）**：对财务高频小决策做判断/打分/选择，绝不触发过账/支付/改账（HITL 铁律）。`decision_type` 可选：`budget_variance`(预算差异分级 F11) / `risk_severity`(凭证风险严重度 F14) / `duplicate_voucher`(重复凭证标记 F3) / `ap_open_health`(应付未清项健康度 F1) / `expense_compliance`(费用合规判定 F6) / `approval_route`(费用审批路由 F7)；返回 Decision{kind,label,value,severity,human_review_required,basis,evidence}，歧义/硬约束不满足时 `human_review_required=true` 升人工 | AI 接管"分类/评分"贱活，把 LLM 从高频决策解放；`decision_type` 留空返回可用决策清单 |
| `operating_partner_profile` | **运营财务画像（Phase E/E1）**：往来单位一站式画像——应收/应付/敞口/账龄/对账健康，由 VoucherLine.aux_dims + ArapClearing 动态重建（ADR-002 单一真源），**全只读、零新表零投影** | 看某客户/供应商的全景财务健康 |
| `operating_graph_metrics` | **运营财务图谱指标（Phase E/E1）**：AR·AP 总额 + 敞口 TopN + HHI 集中度 + 对账健康度，全只读、零新表零投影，不改账 | 集团/组合层面的应收应付集中度与风险 |
| `copilot_ask` | **实时 Copilot（Phase E/E2）**：确定性自然语言意图路由（离线可审计、零 LLM 成本），输出 answer_zh/intent/tool_calls/evidence/followups/severity；严重项经算子信号桥置 ALERT（推送≠执行），复用跨进程信号桥联动 Web | 用大白话问账（如"谁欠我最多"），拿可读答案+证据链 |
| `what_if_simulation` | **情景推演（Phase E/E3）**：复用 `forecast` 纯函数做基准 vs 杠杆对比——6 预设杠杆 + 自定义覆盖，逐指标 delta（期末现金/净利润/应收/应付/经营现金流/总资产），**全只读不改账** | 回答"如果延长付款/不扩张，期末现金变多少" |
| `anomaly_healing_suggestions` | **异常自愈建议（Phase E/E4）**：基于 `anomaly.rule_scan` 确定性规则扫描出 HITL 整改清单（`human_approval_required=true`/`auto_executable=False`，绝不自动修复）+ 汇总 Agent 断路器复核建议（`anomaly_release` 仅 admin 解除）；只读不跳闸 | 把"哪里可能错"变成可执行的整改清单，等 Boss 拍板 |
| `cockpit_snapshot` | **AI 原生财务驾驶舱快照（本地优先区隔）**：围绕"管理者此刻该看什么、该做什么决策"重做整条流——一次调用拿齐零日结账视图（三表 KPI + 应收/应付子账↔总账对账健康度 + JEV 异常）与持续预测（what-if 6 杠杆推演）；**只读、复用 `kernel.reporting.cockpit` 单一真源，与 Web `/ledger/{ls_id}/cockpit` 同源**；AI 客户端不必自己拼 5 个工具 | Web 驾驶舱的等价的 MCP 入口，AI 对话里也能出驾驶舱 |

---

## 三、PRO_ONLY 专业（31，仅专业档）

审批通道、外部系统适配器、转账模板、自治授权与风控（会自行过账，属「授权后才开」）、多主体合并报表、应收应付核销落地。

| 工具 | 中文描述 | 关键约束 / 触发 |
|---|---|---|
| `get_workspace` | 【已废弃】过渡别名，请用 `get_session_context`；行为一致 | 不应主动调用 |
| `feishu_send_approval` | 把待审凭证推送为飞书审批卡片（PUSHED 状态） | 飞书审批集成 |
| `wecom_send_approval` | 把待审凭证推送为企业微信模板卡片（批准/驳回按钮，回调写回状态机） | 企微审批集成 |
| `wecom_send` | 企业微信通知推送：text / markdown（agent 主动汇报） | 主动汇报 |
| `wecom_finish_card` | 推送完成态展示卡片：无交互按钮，仅展示最终处理结果 | 结果通知 |
| `adapter_list` | 列出已注册的事件适配器规则（第三方业务事件 → 凭证模板） | 看适配器 |
| `adapter_preview` | 不落库地预览「该事件按规则会生成怎样的凭证」 | 接前先试 |
| `adapter_ingest` | 消费第三方业务事件，按规则自动生成凭证（**幂等**） | 系统对接 |
| `adapter_register` | 运行时注册/更新一条适配器规则——第三方接入唯一入口，**零核心改动** | 接新系统 |
| `transfer_define` | 注册/更新转账模板（声明式 JSON：取数公式=科目×scope×ratio） | 定义摊销/计提 |
| `transfer_list` | 列出全部转账模板 | 看模板 |
| `transfer_run` | 执行转账模板：按取数公式生成 PUSHED 凭证（模拟计算待人审） | 跑模板 |
| `autonomy_authorize` | **签发 L3 自治授权令牌（O11 红线）**：仅人类 admin（`ledger:manage` 鉴权）可签发，代表「人授权该 Agent 在预算 budget、到期 expires_at（ISO8601 UTC）前可自执行过账」；返回 grant_id，`autonomy_post` 必须持此令牌。预算用尽自动跳闸冻结该 Agent 并通知人类，可随时吊销 | 开启自治前由 Boss 显式授权；Agent 绝不能自签 |
| `autonomy_post` | L3 自治过账：有效授权令牌 + 额度内 + 断路器闭合 → 直接 POSTED（系统规则执行，非 Agent 自审）；全部凭证进抽检池。无令牌/已吊销/过期/预算不足均拒；预算用尽自动跳闸冻结 | 授权后自治；仍受熔断与抽检约束 |
| `autonomy_audit_list` | 抽检池：全部 L3 自治过账凭证及抽检状态（pending/passed/reversed） | 人工抽检 |
| `autonomy_audit_review` | 抽检裁决（人工）：pass / reverse（生成红字冲销凭证并过账） | 抽检裁决 |
| `autonomy_replay` | 一键回放：按凭证聚合全部事件（创建/推送/审批/过账/AI 决策），审计轨迹不漏一行 | 审计回放 |
| `anomaly_scan` | 对单张凭证执行异常侦测（规则 + LLM 双通道） | 风险扫描 |
| `anomaly_release` | 人工解除 Agent 断路器（恢复自治）。**Agent 不能自解，需人类 admin** | 解除熔断 |
| `log_agent_decision` | AI 决策留痕：把 prompt 哈希（可选全文）、工具调用、输出摘要写入事件账本 | 决策审计 |
| `ocr_accuracy_report` | 字段级准确率抽检报告（DoD：抽检 ≥95%） | OCR 质量 |
| `consolidate_reports` | **多主体合并报表（v2.0）**：多账套 code 级聚合为集团合并资产负债表 + 利润表（全额合并 + 少数股权），**完全只读**；内部往来/交易抵消 `eliminations` 必须由 Boss 显式提供（HITL），AI 不臆测任何抵消金额。可选 ownership/fx_rates，channel=wecom 推卡片 | 集团层合并；只读聚合不写账 |
| `consolidation_workbook` | **合并工作底稿（可视化层，只读）**：在 `consolidate_reports` 之上把「逐主体贡献」与「内部抵消」并排成一张可审计底稿——每个资产负债表大类 / 利润表项目都给四列：各主体分项 + 主体小计 + 抵消调整 + 合并数（**抵消调整 = 主体小计 − 合并数**，由 consolidate 已应用结果反推，不另起抵消逻辑，单一真源）。抵消明细另含 code 级 PL00/PL20/合并数（consolidated_posting_levels）与已应用消除对（dr_code/cr_code/amount），供 Boss 审阅「这一行里有多少是抵消出来的」。Web 端 `/group/workbook` 复用同一内核。**完全只读、绝不写账** | 合并审计/汇报前置透明化 |
| `consolidate_lineage` | **合并血缘下钻（阶段0 / P0-1）**：给定合并资产负债表的一个科目 `code` 或大类 `group`，返回三层血缘「集团合并数 → 各主体分项 → 各主体源凭证」，每个数字都由 POSTED 凭证明细重建（Palantir 式端到端血缘）；完全只读 | 合并数对不上时追到哪张凭证 |
| `consolidate_posting_levels` | **合并分录层级标注（阶段0 / P0-2）**：把合并表每个科目拆成来源层 PL00 主体上报 / PL20 Boss 抵消，让「这一行有多少是抵消出来的」一目了然（SAP posting level 透明化）；完全只读 | 合并审计前置透明化 |
| `consolidate_propose_icp` | **内部往来自动配对草稿（阶段1 / Oracle ICP 精神）**：集团级净额配对——取各账套折算后应收（1122）与应付（2202）净额，可抵消额 = min(应收合计, 应付合计)，生成一笔集团级抵消建议 `draft_eliminations`，**完全只读、不写账**；凭证明细无「对手方账套」维度，故是集团级近似而非逐对手方配对，差额可能来自外部往来，必须 Boss 复核后喂回 `consolidate_reports` / `consolidate_posting_levels` 的 `eliminations` 确认执行 | 合并前算清「该抵消多少内部往来」，把手敲变确认 |
| `consolidate_propose_coi` | **长投-权益抵销草稿（阶段1 / SAP COI 精神）**：自动把母公司长期股权投资（1511）与子公司所有者权益（按 `category=='equity'` 识别）配比抵销，推算应享权益份额(持股×权益)、商誉(长投−应享)、少数股东权益，生成 `all_suggested_eliminations`，**完全只读、不写账**；商誉因消除机制只能「减余额」无法增记，改为在 `notes` 披露而非造分录；母公司识别优先 `parent_id`，否则取 `ownership==1.0` 唯一账套，缺失/多个（含全资子公司母子均 1.0）回退到「持有长投余额>0 的账套」。Boss 确认后原样喂回 `consolidate_reports` | 合并前算清「长投该抵消多少权益」，把手敲变确认 |
| `consolidate_cash_flow` | **合并现金流量表（阶段2 / 直接法）**：汇总各参与账套单体现金流量表（复用 `report_cash_flow` 同一口径），按报告币种（平均汇率）折算后加总，保持勾稽（合并期初现金 + 净增加 = 合并期末现金）；内部现金往来抵消 `eliminations` 必须由 Boss 显式提供（HITL，与合并 BS/IS 同哲学），**完全只读、绝不写账**。支持分层汇率 fx_rates（现金流折算用平均汇率） | 集团层合并第三张表；只读聚合不写账 |
| `consolidate_propose_cash_flow` | **合并现金流内部往来抵消草稿（阶段2）**：识别集团内权益性投资现金流镜像配对——母公司「投资支付的现金」(investing 流出) 与子公司「吸收投资收到的现金」(financing 流入) 在合并层面应当等额对冲，生成两笔建议抵消项（投资支付 / 吸收投资），**完全只读、不写账**；凭证明细无「对手方账套」维度，仅做集团级镜像配对，借款/股利类内部现金往来不在本建议内，必须 Boss 结合 ICP/COI 配对结果手工判断。建议抵消项可直接喂回 `consolidate_cash_flow` 的 `eliminations` | 合并前算清「内部现金往来该抵消多少」，把手敲变确认 |
| `arap_apply_clearing` | **记录核销（写动作，需 HITL 确认后调用）**：把回款与发票做单据级核销——新增**不可变** `arap_clearing` 记录，不改动任何凭证或余额投影；超额校验（OVER_CLEAR_INVOICE / OVER_APPLY_PAYMENT）防过度核销。典型闭环：`arap_propose_clearing`（只读草稿）→ 人工确认 → 本工具落库。assignments=[{invoice_line_id, payment_line_id, amount}]；source=manual/proposal/auto | 确认核销方案后落库；dim_key=customer/supplier，必须同往来单位 |

---

## 四、状态机（所有凭证动作的底层契约）

```
DRAFT ──push──▶ PUSHED ──approve──▶ APPROVED ──post──▶ POSTED
  ▲                  │  ▲                 │
  │  reject          │  │ withdraw         │ cancel_post
  └──────────────────┘  └─────────────────┘
```

- **NO_SELF_APPROVAL**：制单人与审批人不能相同（AI 只产草稿，终态须换人）。
- **审计不可篡改**：POSTED 凭证不直接改/删，只能红字冲销（cancel_post → 重制）。
- 任何写入动作都进入事件账本（不可篡改审计链）。

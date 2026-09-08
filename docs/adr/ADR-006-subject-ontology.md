# ADR-006: 科目语义本体（Ontology）底座

- 日期：2026-09-08
- 关联：ADR-004（科目是语义骨架）；「超级AI总账模块构想」阶段1；DESIGN.md 确定性内核铁律

## 背景

coa 模板六字段（code/name/direction/category/parent_code/aux_dims）是平面账表而非本体：
科目不知道自己「应收账款应挂客户辅助、参与账龄坏账、与预收账款重分类对冲」。
AI 引导因此无据可依——任何「聪明建议」都只能靠模型臆断。
顺序不能反：先固化确定性本体，再让 AI 基于本体引导（AI 只建议不动账）。

## 决策

1. **三真源、零双写**（对构想的演进：governed_by 列被砍掉）：

| 真源 | 承载 | 落库 |
|---|---|---|
| coa 模板 `attrs` 列 | 科目级声明属性（`k=v;k=v`：cash_flow/bad_debt/depreciate/amortize/deduction） | `accounts.attrs` JSON 列（migration 0005，幂等加列；重导入模板幂等回补旧种子） |
| `kernel/data/ontology/rules.csv` | 流程级治理规则（scope 精确编码或 `前缀*`；rule_type：aux_required/deduction_limit；severity：error/recommend） | 不落库（随准则 profile 全局一致，落库反而引入同步问题） |
| `kernel/data/ontology/relations.csv` | 跨科目语义关系（contra_of 备抵 / reclass_pairs 报表重分类对冲 / carryforward_from 结转对应） | 不落库 |

   砍掉 governed_by 列的理由：rules.csv 的 scope 已经声明「哪些科目受哪条规则管」，
   coa 行再写一遍 rule_id 是双真源，必然漂移。relates 同理不属于单行科目。

2. **唯一消费入口** `kernel/ontology.py`：load_rules / load_relations / template_attrs /
   subject_semantics（聚合 attrs+规则+关系，含出入方向）/ check_lines（确定性凭证行预检，
   只读不拦截，error/recommend 由调用方定夺）。禁止绕过该模块直读本体文件。

3. **确定性校验前置**：加载即校验值域（severity/rule_type/rel_type）、非空、以及
   scope/subj_code/obj_code 必须存在于 coa 模板（测试把守）——本体文件坏了当场报
   OntologyError，不允许静默降级。

4. **attrs 不继承**：属性按行声明（父科目与子科目各自写全），与 aux_dims 同一惯例，
   避免「按前缀推断继承」这类隐式规则。

## 后果

- 正面：AI 引导从此「有据可依」——subject_semantics 即 AI 提示词素材；
  check_lines 可挂制单预检/导入校验；报表重分类（预收预付/应收应付）有了确定性依据。
- 负面：新准则 profile 需要同时维护三处文件（接受：模板本就是人工治理物）；
  rules/relations 不在账套隔离内（接受：准则级语义对所有账套一致）。
- 迁移：老库需 `alembic upgrade head`（0005 幂等加列，SQLite/PG 通用）。

## 接线（阶段1第二步，2026-09-08）

- MCP 新工具 `subject_semantics`（归 minimal 档，计数 17/32/52）；
  `create_voucher` 响应自动附 `ontology_findings`（含幂等重放分支）——
  「AI 只建议不动账」：拦截留给状态机与硬校验，本体提示由 AI 复述给用户。
- Web 制单表单 HITL：POST 命中规则 → 原地回填 + 提示区（error=fail 红 /
  recommend=warn 黄）+「已知晓，继续创建」勾选确认后才落库；
  纯现金科目零命中时零摩擦直通。预检文件缺失时静默降级为空——
  本体是引导增强，不允许成为制单的单点故障。

## 报表消费：往来重分类列报（阶段1，2026-09-08）

本体 `reclass_pairs` 第一次进报表。口径是**按往来单位余额方向重分类**，
不是简单对冲抵消：

    1122 应收账款 按客户：借方→应收账款（资产）；贷方→预收款项（负债 2203）
    2203 预收账款 按客户：贷方→预收款项；借方→应收账款
    1123 预付账款 按供应商：借方→预付款项；贷方→应付账款
    2202 应付账款 按供应商：贷方→应付账款；借方→预付款项

三条硬约束（由测试钉死）：

1. **取数与资产负债表同源**（Balance 投影 × dims_key canonical 键）——
   重分类只在资产/负债两边之间搬金额，因此「资产=负债+所有者权益」仍成立。
   不加这条约束，重分类会变成又一个对不上的数。
2. **默认关闭**：`balance_sheet(apply_reclass=False)` 与 MCP/Web 开关默认关。
   列报口径变更必须显式选择，历史口径不因升级而漂移。
3. **未挂往来维度的余额保守留在原报表项目**，在 `reclass.untracked` 单列——
   宁可暴露脏数据，也不靠猜测搬动金额（同 partner_balances 纪律）。

反直觉但正确的一点：不重分类时反向余额以负数留在科目净额里，
会让资产与负债**同时虚减**；重分类后两边同时增加同一金额。

# ADR-005 权限模型：RBAC + 数据范围 + Agent 一等身份

- 状态：已接受（Accepted）
- 日期：2026-08-27
- 关联：DESIGN.md L0 审计与权限；手稿三要素（身份、数据权限、功能权限）；U8 三层权限模型继承

## 背景

权限必须先于功能（铁律 3）。传统 ERP 权限只面向人；XErp 中 Agent 是操作主体之一，必须以一等公民身份纳入同一权限体系，否则「谁授权 AI 做了什么」无法回答。

## 决策

1. **三元模型**（与手稿一一对应）：
   - **身份**：`subject = {type: user|agent, id}`；agent 与人同表管理（`subjects`），agent 额外携带 `autonomy_level(L0-L3)` 与 `autonomy_quota`（单笔上限/月累计）。
   - **功能权限**：`role → permission(resource, action)`，如 `accountant → voucher.approve`；**deny-by-default**。
   - **数据权限**：scope = `ledger_set` × `account` 前缀 × 辅助核算维度（部门/往来/项目），作用于所有 query/get 与写操作过滤。
2. **双重门禁**：内核服务层强制（唯一真相）+ MCP 层预检（快速失败给出友好错误）。MCP 层只是优化，绕过 MCP 直连内核同样被拦。
3. **关键制衡规则**：approve 权限与 create 身份互斥（ADR-004）；agent 的 autonomy 配置变更本身是事件（`agent.permission.changed`），可审计。
4. **实现策略**：P0 用 5 张表（subjects/roles/permissions/role_permissions/scope_rules）+ 简单检查器；不引入 Casbin。触发迁移条件：scope 规则 >50 条或出现继承需求。

## 后果

- 正面：审计链完整（每个事件有可追责 subject）；「员工离职撤销其授权过的 Agent」有明确操作路径。
- 负面：双重门禁有少量重复代码（接受，各 ~50 行）；P0 权限 UI 从简（配置文件 + SQL）。

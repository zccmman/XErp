# ADR-003 MCP 工具命名规范与数据契约

- 状态：已接受（Accepted）
- 日期：2026-08-27
- 关联：DESIGN.md L2 能力层；U8 启示（ufcomsql 共享组件库 → MCP 工具集；ZWSQL 通道 → Resources 受控查询）

## 背景

MCP 工具是 LLM 与内核之间的唯一通道。命名混乱或契约含糊会直接放大模型幻觉；金额类字段若用浮点将造成不可接受的账务误差。

## 决策

1. **命名**：`snake_case` 的 `动词_资源` 模式，动词限定白名单：`create / push / approve / post / cancel / get / list / query / close / export`。
   P0 七工具：`list_accounts`、`create_voucher`、`push_voucher`、`approve_voucher`、`post_voucher`、`get_voucher`、`query_balances`。
2. **金额契约**：一律 `string` 型十进制（如 `"1234.56"`），schema 中标注 `"format": "decimal-string"`；内核收到后转 `Decimal` 校验小数位 ≤2。**禁止 float 出现于任何 schema**。
3. **统一错误信封**：
   ```json
   {"ok": false, "error": {"code": "VOUCHER_UNBALANCED", "message_zh": "借贷不平衡：借 100.00 ≠ 贷 99.00", "details": {}}}
   ```
   `code` 大写蛇形常量；`message_zh` 面向用户可直接展示；details 结构化供 Agent 推理。
4. **幂等**：所有写操作接受客户端 `idempotency_key`（UUID），内核唯一约束防重。
5. **身份透传**：每次调用必须携带 `actor`（user/agent 身份），匿名调用一律拒绝——这是 ADR-005 审计的前置。

## 后果

- 正面：LLM 可靠调用（错误信息自解释）；金额零精度丢失；幂等使 Agent 重试安全。
- 负面：工具数增长后需按资源分组（P1 引入 tool namespace），P0 不预做。

# ADR-002 事件账本：append-only 表 + sha256 hash 链

- 状态：已接受（Accepted）
- 日期：2026-08-27
- 关联：DESIGN.md 铁律 2「审计即架构」；传统总账 逆向启示（gl_accvouch 事实与 gl_accsum 汇总分离 → 事件与投影分离）

## 背景

财务数据必须可审计、防篡改、可回放。传统 ERP 靠备份回滚（传统总账 UFBAK）补救；事件溯源（Event Sourcing）让「回放」成为第一能力而非补丁。

## 决策

1. **单一 `events` 表**作为唯一事实源， envelope 结构：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | BIGINT（序列） | 全局单调 |
| ledger_set_id | FK | 账套隔离 |
| event_type | text | 枚举：`voucher.created / voucher.pushed / voucher.approved / voucher.posted / voucher.cancelled / period.closed / …` |
| aggregate_id | text | 聚合根（如凭证号） |
| payload | JSONB | 事件体（金额为 string 型 Decimal） |
| actor | JSONB | `{type: user|agent, id, display_name}` |
| occurred_at | timestamptz | 业务时间 |
| prev_hash / hash | char(64) | `hash = sha256(prev_hash + canonical_json(除 hash 外全字段))`，账套内成链 |

2. **append-only 强制**：数据库触发器拒绝任何 UPDATE/DELETE（含表属主），P0-04 实现并有测试证明「篡改一条→verify_chain 失败」。
3. **余额是投影不是事实**：`balances` 物化表可随时由事件流全量重建；报表（P1-01）同样建在投影上。
4. **canonical_json 规范**：键排序（sort_keys）、无空白、UTF-8、金额 `str(Decimal)`、`occurred_at` 统一转 UTC 后去 tzinfo 取 isoformat（SQLite 往返会剥 tzinfo、PG 返回 aware——naive-UTC 是唯一跨库确定表示，P0-04 实测确立）——在 `kernel/ledger/canonical.py` 唯一实现。

## 后果

- 正面：审计=回放；「取消记账」=追加补偿事件而非改历史；hash 链使外审可独立验证。
- 负面：存储随事件线性增长（P2 前无需分区，预计单账套年事件 <10⁶）；查询需经投影（接受，投影重建即可）。

# XErp 全量验收回归报告（2026-09-01）

> 范围：ACCEPTANCE.md 12 项验收场景 A-L + Web/A2UI 界面 + 真机项状态梳理
> 执行方式：`scripts/acceptance_regression.py`（MCP 内存客户端全链路，数字级断言）
> 结论：**31/31 全绿 ✅**（引擎侧验收全部通过；真机双通道待凭据，见 §4）

---

## 1. 总体结果

| 项 | 场景 | 结果 | 关键断言 |
|---|---|---|---|
| setup | 建账 + 期间 + 审批员主体 | ✅ | 小企业会计准则 144 科目 |
| A | 期初导入 | ✅ 2/2 | 5 笔期初 POSTED 借=贷=122,000；不平衡期初硬拒 TRIAL_BALANCE_UNBALANCED（借 100≠贷 90） |
| B | 10 笔业务全流程 | ✅ 2/2 | 10/10 POSTED；制单=审批红线 NO_SELF_APPROVAL 生效 |
| C | 三表精确数字 | ✅ 4/4 | 利润表 19,000/5,800/**13,200**；资产负债表 负债 16,000 + 权益 123,200 = 资产 **139,200** 平衡；现金流经营净额 **8,200**；期末余额逐科目与手册 C1 核对表一致（8 行零差异） |
| F | 适配器 | ✅ 3/3 | 开票/回款自动入账 PUSHED、重放幂等、往来余额表在途口径 + untracked 诚实单列 |
| G | 发票 OCR | ✅ 4/4 | ingested / DUPLICATE_INVOICE 硬闸 / 勾稽不符 flagged / 准确率 100%≥95% |
| H | 银行对账 | ✅ 3/3 | 导入 3 条、重复导入 skipped=3、勾对 2 笔 + 未达账 TXN-U3 银行有账上无 |
| J | L3 自治 | ✅ 4/4 | 额度内直接 POSTED、QUOTA_EXCEEDED、人工抽检推翻→红字冲销、一键回放事件链完整（AUTONOMOUS_POSTED→REVIEWED→REVERSED） |
| I | 关账 | ✅ 2/2 | dry-run 列出未审凭证不动账；正式关账结转+试算通过 |
| K | 异常断路器 | ✅ 2/2 | 大额命中 large_amount → BREAKER_OPEN 冻结 Agent；人工解除 |
| L | 审计安全 | ✅ 3/3 | 审计链 verify_chain 无断裂、账账核对 18 张凭证零差异、SQL 直改被检出（issues=3） |

补充基线：pytest 全量 **193 passed / 0 failed**（本次回归前确认）。

## 2. Web/A2UI 界面验证（验收 E 项）✅

以 `python -m kernel.ui_server`（端口 8002，dev 库）启动后 curl 实测：

| 端点 | 结果 |
|---|---|
| `/`（首页） | 200 |
| `/init` | 200 |
| `/ui/`（A2UI 前端） | 200 |
| `/docs` | 200 |
| `/ui/` 页面静态资源引用（js/css） | 全部 200，无 orphan-404 |
| `/api/ledger/{id}/a2ui` 数据端点 | 200，返回完整 A2UI 协议 JSON |

## 3. 本轮发现并修复的问题

| # | 问题 | 根因 | 修复 | severity |
|---|---|---|---|---|
| 1 | **手册场景缺陷**：G 项发票日期 2026-08-28 早于业务期间，OCR 适配器按 `date_field: invoice_date` 落凭证到 8 月，账套未开 8 月期间直接报「账套不存在 2026-08 期间」 | 手册编写时未对齐业务期间；系统行为本身正确（防跨期漏开账） | 手册与脚本统一改为 2026-09-01（期间内且不触发未来日期闸门） | 高（阻碍验收） |
| 2 | **文档漂移**：附录 B 工具清单停在 36 个，实际 43 个 | 企微双通道 + 转账模板 + ledger_detail 五批工具上线后未同步 | 按内省结果补齐 7 个（`ledger_detail` `transfer_define/list/run` `wecom_send/send_approval/finish_card`） | 中 |
| 3 | 验收脚本断言口径 ×3 | C3 误用期初权益 122,000（应为含净利的 123,200）；J4 沿用旧事件名前缀；H2 误读 matched_count 层级（在 `report.summary`） | 全部按内核实际返回修正 | 低（脚本侧） |

> 备注：问题 1 反向验证了「未来日期→flagged」风控闸门与「跨期防护」真实生效——OCR 落账日期取发票日期是设计内行为。

## 4. 真机项状态（验收 D 项：双审批通道）

| 通道 | 代码/测试 | 真机联调 | 说明 |
|---|---|---|---|
| 飞书审批卡片 | ✅ 已落地（`mcp-server/xerp_mcp/feishu.py` tenant_access_token + `scripts/feishu_ws.py` WebSocket 长连接，无需公网回调） | ✅ 已闭环（P0-11，真机凭证 记-0002：飞书回复「同意」→ APPROVED→POSTED） | 凭据已在 `.env`（FEISHU_APP_ID/SECRET + 已绑定 open_id/chat_id）；交互方式为「回复消息 同意/驳回 凭证号」 |
| 企业微信审批 | ✅ 已落地（`kernel/wecom.py`：token 缓存/卡片推送/回调 AES 加解密；MCP `wecom_send_approval`/`wecom_send`/`wecom_finish_card`） | ✅ 已闭环（2026-09-01 真机：绑定→推卡→点「批准」→ APPROVED，actor=ZhengChengChen） | 凭据已在 `.env`；交互方式为「模板卡片按钮回调」；遗留：trycloudflare 隧道域名临时，正式化需固定域名 |

双通道均已真机端到端闭环；仅企微正式化待固定域名（F7）。

## 5. 复跑方式

```bash
python scripts/acceptance_regression.py
# 每次运行使用独立临时库，幂等可重复；输出 PASS/FAIL 明细 + 汇总
```

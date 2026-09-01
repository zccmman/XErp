# XErp × 企业微信：审核与交互端配置与验收（P4-W1）

企业微信自建应用作为 XErp HITL 审批的第二交互端，与飞书通道并存。指令内核同源
（`kernel/approval_bot.py`），状态机红线完全一致；企微侧额外支持**模板卡片按钮**直接批准/驳回。

## 1. 架构

```
企微自建应用
  ├── 主动推送（服务端 → 企微）：message/send（text/markdown/template_card）
  │     wecom_send_approval：待审凭证 → button_interaction 卡片（批准/驳回按钮）
  │     wecom_send：通用通知
  └── 回调（企微 → 服务端）：POST /wecom/callback
        文本指令   → kernel/approval_bot.handle_approval_command → 被动加密回复
        卡片按钮   → kernel/wecom.handle_card_event → 状态机 + template_card/update 更新卡片
```

- 回调加解密为与官方 `WXBizMsgCrypt` 等价的实现（sha1 签名 + AES-256-CBC + PKCS7(32) + corp_id 校验）。
- 企微回调**必须公网可达**（开发期可用内网穿透）。

## 2. 企微管理后台配置

1. 管理后台 → 应用管理 → 创建自建应用，记录 **AgentId** 与 **Secret**。
2. 应用详情 → 开发者接口 → 「接收消息」→ 设置回调：
   - URL：`https://<你的域名>/wecom/callback`（服务启动后填写）
   - Token / EncodingAESKey：随机生成，记入 `.env`。
   - 保存时企微会向 URL 发 GET 验证（`echostr` 解密回显），端点已实现。
3. 企业信息页记录 **企业 ID（CorpID）**。
4. 同页确认「企业可信 IP」包含服务端出口 IP（message/send 必需）。
5. 成员侧：用户在应用会话里发送「绑定」，其 userid 写入 `.env` 的 `WECOM_RECEIVE_USER`。

## 3. .env 配置样例（不入库）

```ini
WECOM_CORP_ID=wwxxxxxxxxxxxxxxxx
WECOM_CORP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
WECOM_AGENT_ID=1000002
WECOM_TOKEN=xxxxxxxxxxxxxxxxxxxxx
WECOM_ENCODING_AES_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # 43 位
# 以下由「绑定」指令自动写入，无需手填
WECOM_RECEIVE_USER=ZhangSan
```

## 4. 交互指令（与飞书同源）

| 指令 | 效果 | 审计 |
| --- | --- | --- |
| `绑定` | 当前企微账号设为审批接收人 | 写 `.env`（不入链） |
| `同意 <凭证号>` | PUSHED → APPROVED | 审批人身份入事件链 |
| `驳回 <凭证号> <意见>` | PUSHED → DRAFT | 意见入 `voucher.rejected` 事件 |
| `帮助` / `help` | 指令说明 | — |
| 卡片按钮「批准/驳回」 | 同上（驳回意见固定占位，可补文本指令） | 同上 |

红线不变：仅 PUSHED 可批/驳；制单≠审批（NO_SELF_APPROVAL）；Agent 审批禁止。

## 5. MCP 工具

| 工具 | 用途 |
| --- | --- |
| `wecom_send_approval(voucher_id, user?)` | 待审凭证 → 企微模板卡片（按钮回调写回状态机） |
| `wecom_send(content, msg_type, user?)` | text/markdown 主动通知 |

## 6. 验收步骤

### 6.1 离线（无需企微凭据）

```bash
python -m pytest tests/test_wecom.py -v
```

预期 18 passed：加解密往返 / 签名校验 / 被动回复信封 / 指令内核 / 卡片事件 / 回调端点 E2E。

### 6.2 联调（需配置 + 公网回调）

1. `python -m kernel.ui_server`（8002，回调挂在同一 app 的 `/wecom/callback`）。
2. 企微后台保存回调配置 → 验证通过（GET echostr）。
3. 应用会话发送 `绑定` → 收到「已绑定」回执，`.env` 出现 `WECOM_RECEIVE_USER`。
4. 经 MCP `create_voucher` + `push_voucher` 建一张 PUSHED 凭证 → 调 `wecom_send_approval`。
5. 企微收到卡片 → 点「批准」→ 卡片变为「已批准 ✅」，`ledger_detail` 确认可过账；
   或点「驳回」→ 卡片变为「已驳回」，凭证回 DRAFT，事件链含驳回记录。
6. 同凭证发文本指令复核幂等边界：再次驳回应回「仅待审（PUSHED）凭证可驳回」。

## 7. 已知边界

- 卡片按钮驳回暂无意见输入框，意见以占位语入链（企微 API 限制），随后可用文本指令补充。
- `template_card/update` 要求卡片 `card_type` 不变，完成后按钮降级为「已处理 ✅」noop 按钮。
- 同凭证重复推送 `wecom_send_approval` 复用同一 `task_id`（=voucher_id），旧卡自动被覆盖。

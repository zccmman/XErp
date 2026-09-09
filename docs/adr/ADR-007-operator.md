# ADR-007 · AI Runtime 具象：算子 · 账本精灵

> 决策日期：2026-09-09 · 基线：ledgeros `@52351ed` · 状态：**已接受**

## 背景

产品设计方案 §8 已确立「AI Runtime 具象化」决策（命名为 **算子 Operator**、视觉形象 **账本精灵**）。本 ADR 记录**第一版 Web 落地**的具体决策与红线约束。

## 决策

**D1** 新建 `kernel/operator.py`，**进程内单点真源**（模块级 `_state` 全局变量；Web 工作进程单线程天然安全）。
**D2** 6 态枚举 + 合法转移表（`OperatorState` × `_LEGAL_TRANSITIONS`）—— 非法转移抛 `IllegalOperatorTransition`，禁止业务方绕过。
**D3** 6 个 SVG 资产**内联**进 `kernel/operator.py` 常量，**不依赖 StaticFiles / 新路由**——最小爆炸半径。
**D4** `render_fragment()` 输出带 `op-container` 包装的 HTML 片段；`_page()` 加 `show_operator` 开关，**仅 M1 制单页开启**，其他页面零改动。
**D5** 偏好开关 `XERP_OPERATOR_HIDDEN=1` → 容器渲染空 fragment（不删 SVG 资产）。
**D6** 视觉规范：viewBox 24×24 · 描边 1.5px · 圆角 8-10px · 仅 4 色 token（黑/灰白/警示红/通过绿，与产品方案 §8.2 钉死）。

## 状态真源

| 状态 | 触发 | 来源 | 备注 |
|---|---|---|---|
| IDLE | 默认 / 任意活动结束 | webapp 渲染时 `current_state()` | 第一版仅展示，不联动 |
| LISTENING | 用户 focus 输入框 | （迭代 2 联动） | 当前不接 |
| DRAFTING | 草稿生成中 | （迭代 2 联动） | 当前不接 |
| PENDING | 凭证 PUSHED | （迭代 2 联动） | 当前不接 |
| ALERT | 异常扫描发现 | （迭代 2 联动） | 当前不接 |
| OFFLINE | LLM 不可用 | （迭代 2 联动） | 当前不接 |

**第一版仅暴露** `current_state()` / `set_state()` / `render_fragment()`，所有联动放迭代 2。

## 红线（破坏任意一条就回退设计）

| 红线 | 守则 | 落地检查 |
|---|---|---|
| **不能是装饰品** | 状态必须承载 AI Runtime 当前活动 | `data-state` 暴露给测试断言 |
| **不能喧宾夺主** | ≤ 24×24 常驻右上角，不参与主流程 | CSS `position:fixed; top:8px; right:16px`；不参与 POST 处理 |
| **不能加戏** | 无动画、无拟人对话、无心情表情 | 容器内仅 SVG + hover 详情卡 |
| **不能拟人化** | 不做"宠物/助手/吉祥物"——是**被调度**的执行者 | 命名 = **算子**（Operator）非"小助手/小精灵/小宠" |
| **不脱离极简色板** | 仅 4 色 token | SVG 内联常量硬编码 4 色，不读 CSS var（避免样式耦合） |

## 拒绝的反模式

- ❌ 把算子做成 modal 弹窗（违反"不喧宾夺主"）
- ❌ 让算子点头 = 落账（违反"不参与终态动作"）
- ❌ 算子有配音 / 拟声 / 心情表情（违反"不加戏"）
- ❌ 算子能换皮肤 / 改颜色（违反"不脱离极简"）
- ❌ 算子有「算子正在思考…」「算子理解你的意思了」气泡（违反"不加戏+不拟人化"）

## 后果

- **正面**：AI Runtime 从黑盒变成可见的进程角色；HaaB 隐喻从文案口号落地为视觉具象；后续每加一个状态联动都只是改 `set_state` 调用点
- **负面**：状态真源是进程内全局变量，**多进程部署时各进程独立持有**（无共享态）——接受：算子本就"本地协作伙伴"，跨进程同步反而违背产品定位
- **风险**：6 个 SVG 内联意味着改视觉需改 Python 源码（接受：低频变更，且可视化需要工程师参与评审）

## 验证

- 单元测试 10 项（`test_operator.py`）：六态枚举 / 默认值 / 合法转移 / 非法转移 / 6 态渲染 / 容器渲染 / 隐藏开关 / 离线只回 idle / alert 可回 idle / drafting→pending 合法
- 集成测试 4 项（`test_web_operator.py`）：M1 制单页含算子 / 其他页不含 / 关闭开关后容器为空 / set_state 后页面跟随
- 相关回归 124/124 全绿（test_web_voucher_form / test_web_guide / test_webapp / test_web_approval / test_web_classic / test_ontology / test_posting / test_tool_profiles）
- 全量逐文件回归（环境就绪后直跑复核）

## 后续迭代（不在本 ADR 范围）

- **迭代 2**：D3 联动（`create_voucher` 回调写 drafting→pending / 异常扫描回调写 alert / LLM ping 写 offline）+ i18n 资源注入
- **迭代 3**（C 完整版）：MCP `ai_runtime.state` 工具暴露 + IM 卡片显示算子状态
"""业务语言分录向导（S1 · 超级AI总账阶段 2）。

设计定位
--------
把「超级AI总账」的核心差异——*让不懂借贷的小微企业主也能记账*——落到确定
性内核上。用户用大白话描述一笔业务（"发工资""收了一笔现销""提了点备用金"），
向导给出**候选分录 + 每个科目为什么这么记 + 借贷是否平衡**，确认后才落库。

为什么接在 kernel/adapters 之上
-------------------------------
业务事件 → 凭证 的映射已经有完整的声明式引擎（DSL + 静态校验 + preview/ingest，
且 ingest 会把科目代码解析到账套真实 coa，缺科目直接报错，**绝不会造科目**）。
S1 不重复造轮子，只补两层：

1. **业务语言外壳**：场景目录（自然语言别名 → adapter/event_type）+ 逐行解释。
2. **预览富化**：把 preview 返回的科目代码解析成账套里的真实科目名，并附上
   "为什么记这个科目"的人话解释，让新手看得懂。

铁律（与全局一致）：AI/向导只建议、只预填，**最终落库权在人**；建议的分录必须
能落到本体真源，落不了的（如账套没配某科目）明确告知用户去补，而非直接写。

本模块全部为**只读**探活（propose / match）。真正写凭证走 `kernel.adapters.
ingest_event`（与第三方事件同源，保证"对话进得来、界面进得来"两套一致）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.adapters.engine import preview as _preview
from kernel.adapters.registry import get_rule
from kernel.db.models import Account

ZERO = Decimal("0.00")


class WizardError(ValueError):
    """向导输入不合法（金额/日期格式错、缺必填项等）。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


# ---------------------------------------------------------------------------
# 场景目录（业务语言 → 适配器规则 + 人话解释）
# ---------------------------------------------------------------------------
# 每个场景：
#   key        稳定 id
#   name       友好中文名（界面与匹配都用）
#   aliases    自然语言别名（用于"说业务"搜索框的关键词匹配）
#   adapter / event_type  命中的适配器规则
#   fields     用户要填的项：key/label/type/required/event_key/options
#   lines      逐行解释（与规则 lines 同序）：account 仅作信息展示，
#              role=借贷方向中文，why=为什么记这个科目
#   note       风险提示 / 会计分录讲究（需用户注意的会计口径）
SCENARIOS: list[dict] = [
    {
        "key": "cash_sale",
        "name": "现销收款（零售 / 无票）",
        "aliases": ["现销", "收现", "零售", "无票", "现金收款", "卖货收钱", "收到现金", "现金销售"],
        "adapter": "biz", "event_type": "cash.sale",
        "tags": ["收入", "资产"],
        "fields": [
            {"key": "amount", "label": "收款金额（元）", "type": "amount",
             "required": True, "placeholder": "如 1200.00", "event_key": "amount"},
            {"key": "sale_date", "label": "收款日期", "type": "date",
             "required": True, "event_key": "sale_date"},
            {"key": "customer", "label": "客户 / 备注（可选）", "type": "text",
             "required": False, "event_key": "customer", "placeholder": "留空亦可"},
        ],
        "lines": [
            {"account": "1001", "role": "借：库存现金",
             "why": "收到的现金进了出纳 / 保险柜"},
            {"account": "6001", "role": "贷：主营业务收入",
             "why": "实现了销售，计入营业收入（无票也须如实申报）"},
        ],
        "note": "小规模纳税人现销无票，仍须将款项如实计入主营业务收入并依法申报增值税。",
    },
    {
        "key": "cash_withdraw",
        "name": "提取备用金 / 取现",
        "aliases": ["提现", "取现", "备用金", "取备用金", "从银行取钱", "取钱"],
        "adapter": "biz", "event_type": "cash.withdraw",
        "tags": ["资产"],
        "fields": [
            {"key": "amount", "label": "取现金额（元）", "type": "amount",
             "required": True, "placeholder": "如 5000.00", "event_key": "amount"},
            {"key": "withdraw_date", "label": "取现日期", "type": "date",
             "required": True, "event_key": "withdraw_date"},
        ],
        "lines": [
            {"account": "1001", "role": "借：库存现金",
             "why": "从银行取出现金，库存现金增加"},
            {"account": "100201", "role": "贷：银行存款",
             "why": "银行账户余额相应减少"},
        ],
        "note": "提取备用金属于资金内部划转，不影响损益（借库存现金、贷银行存款）。",
    },
    {
        "key": "payroll_paid",
        "name": "发放工资",
        "aliases": ["发工资", "工资", "发薪", "发薪酬", "发薪水", "员工工资"],
        "adapter": "biz", "event_type": "payroll.paid",
        "tags": ["负债", "资产"],
        "fields": [
            {"key": "amount", "label": "发放金额（元）", "type": "amount",
             "required": True, "placeholder": "如 30000.00", "event_key": "amount"},
            {"key": "pay_date", "label": "发放日期", "type": "date",
             "required": True, "event_key": "pay_date"},
            {"key": "employee", "label": "发放对象（可选）", "type": "text",
             "required": False, "event_key": "employee", "placeholder": "全员可留空"},
        ],
        "lines": [
            {"account": "221101", "role": "借：应付职工薪酬-工资",
             "why": "冲减此前已计提的应付工资"},
            {"account": "100201", "role": "贷：银行存款",
             "why": "实际发放到员工账户"},
        ],
        "note": "本模板假定工资此前已计提；若未计提应先做计提分录。个税 / 社保代扣另计。",
    },
    {
        "key": "office_supply",
        "name": "采购办公用品",
        "aliases": ["办公用品", "买办公", "办公费", "买文具", "采购耗材", "办公采购"],
        "adapter": "biz", "event_type": "office.supply",
        "tags": ["费用", "资产"],
        "fields": [
            {"key": "amount", "label": "采购金额（元）", "type": "amount",
             "required": True, "placeholder": "如 800.00", "event_key": "amount"},
            {"key": "buy_date", "label": "采购日期", "type": "date",
             "required": True, "event_key": "buy_date"},
            {"key": "pay_via", "label": "支付方式", "type": "select",
             "required": True, "event_key": "pay_via", "default": "bank",
             "options": [("bank", "银行存款"), ("cash", "库存现金")]},
            {"key": "vendor", "label": "供应商 / 备注（可选）", "type": "text",
             "required": False, "event_key": "vendor", "placeholder": "留空亦可"},
        ],
        "lines": [
            {"account": "660202", "role": "借：管理费用-办公费",
             "why": "日常办公耗材支出，计入管理费用"},
            {"account": "100201", "role": "贷：银行存款 / 库存现金",
             "why": "支付办公用品的款项（按上方支付方式走银行或现金）"},
        ],
        "note": "大额办公设备应资本化计入固定资产并计提折旧，而非一次性费用化。",
    },
    {
        "key": "bank_interest_received",
        "name": "收到银行存款利息",
        "aliases": ["银行利息", "利息收入", "存款利息", "收利息", "利息到账"],
        "adapter": "biz", "event_type": "bank.interest.received",
        "tags": ["资产", "损益"],
        "fields": [
            {"key": "amount", "label": "利息金额（元）", "type": "amount",
             "required": True, "placeholder": "如 35.20", "event_key": "amount"},
            {"key": "receive_date", "label": "到账日期", "type": "date",
             "required": True, "event_key": "receive_date"},
        ],
        "lines": [
            {"account": "100201", "role": "借：银行存款",
             "why": "利息到账，银行存款增加"},
            {"account": "660302", "role": "贷：财务费用-利息收入",
             "why": "存款利息冲减财务费用（利润表以负数列示）"},
        ],
        "note": "利息收入在利润表作为财务费用的减项列示。",
    },
    {
        "key": "loan_interest_paid",
        "name": "支付借款利息",
        "aliases": ["借款利息", "付利息", "贷款利息", "利息支出", "还利息"],
        "adapter": "biz", "event_type": "loan.interest.paid",
        "tags": ["费用", "资产"],
        "fields": [
            {"key": "amount", "label": "利息金额（元）", "type": "amount",
             "required": True, "placeholder": "如 1200.00", "event_key": "amount"},
            {"key": "pay_date", "label": "支付日期", "type": "date",
             "required": True, "event_key": "pay_date"},
        ],
        "lines": [
            {"account": "660301", "role": "借：财务费用-利息支出",
             "why": "借款利息支出，计入财务费用"},
            {"account": "100201", "role": "贷：银行存款",
             "why": "利息实际付出"},
        ],
        "note": "资本化的借款利息不计入财务费用，应计入相关资产成本。",
    },
    {
        "key": "ar_received",
        "name": "收到客户回款",
        "aliases": ["收货款", "回款", "收到货款", "客户付款", "收到欠款", "收回应收"],
        "adapter": "ar", "event_type": "payment.received",
        "tags": ["资产"],
        "fields": [
            {"key": "amount", "label": "回款金额（元）", "type": "amount",
             "required": True, "placeholder": "如 50000.00", "event_key": "amount"},
            {"key": "received_at", "label": "收款日期", "type": "date",
             "required": True, "event_key": "received_at"},
            {"key": "customer", "label": "客户名称", "type": "text",
             "required": True, "event_key": "customer", "placeholder": "如 甲公司"},
        ],
        "lines": [
            {"account": "100201", "role": "借：银行存款",
             "why": "回款到账"},
            {"account": "1122", "role": "贷：应收账款",
             "why": "冲减对该客户的应收（按客户维度归集往来明细）"},
        ],
        "note": "收到的是此前已开票挂账的应收；若为先款后货请走「现销收款」。",
    },
    {
        "key": "ap_paid",
        "name": "支付供应商款项",
        "aliases": ["付供应商", "付款", "付货款", "支付应付账款", "还供应商", "付采购款"],
        "adapter": "ap", "event_type": "payment.made",
        "tags": ["负债", "资产"],
        "fields": [
            {"key": "amount", "label": "付款金额（元）", "type": "amount",
             "required": True, "placeholder": "如 20000.00", "event_key": "amount"},
            {"key": "paid_at", "label": "付款日期", "type": "date",
             "required": True, "event_key": "paid_at"},
            {"key": "supplier", "label": "供应商名称", "type": "text",
             "required": True, "event_key": "supplier", "placeholder": "如 乙工厂"},
        ],
        "lines": [
            {"account": "2202", "role": "借：应付账款",
             "why": "冲减对供应商的应付（按供应商维度归集）"},
            {"account": "100201", "role": "贷：银行存款",
             "why": "款项实际付出"},
        ],
        "note": "支付的是此前已入账的应付；若为先货后款请走「采购发票入账」。",
    },
    {
        "key": "ar_invoiced",
        "name": "开具销售发票（确认收入）",
        "aliases": ["开票", "销售开票", "开销售票", "确认收入", "开增值税发票", "开收入票"],
        "adapter": "ar", "event_type": "invoice.issued",
        "tags": ["资产", "收入", "负债"],
        "fields": [
            {"key": "invoice_no", "label": "发票号码", "type": "text",
             "required": True, "event_key": "invoice_no", "placeholder": "如 XD2026-001"},
            {"key": "total_amount", "label": "价税合计（元）", "type": "amount",
             "required": True, "placeholder": "如 11300.00", "event_key": "total_amount"},
            {"key": "net_amount", "label": "不含税金额（元）", "type": "amount",
             "required": True, "placeholder": "如 10000.00", "event_key": "net_amount"},
            {"key": "tax_amount", "label": "税额（元）", "type": "amount",
             "required": True, "placeholder": "如 1300.00", "event_key": "tax_amount"},
            {"key": "customer", "label": "客户名称", "type": "text",
             "required": True, "event_key": "customer", "placeholder": "如 甲公司"},
            {"key": "issued_at", "label": "开票日期", "type": "date",
             "required": True, "event_key": "issued_at"},
        ],
        "lines": [
            {"account": "1122", "role": "借：应收账款",
             "why": "确认对客户的应收（按客户维度归集）"},
            {"account": "6001", "role": "贷：主营业务收入",
             "why": "不含税金额计入营业收入"},
            {"account": "222101", "role": "贷：应交税费-应交增值税",
             "why": "销项税额暂挂应交增值税，申报时清缴"},
        ],
        "note": "价税合计 = 不含税金额 + 税额；三者须勾稽相等，否则借贷不平。",
    },
    {
        "key": "expense_claimed",
        "name": "员工费用报销",
        "aliases": ["报销", "员工报销", "费用报销", "差旅报销", "招待费报销", "垫款报销"],
        "adapter": "ap", "event_type": "expense.claimed",
        "tags": ["费用", "资产"],
        "fields": [
            {"key": "amount", "label": "报销金额（元）", "type": "amount",
             "required": True, "placeholder": "如 1500.00", "event_key": "amount"},
            {"key": "claimed_at", "label": "报销日期", "type": "date",
             "required": True, "event_key": "claimed_at"},
            {"key": "category", "label": "费用类别（备注，可选）", "type": "text",
             "required": False, "event_key": "category", "placeholder": "如 差旅 / 招待"},
        ],
        "lines": [
            {"account": "660204", "role": "借：管理费用-业务招待费",
             "why": "员工垫付的费用报销（本模板统一计入业务招待费 660204）"},
            {"account": "100201", "role": "贷：银行存款",
             "why": "报销款支付给员工"},
        ],
        "note": "本模板统一计入业务招待费；若需区分差旅 / 办公，请改用「采购发票入账」或制单页自选科目。",
    },
    {
        "key": "purchase_invoiced",
        "name": "采购发票入账（确认应付）",
        "aliases": ["采购发票", "收发票", "进项发票", "采购入账", "收到发票", "供应商发票"],
        "adapter": "ocr", "event_type": "invoice.received",
        "tags": ["费用", "负债"],
        "fields": [
            {"key": "invoice_no", "label": "发票号码", "type": "text",
             "required": True, "event_key": "invoice_no", "placeholder": "如 XD2026-002"},
            {"key": "expense_category", "label": "费用类别", "type": "select",
             "required": True, "event_key": "expense_category", "default": "办公费",
             "options": [("办公费", "办公费"), ("差旅费", "差旅费"), ("业务招待费", "业务招待费")]},
            {"key": "total_amount", "label": "发票金额（元）", "type": "amount",
             "required": True, "placeholder": "如 5650.00", "event_key": "total_amount"},
            {"key": "supplier", "label": "供应商名称", "type": "text",
             "required": True, "event_key": "supplier", "placeholder": "如 乙工厂"},
            {"key": "invoice_date", "label": "发票日期", "type": "date",
             "required": True, "event_key": "invoice_date"},
        ],
        "lines": [
            {"account": "660202", "role": "借：管理费用（按类别）",
             "why": "采购支出计入对应管理费用明细（办公费 660202 / 差旅费 660203 / 业务招待费 660204）"},
            {"account": "2202", "role": "贷：应付账款",
             "why": "确认对供应商的应付（按供应商维度归集）"},
        ],
        "note": "进项税本模板未单列（默认含税计入费用）；如需价税分离请走定制分录。",
    },
]


def get_scenario(key: str) -> dict | None:
    for sc in SCENARIOS:
        if sc["key"] == key:
            return sc
    return None


def list_scenarios() -> list[dict]:
    """返回精简后的场景目录（不含解释明细，供 UI / MCP 列出）。"""
    out = []
    for sc in SCENARIOS:
        out.append({
            "key": sc["key"],
            "name": sc["name"],
            "tags": sc.get("tags", []),
            "aliases": sc.get("aliases", []),
            "fields": [
                {
                    "key": f["key"],
                    "label": f["label"],
                    "type": f["type"],
                    "required": f.get("required", False),
                    "options": [list(o) for o in f.get("options", [])] or None,
                }
                for f in sc["fields"]
            ],
        })
    return out


def match_scenarios(text: str, limit: int = 6) -> list[dict]:
    """自然语言匹配场景：按别名 / 名称子串命中打分，返回排名前 limit。

    空文本返回空列表（调用方应展示完整目录）。确定性：同输入同输出。
    """
    q = (text or "").strip()
    if not q:
        return []
    scored: list[tuple[int, dict]] = []
    for sc in SCENARIOS:
        score = 0
        for alias in sc.get("aliases", []):
            if alias and alias in q:
                score += len(alias)  # 越长越具体的别名权重越高
        if sc["name"] in q:
            score += 5
        if score > 0:
            scored.append((score, sc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"key": sc["key"], "name": sc["name"], "score": score}
        for score, sc in scored[:limit]
    ]


def _coerce_field(field: dict, raw: str) -> str:
    """把表单原始字符串规格化为事件字段值，非法即抛 WizardError。"""
    ftype = field["type"]
    val = (raw or "").strip()
    if ftype == "amount":
        if not val:
            raise WizardError("AMOUNT_REQUIRED", f"{field['label']} 不能为空")
        try:
            d = Decimal(val)
        except (InvalidOperation, ValueError, TypeError):
            raise WizardError("AMOUNT_INVALID", f"{field['label']} 不是合法金额：{val!r}")
        if d <= ZERO:
            raise WizardError("AMOUNT_POSITIVE", f"{field['label']} 必须是正数")
        return val
    if ftype == "date":
        if not val:
            raise WizardError("DATE_REQUIRED", f"{field['label']} 不能为空")
        try:
            date.fromisoformat(val)
        except ValueError:
            raise WizardError("DATE_INVALID", f"{field['label']} 应为 YYYY-MM-DD：{val!r}")
        return val
    # text / select：可选项为空允许；有 options 时校验取值合法
    if ftype == "select":
        opts = [o[0] for o in field.get("options", [])]
        if opts and val and val not in opts:
            raise WizardError("OPTION_INVALID", f"{field['label']} 取值非法：{val!r}")
        return val or field.get("default", "")
    return val


def build_event(scenario: dict, form: dict) -> dict:
    """把用户表单映射到适配器事件字典（供 propose 预览与 confirm 落库共用）。

    与 :func:`propose` 共用同一套字段规格——预览看到什么，落库就写什么，
    不会出现「预览一套、执行另一套」的漂移。
    """
    event: dict = {}
    for f in scenario["fields"]:
        if f.get("required") or (f.get("key") in form and (form.get(f["key"]) or "").strip()):
            event[f["event_key"]] = _coerce_field(f, form.get(f["key"], ""))
        elif f["type"] == "select":
            # 可选下拉保留默认值（如 pay_via=bank）
            event[f["event_key"]] = f.get("default", "")
    return event


def propose(session: Session, ledger_set_id: str, key: str, form: dict) -> dict:
    """只读预览：给定场景 + 用户表单，返回候选分录 + 科目真名 + 逐行解释。

    不落库。返回结构：
        {scenario_key, scenario_name, summary, lines[], debit, credit,
         balanced, missing_accounts[], note}
    lines[] 每项：line_no / side / account / account_name / amount /
                  role / why / missing
    """
    sc = get_scenario(key)
    if sc is None:
        raise WizardError("SCENARIO_NOT_FOUND", f"未知业务场景：{key}")
    rule = get_rule(sc["adapter"], sc["event_type"])
    if rule is None:
        raise WizardError(
            "RULE_NOT_FOUND", f"场景 {key} 缺少底层适配器规则",
            {"adapter": sc["adapter"], "event_type": sc["event_type"]},
        )

    event = build_event(sc, form)
    pv = _preview(rule, event)

    codes = [ln["account"] for ln in pv["lines"]]
    name_map = {
        a.code: a.name
        for a in session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id,
                Account.code.in_(codes),
            )
        ).all()
    }
    missing = sorted(c for c in codes if c not in name_map)

    enriched = []
    for i, ln in enumerate(pv["lines"]):
        sc_line = sc["lines"][i] if i < len(sc["lines"]) else {}
        code = ln["account"]
        enriched.append({
            "line_no": ln["line_no"],
            "side": ln["side"],
            "account": code,
            "account_name": name_map.get(code, "（账套未配置）"),
            "amount": ln["amount"],
            "role": sc_line.get("role", ln["side"]),
            "why": sc_line.get("why", ""),
            "missing": code not in name_map,
        })

    return {
        "scenario_key": key,
        "scenario_name": sc["name"],
        "summary": pv["summary"],
        "lines": enriched,
        "debit": pv["debit"],
        "credit": pv["credit"],
        "balanced": pv["balanced"],
        "missing_accounts": missing,
        "note": sc.get("note", ""),
    }

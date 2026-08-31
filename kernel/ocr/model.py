"""发票结构化模型与字段级校验（P2-03）。

定位：OCR/视觉模型只负责「看图说话」，内核负责**判定这张发票能不能入账**——
价税勾稽、税率合理性、日期与发票号格式、查重。任何提取器（人工/LLM/PaddleOCR）
产出的都必须过同一套校验，入账质量与提取器解耦。

字段级准确率（DoD：抽检 ≥95%）由 :func:`compare_fields` 度量：
对人工标注的真值逐字段比对，产出可运营的抽检报告，而不是一句"很准"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

# 增值税发票号：8 位（数电票 20 位也接受）
INVOICE_NO_RE = re.compile(r"^\d{8}(\d{12})?$")
# 纳税人识别号：15/18/20 位数字与大写字母（不含 I O Z S V）
TAX_ID_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{15}(?:[0-9A-HJ-NPQRTUWXY]{3,5})?$")

# 常见税率（小规模 1%/3%，一般纳税人 6%/9%/13%）
KNOWN_RATES = {Decimal("0.01"), Decimal("0.03"), Decimal("0.06"),
               Decimal("0.09"), Decimal("0.13")}

# 参与准确率比对的字段与权重（金额字段权重高——错金额比错摘要严重）
SCORED_FIELDS: dict[str, float] = {
    "invoice_no": 2.0,
    "invoice_date": 1.5,
    "seller_name": 1.0,
    "seller_tax_id": 1.0,
    "total_amount": 3.0,
    "net_amount": 2.0,
    "tax_amount": 2.0,
    "expense_category": 1.0,
}


@dataclass
class InvoiceData:
    """从发票图片提取出的结构化数据。"""

    invoice_no: str
    invoice_date: str                     # ISO 日期
    seller_name: str = ""
    seller_tax_id: str = ""
    total_amount: str = "0.00"            # 价税合计
    net_amount: str = "0.00"              # 不含税金额
    tax_amount: str = "0.00"              # 税额
    expense_category: str = "办公费"       # 费用归类（决定科目映射）
    confidence: dict[str, float] = field(default_factory=dict)  # 字段级置信度

    def to_event(self) -> dict:
        """转成适配器事件格式（customer→supplier、amount 等字段名对齐规则）。"""
        return {
            "event_id": f"INV-{self.invoice_no}",
            "invoice_no": self.invoice_no,
            "supplier": self.seller_name,
            "invoice_date": self.invoice_date,
            "total_amount": self.total_amount,
            "net_amount": self.net_amount,
            "tax_amount": self.tax_amount,
            "expense_category": self.expense_category,
        }


def _dec(value: Any, where: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{where} 不是合法金额：{value!r}") from exc
    if not d.is_finite() or d < ZERO:
        raise ValueError(f"{where} 必须是非负有限金额：{value!r}")
    return d


def validate_invoice(inv: InvoiceData) -> list[str]:
    """字段级校验，返回问题列表（空 = 通过）。金额均分到分再比较。"""
    problems: list[str] = []

    if not INVOICE_NO_RE.match(inv.invoice_no or ""):
        problems.append(f"发票号格式非法：{inv.invoice_no!r}（应为 8 位或 20 位数字）")

    try:
        d = date.fromisoformat(str(inv.invoice_date)[:10])
    except (ValueError, TypeError):
        problems.append(f"发票日期非法：{inv.invoice_date!r}")
    else:
        if d > date.today():
            problems.append(f"发票日期在未来：{d}")

    if inv.seller_tax_id and not TAX_ID_RE.match(inv.seller_tax_id):
        problems.append(f"销方纳税人识别号格式可疑：{inv.seller_tax_id!r}")

    try:
        total = _dec(inv.total_amount, "价税合计").quantize(CENT)
        net = _dec(inv.net_amount, "不含税金额").quantize(CENT)
        tax = _dec(inv.tax_amount, "税额").quantize(CENT)
    except ValueError as e:
        problems.append(str(e))
        return problems

    # 价税勾稽：合计 = 净额 + 税额（±0.02 容差，兼容四舍五入）
    if abs(total - (net + tax)) > Decimal("0.02"):
        problems.append(
            f"价税勾稽不符：合计 {total} ≠ 净额 {net} + 税额 {tax}"
        )

    # 税率合理性：税额/净额 应接近常见税率（±1% 容差；净额为 0 跳过）
    if net > ZERO:
        rate = (tax / net).quantize(Decimal("0.0001"))
        if not any(abs(rate - r) <= Decimal("0.01") for r in KNOWN_RATES):
            problems.append(f"隐含税率 {rate} 不在常见税率范围")

    return problems


def low_confidence_fields(inv: InvoiceData, threshold: float = 0.85) -> list[str]:
    """置信度低于阈值的字段——这些字段需要人工复核后才能自动入账。"""
    return sorted(
        f for f, c in inv.confidence.items() if c < threshold
    )


def compare_fields(
    extracted: dict[str, str], ground_truth: dict[str, str]
) -> dict[str, Any]:
    """字段级准确率（DoD 抽检机制）。

    对每个参与比对的字段判断提取值是否与真值一致（金额字段允许 ±0.01 容差），
    按权重加权得出正确率。返回逐字段明细与总分，可直接出抽检报告。
    """
    details: list[dict[str, Any]] = []
    score = weight_sum = 0.0
    for name, weight in SCORED_FIELDS.items():
        if name not in ground_truth:
            continue  # 真值未标注的字段不参与
        got = str(extracted.get(name, ""))
        want = str(ground_truth[name])
        if name in ("total_amount", "net_amount", "tax_amount"):
            try:
                ok = abs(Decimal(got) - Decimal(want)) <= Decimal("0.01")
            except InvalidOperation:
                ok = False
        else:
            ok = got.strip() == want.strip()
        score += weight if ok else 0.0
        weight_sum += weight
        details.append({"field": name, "extracted": got, "expected": want,
                        "match": ok, "weight": weight})
    accuracy = score / weight_sum if weight_sum else 0.0
    return {
        "accuracy": round(accuracy, 4),
        "pass_threshold_95": accuracy >= 0.95,
        "fields": details,
        "sample_size": len(details),
    }

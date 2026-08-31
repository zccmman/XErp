"""事件适配器规则 DSL（P2-01）。

设计原则
--------
1. **声明式**：第三方业务事件 → 凭证模板，用 JSON 描述，不写代码。
2. **无 eval**：金额只用受限算子（取字段 / 常量 / 四则），不解析任意表达式，
   规则文件可来自外部配置而不引入代码执行风险。
3. **可静态校验**：规则在注册时即完整校验，坏规则不允许进入注册表。

规则结构
--------
{
  "adapter": "ar",
  "event_type": "invoice.issued",
  "version": "v1",
  "target_status": "PUSHED",
  "summary": "销售开票 {invoice_no}",
  "date_field": "issued_at",
  "lines": [
    {"side": "debit",  "account": "1122", "amount": {"from": "total_amount"}},
    {"side": "credit", "account": "6001", "amount": {"from": "net_amount"}},
    {"side": "credit", "account": "2221", "amount": {"from": "tax_amount"}}
  ]
}

金额规格（amount）
-----------------
- ``{"from": "field.path"}``          取事件字段（支持 ``a.b.c`` 路径）
- ``{"const": "100.00"}``             常量
- ``{"ratio": 0.01, "of": <spec>}``   比例（语法糖，等价于 mul）
- ``{"op": "mul"|"div"|"add"|"sub", "left": <spec>, "right": <spec>}``

字段路径只允许 ``[A-Za-z0-9_.]``，且禁止以 ``_`` 开头——防止属性逃逸到对象内部。
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

_FIELD_RE = re.compile(r"^[A-Za-z0-9_](\.?[A-Za-z0-9_]+)*$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)\}")
_OPS = ("mul", "div", "add", "sub")
_SIDES = ("debit", "credit")
_TARGET_STATUS = ("DRAFT", "PUSHED")


class RuleError(ValueError):
    """规则非法（结构/字段/算子）。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


class EventFieldError(ValueError):
    """事件数据不满足规则要求（缺字段/类型错）。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _is_valid_path(path: str) -> bool:
    return bool(_FIELD_RE.match(path)) and not path.startswith("_")


def get_field(event: dict, path: str) -> Any:
    """按 ``a.b.c`` 路径取事件字段；路径非法或缺失直接报错。"""
    if not _is_valid_path(path):
        raise EventFieldError("BAD_FIELD_PATH", f"非法字段路径：{path}")
    cur: Any = event
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise EventFieldError(
                "FIELD_MISSING", f"事件缺少字段：{path}", {"path": path}
            )
        cur = cur[part]
    return cur


def _to_decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise EventFieldError("BAD_AMOUNT", f"{where} 不是合法金额：{value!r}")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise EventFieldError("BAD_AMOUNT", f"{where} 不是合法金额：{value!r}") from exc
    if not d.is_finite():
        raise EventFieldError("BAD_AMOUNT", f"{where} 不是有限数值：{value!r}")
    return d


def resolve_amount(spec: Any, event: dict) -> Decimal:
    """按金额规格求值为 Decimal（两位小数，四舍五入）。

    只支持受限算子，不解析任意表达式。
    """
    if isinstance(spec, dict):
        if "from" in spec:
            return _to_decimal(get_field(event, spec["from"]), spec["from"])
        if "const" in spec:
            return _to_decimal(spec["const"], "const")
        if "ratio" in spec:
            base = (
                resolve_amount(spec.get("of"), event)
                if "of" in spec
                else ZERO
            )
            return (base * _to_decimal(spec["ratio"], "ratio")).quantize(
                CENT, rounding=ROUND_HALF_UP
            )
        if "op" in spec:
            op = spec["op"]
            if op not in _OPS:
                raise RuleError("BAD_OP", f"不支持的算子：{op}", {"op": op})
            left = resolve_amount(spec.get("left"), event)
            right = resolve_amount(spec.get("right"), event)
            if op == "mul":
                return (left * right).quantize(CENT, rounding=ROUND_HALF_UP)
            if op == "div":
                if right == ZERO:
                    raise EventFieldError("DIV_BY_ZERO", "金额计算出现除以零")
                return (left / right).quantize(CENT, rounding=ROUND_HALF_UP)
            if op == "add":
                return (left + right).quantize(CENT, rounding=ROUND_HALF_UP)
            return (left - right).quantize(CENT, rounding=ROUND_HALF_UP)
        raise RuleError("BAD_AMOUNT_SPEC", f"无法识别的金额规格：{spec}")
    return _to_decimal(spec, "amount")


def render_summary(template: str, event: dict) -> str:
    """用事件字段渲染摘要模板，``{field}`` 取值，缺失原样保留（不因摘要丢事件）。"""

    def sub(m: re.Match[str]) -> str:
        try:
            return str(get_field(event, m.group(1)))
        except EventFieldError:
            return m.group(0)

    return _PLACEHOLDER_RE.sub(sub, template)


def validate_rule(rule: dict) -> None:
    """规则静态校验：结构完整性 + 借贷平衡（结构层面）+ 算子合法性。

    借贷是否相等依赖运行时金额，这里只校验两条及以上分录且借贷两侧都存在。
    """
    if not isinstance(rule, dict):
        raise RuleError("BAD_RULE", "规则必须是对象")
    for key in ("adapter", "event_type", "version", "date_field", "lines"):
        if not rule.get(key):
            raise RuleError("RULE_FIELD_MISSING", f"规则缺少必填字段：{key}")

    target = rule.get("target_status", "PUSHED")
    if target not in _TARGET_STATUS:
        raise RuleError(
            "BAD_TARGET_STATUS",
            f"target_status 必须是 {'/'.join(_TARGET_STATUS)}，收到 {target}",
        )

    lines = rule["lines"]
    if not isinstance(lines, list) or len(lines) < 2:
        raise RuleError("TOO_FEW_LINES", "规则至少需 2 条分录")

    sides = set()
    for idx, ln in enumerate(lines, start=1):
        if not isinstance(ln, dict):
            raise RuleError("BAD_LINE", f"第 {idx} 条分录必须是对象")
        side = ln.get("side")
        if side not in _SIDES:
            raise RuleError(
                "BAD_SIDE", f"第 {idx} 条分录 side 必须是 debit/credit，收到 {side}"
            )
        sides.add(side)
        account_spec = _validate_account_spec(ln, idx)
        if "amount" not in ln:
            raise RuleError("BAD_AMOUNT_SPEC", f"第 {idx} 条分录缺少 amount 规格")
        _validate_amount_spec(ln["amount"], idx)
        partner = ln.get("partner")
        if partner is not None:
            _validate_partner(partner, idx)
        if account_spec:  # 动态科目：映射表值必须合法
            for code in account_spec.values():
                if not re.match(r"^\d{4,6}$", str(code)):
                    raise RuleError(
                        "BAD_ACCOUNT", f"第 {idx} 条分录科目映射值非法：{code}"
                    )

    if sides != set(_SIDES):
        raise RuleError("ONE_SIDED", "规则必须同时包含借方与贷方分录")

    summary = rule.get("summary", "")
    if not isinstance(summary, str):
        raise RuleError("BAD_SUMMARY", "summary 必须是字符串")


def _validate_account_spec(line: dict, line_idx: int) -> dict | None:
    """科目规格：静态 ``account`` 或动态 ``account_from`` + ``account_map``。

    动态映射用于「发票归类决定科目」类场景：按事件字段值查映射表取科目，
    未命中走 ``default_account``。
    """
    where = f"第 {line_idx} 条分录"
    if line.get("account"):
        return None
    src = line.get("account_from")
    mapping = line.get("account_map")
    if src and isinstance(mapping, dict) and mapping:
        if not _is_valid_path(src):
            raise RuleError("BAD_FIELD_PATH", f"{where} 非法科目来源路径：{src}")
        return mapping
    raise RuleError(
        "BAD_ACCOUNT",
        f"{where} 缺少科目：需 account 或 account_from+account_map",
    )


def _validate_partner(partner: Any, line_idx: int) -> None:
    """往来辅助维度规格：``{"dim": "customer", "from": "payer"}``。

    挂上 partner 的分录会把 ``{"dim": 取值}`` 写入 aux_dims，
    余额投影天然按（科目 × 往来单位）聚合——往来明细不建子科目。
    """
    where = f"第 {line_idx} 条分录 partner"
    if not isinstance(partner, dict):
        raise RuleError("BAD_PARTNER", f"{where} 必须是对象")
    dim = partner.get("dim")
    if not isinstance(dim, str) or not _is_valid_path(dim):
        raise RuleError("BAD_PARTNER_DIM", f"{where} 维度名非法：{dim!r}")
    src = partner.get("from")
    if not src or not _is_valid_path(src):
        raise RuleError("BAD_PARTNER_FIELD", f"{where} 缺少合法的 from 字段路径")


def _validate_amount_spec(spec: Any, line_idx: int) -> None:
    """递归校验金额规格结构（不依赖事件数据，可在注册期执行）。"""
    where = f"第 {line_idx} 条分录 amount"
    if isinstance(spec, (int, str, float)):
        try:
            _to_decimal(spec, where)
        except EventFieldError as e:
            # 注册期发现的问题一律是「规则非法」，不是「事件数据问题」
            raise RuleError("BAD_AMOUNT_SPEC", f"{where} 不是合法金额字面量：{spec!r}") from e
        return
    if not isinstance(spec, dict):
        raise RuleError("BAD_AMOUNT_SPEC", f"{where} 必须是对象或字面量")

    if "from" in spec:
        if not _is_valid_path(spec["from"]):
            raise RuleError("BAD_FIELD_PATH", f"{where} 非法字段路径：{spec['from']}")
        return
    if "const" in spec:
        try:
            _to_decimal(spec["const"], f"{where}.const")
        except EventFieldError as e:
            raise RuleError("BAD_AMOUNT_SPEC", f"{where}.const 不是合法金额") from e
        return
    if "ratio" in spec:
        _to_decimal(spec["ratio"], f"{where}.ratio")
        if "of" in spec:
            _validate_amount_spec(spec["of"], line_idx)
        return
    if "op" in spec:
        if spec["op"] not in _OPS:
            raise RuleError("BAD_OP", f"{where} 不支持的算子：{spec['op']}")
        _validate_amount_spec(spec.get("left"), line_idx)
        _validate_amount_spec(spec.get("right"), line_idx)
        return
    raise RuleError("BAD_AMOUNT_SPEC", f"{where} 无法识别")

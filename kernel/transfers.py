"""转账模板引擎（复盘 D3）：把「期末自动转账」从硬编码泛化为声明式模板。

对应能力：成熟总账的「自定义转账」——月结时不只损益结转，还有
费用分摊、计提、结转各类常规模拟凭证。Agent 原生的变革点：
模板就是声明式 JSON，AI 可以按自然语言直接生成并注册。

模板结构
--------
{
  "name": "计提坏账准备",
  "period_type": "monthly",
  "lines": [
    {"side": "debit",  "account": "6701",
     "amount": {"source": "balance", "account": "1122",
                "scope": "balance", "ratio": 0.05}},
    {"side": "credit", "account": "4102",
     "amount": {"source": "balance", "account": "1122",
                "scope": "balance", "ratio": 0.05}}
  ],
  "balance_check": true
}

取数公式 amount 规格（受限，无 eval）：
- {"source": "balance", "account": "1122", "scope": "balance|debit|credit",
   "ratio": 0.05, "fixed": "0"}   —— 取科目余额投影（期初至当期累计）
     scope: balance=净额 / debit=借方发生 / credit=贷方发生；ratio 乘系数
- {"const": "100.00"}              —— 固定金额

与 close_period 的关系：损益结转（P1-02）是内置规则；本模块是**用户自定义**
转账的通用引擎。生成凭证一律 PUSHED 待人审——转账是模拟计算，人拍板才落账。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kernel.db.models import Account, Period, Voucher, VoucherLine
from kernel.events import E
from kernel.ledger import append_event

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

_TPL_DIR = Path(__file__).resolve().parent / "data" / "transfers"

_TEMPLATES: dict[str, dict] = {}


class TransferError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


# ---------- 模板定义与校验 ----------


@dataclass
class TransferTemplate:
    name: str
    lines: list[dict]
    period_type: str = "monthly"       # monthly | yearly
    balance_check: bool = True
    description: str = ""
    tags: list[str] = field(default_factory=list)


def validate_template(tpl: dict) -> None:
    """模板静态校验：结构 + 取数公式 + 借贷两侧存在。"""
    if not isinstance(tpl, dict) or not tpl.get("name"):
        raise TransferError("BAD_TEMPLATE", "模板必须是含 name 的对象")
    lines = tpl.get("lines")
    if not isinstance(lines, list) or len(lines) < 2:
        raise TransferError("TOO_FEW_LINES", "转账模板至少 2 条分录")
    sides = set()
    for idx, ln in enumerate(lines, start=1):
        side = ln.get("side")
        if side not in ("debit", "credit"):
            raise TransferError(
                "BAD_SIDE", f"第 {idx} 条分录 side 必须是 debit/credit"
            )
        sides.add(side)
        if not ln.get("account"):
            raise TransferError("BAD_ACCOUNT", f"第 {idx} 条分录缺少科目")
        amt = ln.get("amount")
        if not isinstance(amt, dict):
            raise TransferError("BAD_AMOUNT", f"第 {idx} 条分录缺少 amount 公式")
        if "const" in amt:
            continue
        if amt.get("source") != "balance":
            raise TransferError(
                "BAD_SOURCE", f"第 {idx} 条 amount.source 仅支持 balance"
            )
        if not amt.get("account"):
            raise TransferError(
                "BAD_SOURCE_ACCOUNT", f"第 {idx} 条取数科目缺失"
            )
        if amt.get("scope") not in ("balance", "debit", "credit", None):
            raise TransferError("BAD_SCOPE", f"第 {idx} 条 scope 非法")
        ratio = amt.get("ratio", 1)
        if not isinstance(ratio, (int, float)) or ratio < 0:
            raise TransferError("BAD_RATIO", f"第 {idx} 条 ratio 必须是非负数")
    if sides != {"debit", "credit"}:
        raise TransferError("ONE_SIDED", "模板必须同时包含借方与贷方分录")


def register_template(tpl: dict) -> TransferTemplate:
    validate_template(tpl)
    _TEMPLATES[tpl["name"]] = tpl
    return TransferTemplate(
        name=tpl["name"], lines=tpl["lines"],
        period_type=tpl.get("period_type", "monthly"),
        balance_check=tpl.get("balance_check", True),
        description=tpl.get("description", ""),
        tags=tpl.get("tags", []),
    )


def load_builtin_templates() -> int:
    """加载内置模板 JSON（用户自定义模板以 runtime register 为主）。"""
    if not _TPL_DIR.exists():
        return len(_TEMPLATES)
    for path in sorted(_TPL_DIR.glob("*.json")):
        tpl = json.loads(path.read_text(encoding="utf-8"))
        _TEMPLATES.setdefault(tpl["name"], tpl)
    return len(_TEMPLATES)


def list_templates() -> list[dict]:
    load_builtin_templates()
    return [
        {"name": t["name"], "period_type": t.get("period_type", "monthly"),
         "description": t.get("description", ""),
         "lines": len(t["lines"])}
        for t in _TEMPLATES.values()
    ]


# ---------- 取数与执行 ----------


def _resolve_amount(
    session: Session, ledger_set_id: str, spec: dict,
    accounts: dict[str, Account], year: int, month: int,
) -> Decimal:
    """取数公式求值：余额投影（净额/借方/贷方）× 系数，或固定值。"""
    if "const" in spec:
        return Decimal(str(spec["const"])).quantize(CENT, ROUND_HALF_UP)
    code = spec["account"]
    acc = accounts.get(code)
    if acc is None:
        raise TransferError(
            "ACCOUNT_NOT_FOUND", f"取数科目 {code} 在账套中不存在",
            {"account": code},
        )
    scope = spec.get("scope", "balance")
    total_dr = total_cr = ZERO
    periods = session.scalars(
        select(Period).where(Period.ledger_set_id == ledger_set_id)
    ).all()
    # 取当期投影（含期初+当期累计）；不能取"第一个<=目标"的期间——
    # 那会命中最早的期间（如 seed 的 8 月）导致取数恒零（单测抓出）
    target = max(
        (p for p in periods if (p.year, p.month) <= (year, month)),
        key=lambda p: (p.year, p.month), default=None,
    )
    if target is None:
        raise TransferError("PERIOD_NOT_FOUND", f"账套无 {year}-{month:02d} 及之前的期间")
    # 简化口径：取目标期间投影（余额 scope=balance 时即累计至该期的净额投影，
    # 因为每月投影在关账后按净额保留——与明细账期初口径一致）
    from kernel.db.models import Balance

    for b in session.scalars(
        select(Balance).where(
            Balance.ledger_set_id == ledger_set_id,
            Balance.period_id == target.id,
            Balance.account_id == acc.id,
        )
    ):
        total_dr += Decimal(str(b.debit_total))
        total_cr += Decimal(str(b.credit_total))
    net = (total_dr - total_cr) if acc.direction == "debit" else (total_cr - total_dr)
    value = {"balance": net, "debit": total_dr, "credit": total_cr}[scope]
    ratio = Decimal(str(spec.get("ratio", 1)))
    return (value * ratio).quantize(CENT, ROUND_HALF_UP)


def run_template(
    session: Session,
    *,
    ledger_set_id: str,
    template_name: str,
    year: int,
    month: int,
    actor: dict,
    voucher_date=None,
) -> dict:
    """执行转账模板：生成 PUSHED 凭证（模拟计算待人审，绝不自动过账）。

    幂等：同期间同名模板已生成（事件查重）→ ALREADY_RUN。
    """
    load_builtin_templates()
    tpl = _TEMPLATES.get(template_name)
    if tpl is None:
        raise TransferError(
            "TEMPLATE_NOT_FOUND", f"转账模板未注册：{template_name}",
            {"known": sorted(_TEMPLATES)},
        )

    from datetime import date as _date

    v_date = voucher_date or _date(year, month, 28)
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year, Period.month == month,
        )
    ).first()
    if period is None:
        raise TransferError("PERIOD_NOT_FOUND", f"账套不存在 {year}-{month:02d} 期间")

    # 幂等：查转账凭证事件
    marker = f"transfer:{template_name}:{year}{month:02d}"
    from kernel.db.models import Event

    if session.scalars(
        select(Event).where(
            Event.event_type == E.VOUCHER_CREATED.value,
            Event.aggregate_id.like(marker + "%"),
        )
    ).first() is not None:
        raise TransferError(
            "ALREADY_RUN",
            f"模板 {template_name} 在 {year}-{month:02d} 已执行过",
        )

    accounts = {
        a.code: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        ).all()
    }
    orm_lines: list[VoucherLine] = []
    total_dr = total_cr = ZERO
    for idx, spec in enumerate(tpl["lines"], start=1):
        code = spec["account"]
        acc = accounts.get(code)
        if acc is None:
            raise TransferError(
                "ACCOUNT_NOT_FOUND", f"分录科目 {code} 不存在", {"account": code}
            )
        if not acc.is_leaf:
            raise TransferError(
                "ACCOUNT_NOT_LEAF", f"科目 {code} 不是最明细科目", {"account": code}
            )
        amount = _resolve_amount(session, ledger_set_id, spec["amount"],
                                 accounts, year, month)
        is_debit = spec["side"] == "debit"
        if is_debit:
            total_dr += amount
        else:
            total_cr += amount
        orm_lines.append(VoucherLine(
            line_no=idx, account_id=acc.id,
            debit=amount if is_debit else ZERO,
            credit=ZERO if is_debit else amount,
        ))

    if tpl.get("balance_check", True) and total_dr != total_cr:
        raise TransferError(
            "TEMPLATE_UNBALANCED",
            f"模板生成结果不平衡：借 {total_dr:,.2f} ≠ 贷 {total_cr:,.2f}"
            "（请检查取数公式与系数）",
            {"debit": str(total_dr), "credit": str(total_cr)},
        )
    if total_dr == ZERO:
        raise TransferError("NOTHING_TO_TRANSFER", "模板取数结果为全零，无需转账")

    existing = session.scalars(
        select(Voucher.voucher_no).where(Voucher.ledger_set_id == ledger_set_id)
    ).all()
    seq = max((int(no[2:]) for no in existing
               if no.startswith("记-") and no[2:].isdigit()), default=0)
    voucher = Voucher(
        ledger_set_id=ledger_set_id, period_id=period.id,
        voucher_no=f"记-{seq + 1:04d}", voucher_date=v_date,
        status="PUSHED", summary=f"[转账:{template_name}] {year}-{month:02d}",
        created_by=str(actor.get("id") or ""), lines=orm_lines,
    )
    # 幂等：idempotency_key unique 约束（与 create_voucher 同模式）
    voucher.idempotency_key = marker
    session.add(voucher)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise TransferError(
            "ALREADY_RUN",
            f"模板 {template_name} 在 {year}-{month:02d} 已执行过",
        ) from None

    append_event(
        session, ledger_set_id=ledger_set_id,
        event_type=E.VOUCHER_PUSHED.value, aggregate_id=marker,
        payload={"template": template_name, "voucher_no": voucher.voucher_no,
                 "total": str(total_dr)},
        actor=actor,
    )
    session.flush()
    return {
        "voucher": {"id": voucher.id, "voucher_no": voucher.voucher_no,
                    "status": voucher.status, "summary": voucher.summary},
        "template": template_name,
        "debit": f"{total_dr:,.2f}", "credit": f"{total_cr:,.2f}",
    }

"""预算编制与预算 v.s. 实际对比（ERP 模块纵深 · 计划/控制）。

设计铁律（ADR-002 单一真源）：
- 预算只是经营计划的量化表达，**绝不**写凭证、**绝不**进余额投影；
- 预算 v.s. 实际对比的「实际数」一律来自 ``amounts_by_code``（与三表、合并报表
  同一入口），不另起取数逻辑，杜绝口径漂移。
- 预算净额与 ``ending_balance`` 同符号约定（资产/费用借方为正、负债/权益/收入贷方
  为正），因此差异可直接相减，不发生符号错配。

控制强度（预警 or 硬拦截）由调用方决定——内核只负责编制与对比，不下发强制约束。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Budget, BudgetLine, LedgerSet
from kernel.reporting.statements import amounts_by_code, ending_balance

ZERO = Decimal("0.00")


class BudgetError(ValueError):
    """预算相关错误：code / message_zh / details 可直接被 MCP 层消费。"""

    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


# ---------------------------------------------------------- 编制（写，DRAFT）


def _validate_lines(lines: list[dict]) -> list[dict]:
    """校验预算明细行结构；返回规整后的行（period 归一、amount 转 Decimal）。

    每行：{account_code:str, period:int(0=年度/1..12=月), amount:Decimal|str|num, note?:str}
    """
    if not isinstance(lines, list) or not lines:
        raise BudgetError("EMPTY_LINES", "预算至少包含一行明细")
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            raise BudgetError("LINE_NOT_OBJECT", f"第 {i + 1} 行不是对象")
        code = str(ln.get("account_code") or "").strip()
        if not code:
            raise BudgetError("CODE_REQUIRED", f"第 {i + 1} 行缺少 account_code")
        period = int(ln.get("period", 0) or 0)
        if period < 0 or period > 12:
            raise BudgetError(
                "PERIOD_RANGE", f"第 {i + 1} 行 period 必须在 0（年度）~12 之间"
            )
        try:
            amount = Decimal(str(ln.get("amount", 0) or 0))
        except Exception as e:  # noqa: BLE001
            raise BudgetError(
                "AMOUNT_INVALID", f"第 {i + 1} 行 amount 非法：{ln.get('amount')}"
            ) from e
        if (code, period) in seen:
            raise BudgetError(
                "DUP_LINE", f"科目 {code} 期间 {period} 重复"
            )
        seen.add((code, period))
        out.append({
            "account_code": code,
            "period": period,
            "amount": amount,
            "note": (str(ln.get("note") or "").strip() or None),
        })
    return out


def _next_version(session: Session, ledger_set_id: str, fiscal_year: int) -> int:
    existing = session.scalars(
        select(Budget.version).where(
            Budget.ledger_set_id == ledger_set_id,
            Budget.fiscal_year == fiscal_year,
        )
    ).all()
    return (max(existing) if existing else 0) + 1


def create_budget(
    session: Session,
    *,
    ledger_set_id: str,
    name: str,
    fiscal_year: int,
    lines: list[dict],
    status: str = "DRAFT",
    created_by: str = "",
    note: str | None = None,
) -> dict:
    """编制一套预算（DRAFT），返回 {budget_id, version, lines_count}。

    幂等键 (ledger_set_id, fiscal_year, version) 由内核自增 version 保证不冲突；
    同名/同年修订请走 copy_budget 升版本。
    """
    ls = session.get(LedgerSet, ledger_set_id)
    if ls is None:
        raise BudgetError("LEDGER_NOT_FOUND", f"账套 {ledger_set_id} 不存在")
    name = (name or "").strip() or f"{fiscal_year} 年度预算"
    version = _next_version(session, ledger_set_id, fiscal_year)
    clean = _validate_lines(lines)

    budget = Budget(
        ledger_set_id=ledger_set_id, name=name, fiscal_year=fiscal_year,
        version=version, status=status, note=note, created_by=created_by,
    )
    session.add(budget)
    session.flush()
    for ln in clean:
        session.add(BudgetLine(
            budget_id=budget.id, account_code=ln["account_code"],
            period=ln["period"], amount=ln["amount"], note=ln["note"],
        ))
    session.flush()
    return {
        "budget_id": budget.id,
        "ledger_set_id": ledger_set_id,
        "name": name,
        "fiscal_year": fiscal_year,
        "version": version,
        "status": status,
        "lines_count": len(clean),
    }


def copy_budget(
    session: Session,
    budget_id: str,
    *,
    new_name: str | None = None,
    new_fiscal_year: int | None = None,
    new_version: int | None = None,
) -> dict:
    """克隆一套预算为新版本（DRAFT），用于滚动修订。返回新预算摘要。"""
    src = session.get(Budget, budget_id)
    if src is None:
        raise BudgetError("BUDGET_NOT_FOUND", f"预算 {budget_id} 不存在")
    fy = new_fiscal_year or src.fiscal_year
    version = new_version or _next_version(session, src.ledger_set_id, fy)
    src_lines = session.scalars(
        select(BudgetLine).where(BudgetLine.budget_id == src.id)
    ).all()
    budget = Budget(
        ledger_set_id=src.ledger_set_id,
        name=new_name or f"{src.name}（修订 v{version}）",
        fiscal_year=fy, version=version, status="DRAFT", note=src.note,
        created_by=src.created_by,
    )
    session.add(budget)
    session.flush()
    for ln in src_lines:
        session.add(BudgetLine(
            budget_id=budget.id, account_code=ln.account_code,
            period=ln.period, amount=ln.amount, note=ln.note,
        ))
    session.flush()
    return {
        "budget_id": budget.id, "ledger_set_id": budget.ledger_set_id,
        "name": budget.name, "fiscal_year": fy, "version": version,
        "status": "DRAFT", "lines_count": len(src_lines),
        "copied_from": budget_id,
    }


def activate_budget(session: Session, budget_id: str) -> dict:
    """将某版本置为 ACTIVE（生效对比基准），同 (账套, 会计年度) 其余版本回退 SUPERSEDED。"""
    budget = session.get(Budget, budget_id)
    if budget is None:
        raise BudgetError("BUDGET_NOT_FOUND", f"预算 {budget_id} 不存在")
    # 让同组其余版本失效
    others = session.scalars(
        select(Budget).where(
            Budget.ledger_set_id == budget.ledger_set_id,
            Budget.fiscal_year == budget.fiscal_year,
            Budget.id != budget.id,
        )
    ).all()
    for o in others:
        if o.status == "ACTIVE":
            o.status = "SUPERSEDED"
    budget.status = "ACTIVE"
    session.flush()
    return {
        "budget_id": budget.id, "fiscal_year": budget.fiscal_year,
        "version": budget.version, "status": "ACTIVE",
        "superseded": [o.id for o in others if o.status == "SUPERSEDED"],
    }


# ---------------------------------------------------------- 查询（读）


def list_budgets(session: Session, ledger_set_id: str) -> list[dict]:
    """列出账套全部预算（按 会计年度、版本倒序）。"""
    rows = session.scalars(
        select(Budget).where(Budget.ledger_set_id == ledger_set_id)
        .order_by(Budget.fiscal_year.desc(), Budget.version.desc())
    ).all()
    return [
        {
            "budget_id": b.id, "name": b.name, "fiscal_year": b.fiscal_year,
            "version": b.version, "status": b.status, "note": b.note,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in rows
    ]


def get_budget(session: Session, budget_id: str) -> dict:
    """取一套预算（表头 + 明细行，按 code、period 排序）。"""
    b = session.get(Budget, budget_id)
    if b is None:
        raise BudgetError("BUDGET_NOT_FOUND", f"预算 {budget_id} 不存在")
    lines = session.scalars(
        select(BudgetLine).where(BudgetLine.budget_id == b.id)
        .order_by(BudgetLine.account_code, BudgetLine.period)
    ).all()
    return {
        "budget_id": b.id, "ledger_set_id": b.ledger_set_id, "name": b.name,
        "fiscal_year": b.fiscal_year, "version": b.version, "status": b.status,
        "note": b.note,
        "lines": [
            {
                "account_code": ln.account_code, "period": ln.period,
                "amount": str(ln.amount), "note": ln.note,
            }
            for ln in lines
        ],
    }


def get_active_budget(
    session: Session, ledger_set_id: str, fiscal_year: int
) -> dict | None:
    """取账套某会计年度的 ACTIVE 预算（无则 None）。"""
    b = session.scalars(
        select(Budget).where(
            Budget.ledger_set_id == ledger_set_id,
            Budget.fiscal_year == fiscal_year,
            Budget.status == "ACTIVE",
        ).order_by(Budget.version.desc())
    ).first()
    return get_budget(session, b.id) if b else None


# ---------------------------------------------------------- 预算 v.s. 实际（读，对比）


def budget_vs_actual(
    session: Session,
    *,
    ledger_set_id: str,
    fiscal_year: int,
    period_month: int,
    budget_id: str | None = None,
) -> dict:
    """预算 v.s. 实际对比（只读，单一真源）。

    - 预算来源：budget_id 指定，否则取该 (账套, 会计年度) 的 ACTIVE 预算；无预算返回 empty。
    - 实际来源：``amounts_by_code`` 的期间净额（与三表同口径，ADR-002）。
    - 预算口径：明细行 period==period_month 直接取额；period==0（年度总额）按月均摊
      （amount/12）作为当月预算份额，并在 note 标注「年度均摊」。
    - 差异 = 实际 − 预算（同符号约定，可直接相减）；variance_pct = 差异/预算。
      **正差异 = 实际高于预算**（费用类为超支、收入类为超额完成），由调用方按科目性质解读。

    返回 rows（按 account_code 排序）+ totals + flags（over_budget_positive 仅作中性标记）。
    """
    if period_month < 1 or period_month > 12:
        raise BudgetError("MONTH_RANGE", "period_month 必须在 1~12 之间")

    budget = (
        get_budget(session, budget_id)
        if budget_id
        else get_active_budget(session, ledger_set_id, fiscal_year)
    )
    if budget is None:
        return {
            "ok": True, "has_budget": False, "ledger_set_id": ledger_set_id,
            "fiscal_year": fiscal_year, "period_month": period_month,
            "budget_id": None, "rows": [], "totals": _zero_totals(),
            "note": "该账套该年度无 ACTIVE 预算（或指定 budget_id 不存在）",
        }

    actuals = amounts_by_code(session, ledger_set_id, fiscal_year, period_month)

    rows: list[dict] = []
    total_budget = ZERO
    total_actual = ZERO
    for ln in budget["lines"]:
        code = ln["account_code"]
        amt = Decimal(ln["amount"])
        if ln["period"] == period_month:
            budget_month = amt
            src = "月度"
        elif ln["period"] == 0:
            budget_month = (amt / 12).quantize(Decimal("0.01"))
            src = "年度均摊"
        else:
            continue  # 非本月的月度行不计入当月对比
        dr, cr = actuals.get(code, (ZERO, ZERO))
        actual = ending_balance(code, dr, cr)
        variance = (actual - budget_month).quantize(Decimal("0.01"))
        pct = (
            (variance / budget_month * 100).quantize(Decimal("0.1"))
            if budget_month != ZERO else Decimal("0.0")
        )
        rows.append({
            "account_code": code,
            "budget": str(budget_month),
            "actual": str(actual),
            "variance": str(variance),
            "variance_pct": str(pct),
            "source": src,
            "note": ln.get("note"),
        })
        total_budget += budget_month
        total_actual += actual

    tv = (total_actual - total_budget).quantize(Decimal("0.01"))
    return {
        "ok": True,
        "has_budget": True,
        "ledger_set_id": ledger_set_id,
        "fiscal_year": fiscal_year,
        "period_month": period_month,
        "budget_id": budget["budget_id"],
        "budget_name": budget["name"],
        "budget_version": budget["version"],
        "rows": rows,
        "totals": {
            "budget": str(total_budget.quantize(Decimal("0.01"))),
            "actual": str(total_actual.quantize(Decimal("0.01"))),
            "variance": str(tv),
        },
        "note": "正差异 = 实际高于预算（费用类为超支，收入类为超额完成）",
    }


def _zero_totals() -> dict:
    return {"budget": "0.00", "actual": "0.00", "variance": "0.00"}

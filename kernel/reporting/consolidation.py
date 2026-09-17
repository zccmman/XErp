"""多主体合并报表（v2.0）：集团层只读聚合。

设计铁律（对齐 ADR-002 / O18 推送≠执行）：

- **完全只读**：只复用 ``balance_sheet`` / ``income_statement`` 的 code 级取数
  （``amounts_by_code``）与同一套标准 mapping，绝不写凭证 / 过账 / 结账。
- **合并在科目 code 级别聚合**，再复用同一套标准 mapping 重映射成合并报表。
  这保证「合并表一个数、单主体另一个数」这类最伤信任的不一致不会出现
  （ADR-002：投影/列报必须可由凭证流重建，口径单一真源）。
- **全额合并 + 少数股权**：各子公司资产/负债/收入/费用按 100% 并入；
  持股 <100% 的部分计入「少数股东权益 / 少数股东损益」并单独列示。
- **内部往来 / 内部交易抵消（eliminations）必须由 Boss 显式提供**（HITL），
  本模块不臆测任何抵消金额——守住「人是 Boss，任何过账/结账需你确认」。

合并方法：全额合并（full consolidation）。
``ledger_set_ids`` 应包含母公司自身账套 + 各子公司账套；母公司的
「长期股权投资」与子公司的权益通过抵消项配对归零。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import LedgerSet
from kernel.reporting import mapping as M
from kernel.reporting.statements import (
    ReportError,
    amounts_by_code,
    balance_sheet,
    ending_balance,
    income_statement,
)

ZERO = Decimal("0")


class ConsolidationError(ReportError):
    """合并报表专属错误（继承 ReportError，便于调用方统一捕获）。"""


# ---------------------------------------------------------- 取数与聚合


def _ledger_sets(session: Session, ledger_set_ids: list[str]) -> list[LedgerSet]:
    ls_map = {
        ls.id: ls
        for ls in session.scalars(
            select(LedgerSet).where(LedgerSet.id.in_(ledger_set_ids))
        ).all()
    }
    missing = [i for i in ledger_set_ids if i not in ls_map]
    if missing:
        raise ConsolidationError(f"账套不存在：{', '.join(missing)}")
    return [ls_map[i] for i in ledger_set_ids]


def _aggregate_amounts(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    fx_rates: dict[str, Decimal] | None = None,
) -> tuple[dict[str, list[Decimal]], str]:
    """把各账套同 code 的借/贷发生额直接相加（按汇率换算到报告币种后）。

    返回 (agg, reporting_currency)。agg: code → [借方发生额, 贷方发生额]。
    """
    fx_rates = fx_rates or {}
    currencies: set[str] = set()
    agg: dict[str, list[Decimal]] = {}
    for ls_id in ledger_set_ids:
        ls = session.get(LedgerSet, ls_id)
        currencies.add(ls.functional_currency or "CNY")
        rate = Decimal(str(fx_rates.get(ls_id, "1")))
        amts = amounts_by_code(session, ls_id, year, month)
        for code, (dr, cr) in amts.items():
            d, c = agg.get(code, [ZERO, ZERO])
            agg[code] = [d + dr * rate, c + cr * rate]
    # 报告币种：全部一致则取其值；否则以首个账套币种为基准，要求 fx_rates 补齐
    if len(currencies) == 1:
        reporting_ccy = next(iter(currencies))
    else:
        reporting_ccy = (session.get(LedgerSet, ledger_set_ids[0]).functional_currency
                         or "CNY")
        for ls_id in ledger_set_ids:
            if ls_id not in fx_rates:
                raise ConsolidationError(
                    f"存在多币种账套（{', '.join(sorted(currencies))}），"
                    f"请为除基准币种外的账套提供 fx_rates（本位币/{reporting_ccy} 每 1 单位）"
                )
    return agg, reporting_ccy


def _apply_eliminations(
    agg: dict[str, list[Decimal]],
    eliminations: list[dict] | None,
) -> list[dict]:
    """应用抵消项（Boss 显式提供）。

    每个抵消项是 ``{dr_code, cr_code, amount}``：把 dr_code 的借方发生额、
    cr_code 的贷方发生额各减 amount（配对归零内部往来/长投与子公司权益）。
    该操作保持表内平衡（双方同减同等金额）。返回归一化后的抵消明细供回显。
    """
    applied: list[dict] = []
    for i, e in enumerate(eliminations or []):
        if not isinstance(e, dict) or "dr_code" not in e or "cr_code" not in e \
                or "amount" not in e:
            raise ConsolidationError(
                f"抵消项#{i} 必须含 dr_code / cr_code / amount 三键"
            )
        try:
            amt = Decimal(str(e["amount"]))
        except Exception as exc:  # noqa: BLE001
            raise ConsolidationError(f"抵消项#{i} 金额非法：{exc}") from exc
        if amt < ZERO:
            raise ConsolidationError(f"抵消项#{i} 金额必须非负")
        dr_code = str(e["dr_code"])
        cr_code = str(e["cr_code"])
        d, c = agg.get(dr_code, [ZERO, ZERO])
        agg[dr_code] = [d - amt, c]
        d2, c2 = agg.get(cr_code, [ZERO, ZERO])
        agg[cr_code] = [d2, c2 - amt]
        applied.append({"dr_code": dr_code, "cr_code": cr_code, "amount": str(amt)})
    return applied


# ---------------------------------------------------------- 重映射（复用单主体口径）


def _build_bs(mp: dict, agg: dict[str, list[Decimal]]):
    """与 ``statements.balance_sheet`` 同源的 BS 重映射：按 code → 大类 → 项目。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for code, (dr, cr) in sorted(agg.items()):
        pos = M.balance_sheet_group(mp, code)
        if pos is None:
            continue
        bal = ending_balance(code, dr, cr)
        if bal == ZERO:
            continue
        groups.setdefault(pos, []).append({"code": code, "ending": bal})

    def build(major: str):
        items, total = [], ZERO
        for (m, g), rows in sorted(groups.items(), key=lambda kv: kv[0]):
            if m != major:
                continue
            sub = sum((r["ending"] for r in rows), ZERO)
            items.append({"group": g, "amount": sub, "accounts": rows})
            total += sub
        return items, total

    assets, total_assets = build("资产")
    liabs, total_liabs = build("负债")
    equity, total_equity = build("所有者权益")
    return (assets, total_assets, liabs, total_liabs, equity, total_equity)


def _build_is(mp: dict, agg: dict[str, list[Decimal]]):
    """与 ``statements.income_statement`` 同源的 IS 重映射：净额口径。"""
    items, revenue, expense = [], ZERO, ZERO
    for name, _prefixes, side in mp["income_statement"]:
        total = ZERO
        for code, (dr, cr) in agg.items():
            hit = M.income_statement_item(mp, code)
            if hit is None or hit[0] != name:
                continue
            # 净额口径：费用=借-贷，收入=贷-借（与单主体一致，避免红字冲减漏算）
            total += (cr - dr) if side == "credit" else (dr - cr)
        items.append({"item": name, "amount": total, "side": side})
        if side == "credit":
            revenue += total
        else:
            expense += total
    net = revenue - expense
    return items, revenue, expense, net


def _group_closed(session: Session, ledger_set_ids: list[str], year: int,
                  month: int) -> bool:
    """集团期是否全部结账：所有参与账套当期均存在「结转-」凭证才算集团已结账。"""
    from kernel.db.models import Voucher

    for ls_id in ledger_set_ids:
        exists = session.scalars(
            select(Voucher.id).where(
                Voucher.ledger_set_id == ls_id,
                Voucher.voucher_no.like(f"结转-{year}{month:02d}-%"),
            )
        ).first()
        if exists is None:
            return False
    return True


def _normalize_ownership(ledger_set_ids: list[str],
                         ownership: dict[str, object] | None
                         ) -> dict[str, Decimal]:
    own = {i: Decimal("1") for i in ledger_set_ids}
    for k, v in (ownership or {}).items():
        try:
            ratio = Decimal(str(v))
        except Exception as exc:  # noqa: BLE001
            raise ConsolidationError(f"ownership[{k}] 非法：{exc}") from exc
        if not (ZERO <= ratio <= Decimal("1")):
            raise ConsolidationError(f"ownership[{k}] 必须在 0~1 之间")
        own[k] = ratio
    return own


# ---------------------------------------------------------- 对外 API


def consolidated_balance_sheet(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    ownership: dict[str, object] | None = None,
    eliminations: list[dict] | None = None,
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """合并资产负债表（只读）。

    Returns 结构含 ``entities``（各主体分项）、``consolidated``（合并后 BS）、
    ``minority_interest``（少数股东权益）、``eliminations``（已应用抵消）、
    ``owned``（持股比例）、``currency``（报告币种）。
    """
    ls_list = _ledger_sets(session, ledger_set_ids)
    own = _normalize_ownership(ledger_set_ids, ownership)
    fx = {k: Decimal(str(v)) for k, v in (fx_rates or {}).items()}
    mp = M.get_mapping(standard)

    agg, ccy = _aggregate_amounts(session, ledger_set_ids, year, month, fx)
    applied = _apply_eliminations(agg, eliminations)
    (assets, total_assets, liabs, total_liabs, equity, total_equity) = _build_bs(mp, agg)

    # 少数股东权益：各子公司 (1-持股) × 该子公司权益总额
    minority_equity = ZERO
    entities = []
    for ls in ls_list:
        bs = balance_sheet(session, ls.id, year, month, standard)
        entities.append({
            "ledger_set_id": ls.id,
            "name": ls.name,
            "currency": ls.functional_currency or "CNY",
            "ownership": str(own[ls.id]),
            "assets_total": bs["assets"]["total"],
            "liabilities_total": bs["liabilities"]["total"],
            "equity_total": bs["equity"]["total"],
        })
        o = own[ls.id]
        if o < Decimal("1"):
            minority_equity += (Decimal("1") - o) * bs["equity"]["total"]

    # 本期合并净利润（集团未全结账时暂列权益项，保证表内平衡）
    inc = consolidated_income_statement(
        session, ledger_set_ids, year, month, standard, ownership, eliminations, fx_rates
    )
    np_total = inc["net_profit"]
    closed = _group_closed(session, ledger_set_ids, year, month)
    if np_total != ZERO and not closed:
        equity.append({
            "group": "未分配利润（本期净利润，未结转）",
            "amount": np_total,
            "accounts": [],
        })
        total_equity += np_total

    # 少数股东权益：作为「披露项」单列，**不**二次加回总额——合并权益总额
    # 已是「母公司 + 100% 子公司」（少数股权天然内含其中），故 total_equity
    # 不重复累计；仅暴露 minority_interest 与归属于母公司的权益 parent_equity。
    parent_equity = total_equity - minority_equity

    return {
        "ledger_set_ids": ledger_set_ids,
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": ccy,
        "entities": entities,
        "owned": {k: str(v) for k, v in own.items()},
        "eliminations": applied,
        "consolidated": {
            "assets": {"items": assets, "total": total_assets},
            "liabilities": {"items": liabs, "total": total_liabs},
            "equity": {"items": equity, "total": total_equity},
        },
        "minority_interest": minority_equity,
        "equity_parent": parent_equity,
        "balanced": (total_assets == total_liabs + total_equity),
        "check": {
            "assets": total_assets,
            "liabilities_plus_equity": total_liabs + total_equity,
            "diff": total_assets - (total_liabs + total_equity),
        },
    }


def consolidated_income_statement(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    ownership: dict[str, object] | None = None,
    eliminations: list[dict] | None = None,
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """合并利润表（只读）。

    合并净利润为 100% 口径；``minority_interest`` 为少数股东损益，
    ``net_profit_parent`` 为归属于母公司股东的净利润。
    """
    _ledger_sets(session, ledger_set_ids)  # 校验账套存在
    own = _normalize_ownership(ledger_set_ids, ownership)
    fx = {k: Decimal(str(v)) for k, v in (fx_rates or {}).items()}
    mp = M.get_mapping(standard)

    agg, ccy = _aggregate_amounts(session, ledger_set_ids, year, month, fx)
    applied = _apply_eliminations(agg, eliminations)
    items, revenue, expense, net = _build_is(mp, agg)

    # 少数股东损益：各子公司 (1-持股) × 该子公司净利润
    minority_pnl = ZERO
    for ls_id in ledger_set_ids:
        o = own[ls_id]
        if o < Decimal("1"):
            sub_net = income_statement(session, ls_id, year, month, standard)["net_profit"]
            minority_pnl += (Decimal("1") - o) * sub_net

    if minority_pnl != ZERO:
        items.append({
            "item": "少数股东损益", "amount": minority_pnl, "side": "credit",
        })

    return {
        "ledger_set_ids": ledger_set_ids,
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": ccy,
        "owned": {k: str(v) for k, v in own.items()},
        "eliminations": applied,
        "items": items,
        "revenue": revenue,
        "expense": expense,
        "net_profit": net,
        "minority_interest": minority_pnl,
        "net_profit_parent": net - minority_pnl,
    }


def consolidate(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    ownership: dict[str, object] | None = None,
    eliminations: list[dict] | None = None,
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """一次性取回合并资产负债表 + 合并利润表（只读）。"""
    bs = consolidated_balance_sheet(
        session, ledger_set_ids, year, month, standard, ownership,
        eliminations, fx_rates,
    )
    inc = consolidated_income_statement(
        session, ledger_set_ids, year, month, standard, ownership,
        eliminations, fx_rates,
    )
    return {
        "ledger_set_ids": ledger_set_ids,
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": bs["currency"],
        "entities": bs["entities"],
        "owned": bs["owned"],
        "eliminations": bs["eliminations"],
        "balance_sheet": bs,
        "income_statement": inc,
    }


def format_wecom_card(payload: dict) -> str:
    """合并报表企微 markdown 卡片（只读播报，含「人是 Boss」铁律提示）。"""
    p = payload["period"]
    bs = payload["balance_sheet"]["consolidated"]
    inc = payload["income_statement"]
    ccy = payload.get("currency", "CNY")
    lines = [
        f"# 合并报表 {p['year']}-{p['month']:02d}（{ccy}）",
        f"> 参与主体：{len(payload.get('entities', []))} 个",
        "",
        "**合并资产负债表**",
        f"- 资产总计：{bs['assets']['total']}",
        f"- 负债合计：{bs['liabilities']['total']}",
        f"- 所有者权益合计：{bs['equity']['total']}",
        f"- 其中：少数股东权益 {payload['balance_sheet'].get('minority_interest', '0')}",
        f"- 表内平衡：{'是' if payload['balance_sheet'].get('balanced') else '否 ⚠️'}",
        "",
        "**合并利润表**",
        f"- 营业收入：{inc['revenue']}",
        f"- 净利润（100% 口径）：{inc['net_profit']}",
        f"- 少数股东损益：{inc.get('minority_interest', '0')}",
        f"- 归属于母公司净利润：{inc.get('net_profit_parent', inc['net_profit'])}",
    ]
    elim = payload.get("eliminations") or []
    if elim:
        lines.append("")
        lines.append(f"**已应用内部抵消 {len(elim)} 项**（Boss 显式提供）")
    lines.append("")
    lines.append("数据自持 · AI 只出合并建议 · **人是 Boss**，抵消项/结账需你确认")
    return "\n".join(lines)

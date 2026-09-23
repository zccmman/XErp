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

from kernel.db.models import Account, LedgerSet, Voucher, VoucherLine
from kernel.opening import is_opening_voucher
from kernel.reporting import mapping as M
from kernel.coa import (
    DEFAULT_AP_ACCOUNTS,
    DEFAULT_AR_ACCOUNTS,
    DEFAULT_INVESTMENT_ACCOUNTS,
)
from kernel.reporting.statements import (
    ReportError,
    _period,
    amounts_by_code,
    balance_sheet,
    cash_flow,
    ending_balance,
    income_statement,
)

ZERO = Decimal("0")


def _rate_for_fx(
    fx_rates: dict[str, object] | None, ls_id: str, kind: str | None = None,
) -> Decimal:
    """解析某账套在指定分层（kind）下的折算汇率（模块级，供合并现金流复用）。

    fx_rates 支持标量形态 ``{ls_id: rate}`` 与分层形态
    ``{ls_id: {"closing": r1, "average": r2, "historical": r3}}``：
    分层 + 指定 kind → 取 kind（缺则回退 closing）；分层 + 未指定 kind → closing；
    标量 → 直接用。
    """
    r = (fx_rates or {}).get(ls_id, "1")
    if isinstance(r, dict):
        if kind:
            return Decimal(str(r.get(kind, r.get("closing", "1"))))
        return Decimal(str(r.get("closing", r.get("rate", "1"))))
    return Decimal(str(r))


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
    fx_rates: dict[str, object] | None = None,
    kind: str | None = None,
) -> tuple[dict[str, list[Decimal]], str]:
    """把各账套同 code 的借/贷发生额直接相加（按汇率换算到报告币种后）。

    返回 (agg, reporting_currency)。agg: code → [借方发生额, 贷方发生额]。

    fx_rates 支持两种形态（向后兼容）：
      - 标量形态（旧）：``{ls_id: rate}`` ——所有项目统一用该汇率折算；
      - 分层形态（新）：``{ls_id: {"closing": r1, "average": r2, "historical": r3}}``
        ——按 ``kind`` 取对应分层汇率：资产负债表用 kind="closing"（期末汇率），
        利润表用 kind="average"（平均汇率），权益可指定 kind="historical"。
        不传 kind 时 layered 回退到 "closing"（合并主表默认 BS 视角），标量
        形态则忽略 kind（所有项目同一汇率）。
    """
    fx_rates = fx_rates or {}

    def _rate_for(ls_id: str) -> Decimal:
        return _rate_for_fx(fx_rates, ls_id, kind)

    currencies: set[str] = set()
    agg: dict[str, list[Decimal]] = {}
    for ls_id in ledger_set_ids:
        ls = session.get(LedgerSet, ls_id)
        currencies.add(ls.functional_currency or "CNY")
        rate = _rate_for(ls_id)
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
    mp = M.get_mapping(standard)

    # 分层汇率：资产负债表用期末汇率（closing）
    agg, ccy = _aggregate_amounts(
        session, ledger_set_ids, year, month, fx_rates, kind="closing"
    )
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
    mp = M.get_mapping(standard)

    # 分层汇率：利润表用平均汇率（average）
    agg, ccy = _aggregate_amounts(
        session, ledger_set_ids, year, month, fx_rates, kind="average"
    )
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


# ---------------------------------------------------------- 阶段0 派生（P0-1 血缘 / P0-2 posting level）


def _scope_codes(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str, code: str | None, group: str | None,
) -> set[str]:
    """把血缘下钻的范围收敛为一组科目 code：code 直接命中；group 取所有
    归入该资产负债表大类的 code（跨主体并集）。"""
    mp = M.get_mapping(standard)
    if code:
        return {code}
    if not group:
        raise ConsolidationError(
            "consolidation_lineage 必须指定 code（科目）或 group（资产负债表大类）之一"
        )
    codes: set[str] = set()
    for ls_id in ledger_set_ids:
        for c in amounts_by_code(session, ls_id, year, month):
            pos = M.balance_sheet_group(mp, c)
            if pos and pos[1] == group:
                codes.add(c)
    if not codes:
        raise ConsolidationError(f"集团内没有任何科目归入「{group}」大类")
    return codes


def _source_vouchers(
    session: Session, ledger_set_id: str, year: int, month: int,
    scope_codes: set[str],
) -> list[dict]:
    """血缘下钻的底层：返回构成 scope_codes 余额的 POSTED 凭证明细（排除结转与期初）。

    口径与 ``amounts_by_code`` 一致（同一期间、POSTED、排除 结转-/期初），
    因此各 code 的凭证明细借贷合计 == Balance 投影发生额，可一路重建（ADR-002）。
    """
    period = _period(session, ledger_set_id, year, month)
    accs = {
        a.code: a.id
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        )
    }
    id_to_code = {accs[c]: c for c in scope_codes if c in accs}
    if not id_to_code:
        return []
    rows = session.execute(
        select(Voucher, VoucherLine)
        .join(VoucherLine, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
            Voucher.status == "POSTED",
            VoucherLine.account_id.in_(id_to_code),
        )
        .order_by(Voucher.voucher_no, VoucherLine.line_no)
    ).all()
    out: list[dict] = []
    for v, ln in rows:
        if v.voucher_no.startswith("结转-"):       # 期结转凭证不是本期经营来源
            continue
        if is_opening_voucher(v.voucher_no):         # 期初及其红字冲销同理
            continue
        out.append({
            "voucher_no": v.voucher_no,
            "voucher_date": v.voucher_date.isoformat(),
            "summary": (ln.summary or v.summary or ""),
            "account_code": id_to_code.get(ln.account_id, ""),
            "debit": Decimal(str(ln.debit)),
            "credit": Decimal(str(ln.credit)),
        })
    return out


def consolidation_lineage(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    code: str | None = None, group: str | None = None,
) -> dict:
    """合并血缘下钻（只读，P0-1 / Palantir 式端到端血缘）。

    给定合并资产负债表的一个科目 code 或大类 group，返回三层血缘：
      1. 集团合并数 ``consolidated_ending`` ——该范围在合并口径下的期末余额；
      2. 各主体分项 ``entities[]`` ——每个参与账套的期末余额贡献与逐 code 拆解；
      3. 各主体源凭证 ``entities[].vouchers`` ——构成该余额的 POSTED 凭证明细，
         可一路追到凭证流（ADR-002：投影可由凭证流重建）。

    仅基于各账套「主体上报数据」（PL00），不含 Boss 抵消项——抵消的本源不在
    任一主体账套内，单列于 posting level（见 ``consolidated_posting_levels``）。

    注：跨币种集团下 ``consolidated_ending`` 仅作信息性汇总（各主体仍按其本位币
    列示）；如需折算后合并数，请先用 ``consolidate`` + fx_rates 取得。
    """
    _ledger_sets(session, ledger_set_ids)
    scope = _scope_codes(session, ledger_set_ids, year, month, standard, code, group)
    mp = M.get_mapping(standard)

    entities: list[dict] = []
    consolidated = ZERO
    for ls_id in ledger_set_ids:
        ls = session.get(LedgerSet, ls_id)
        amts = amounts_by_code(session, ls_id, year, month)
        ending = ZERO
        accounts = []
        for c in sorted(scope):
            d, c_ = amts.get(c, (ZERO, ZERO))
            bal = ending_balance(c, d, c_)
            if bal != ZERO:
                ending += bal
                accounts.append({
                    "code": c,
                    "ending": bal,
                    "period_debit": d,
                    "period_credit": c_,
                })
        vouchers = _source_vouchers(session, ls_id, year, month, scope)
        entities.append({
            "ledger_set_id": ls_id,
            "name": ls.name,
            "currency": ls.functional_currency or "CNY",
            "ending": ending,
            "accounts": accounts,
            "vouchers": vouchers,
        })
        consolidated += ending

    return {
        "scope": {"code": code, "group": group, "codes": sorted(scope)},
        "period": {"year": year, "month": month},
        "standard": standard,
        "consolidated_ending": consolidated,
        "entities": entities,
    }


def consolidated_posting_levels(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    eliminations: list[dict] | None = None,
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """合并分录层级标注（只读，P0-2 / SAP posting level 透明化）。

    把合并资产负债表每个科目的金额拆成 posting level 来源：
      - PL00 主体上报数据：各账套发生额聚合（折算后）的期末余额；
      - PL20 Boss 抵消项：eliminations（HITL 显式提供）对余额的影响额。
    每个 code 的合并余额 = PL00 − PL20 影响；仅暴露有余额或被抵消的 code，
    供合并审计追溯「这一行里有多少是抵消出来的」，是 SAP 双 Monitor 步骤的
    前置透明化（先看清来源，再决定是否下钻/确认）。

    ownership 不影响 code 级余额（少数股权仅在权益披露层体现），故本函数不接收。
    """
    _ledger_sets(session, ledger_set_ids)
    mp = M.get_mapping(standard)

    # 不传 kind → layered 回退 closing（posting level 为 BS 视角）
    pre_agg, ccy = _aggregate_amounts(session, ledger_set_ids, year, month, fx_rates)
    pre_copy = {c: [d, cr] for c, (d, cr) in pre_agg.items()}
    applied = _apply_eliminations(pre_agg, eliminations)

    levels: list[dict] = []
    for c in sorted(set(pre_copy) | set(pre_agg)):
        d_pre, c_pre = pre_copy.get(c, [ZERO, ZERO])
        d_post, c_post = pre_agg.get(c, [ZERO, ZERO])
        pl00 = ending_balance(c, d_pre, c_pre)
        consolidated = ending_balance(c, d_post, c_post)
        pl20 = pl00 - consolidated          # 抵消对余额的影响（= PL00 − 合并余额；正表示该项被抵消、合并余额相对主体上报下降）
        if consolidated == ZERO and pl20 == ZERO:
            continue
        pos = M.balance_sheet_group(mp, c)
        levels.append({
            "code": c,
            "report_line": (pos[1] if pos else "（利润表/未分类项目）"),
            "pl00_entity_reported": pl00,
            "pl20_elimination": pl20,
            "consolidated": consolidated,
        })
    return {
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": ccy,
        "levels": levels,
        "eliminations": applied,
    }


# ---------------------------------------------------------- 阶段1 草稿（Oracle ICP / SAP COI）
#
# 设计定位：把「抵消从手敲变 Boss 确认」。两个函数都**只出草稿**——
# 自动算出可抵消的内部往来 / 长投-权益配比，返回供 Boss 审阅的
# ``draft_eliminations`` / ``suggested_eliminations``；Boss 确认后再把这些
# 消除项原样喂回 ``consolidate`` / ``consolidated_posting_levels``。
# 绝不自行应用、绝不写账，守住 HITL 铁律与 ADR-002 单一真源。


def _find_goodwill_code(session: Session, ledger_set_id: str) -> str | None:
    """在母公司账套里找一个「商誉」科目（资产类、名称含'商誉'或编码以 19 开头）。

    找不到返回 None——此时 COI 草稿只披露商誉、不自动消除，避免引入未定义
    科目导致合并表不平衡。
    """
    for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ):
        if a.category != "asset":
            continue
        if (a.name and "商誉" in a.name) or a.code.startswith("19"):
            return a.code
    return None


def propose_icp_eliminations(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """内部往来自动配对（阶段1 / Oracle ICP 精神，只读草稿）。

    集团内各主体的应收（``DEFAULT_AR_ACCOUNTS``）与应付（``DEFAULT_AP_ACCOUNTS``）
    在合并层面应当等额对冲（甲对乙的应收 = 乙对甲的应付）。本函数做**集团级净额
    配对**：取各账套折算后应收/应付净额，可抵消额 = min(应收合计, 应付合计)，
    生成一笔集团级抵消建议。

    ⚠️ 诚实边界：XErp 当前凭证明细无「对手方账套」维度，无法逐对手方精确配对，
    故这是**集团级近似**——应收与应付不对称的差额可能来自外部往来或非对称内部
    交易，必须 Boss 复核后再确认（不会臆测具体配对）。

    返回结构含 ``draft_eliminations``（可直接喂回 ``consolidate`` 的消除项）、
    ``total_receivables`` / ``total_payables`` / ``matched`` / 各 code 明细，
    以及 ``notes`` 提示。
    """
    _ledger_sets(session, ledger_set_ids)
    # 不传 kind → layered 回退 closing（ICP 为 BS 层面应收/应付）
    agg, ccy = _aggregate_amounts(session, ledger_set_ids, year, month, fx_rates)

    ar_balances: dict[str, Decimal] = {}
    ap_balances: dict[str, Decimal] = {}
    for code, (d, c) in agg.items():
        net = ending_balance(code, d, c)  # 正=资产借超 / 负债贷超
        if code in DEFAULT_AR_ACCOUNTS and net > ZERO:
            ar_balances[code] = net
        if code in DEFAULT_AP_ACCOUNTS and net > ZERO:
            ap_balances[code] = net

    total_ar = sum(ar_balances.values(), ZERO)
    total_ap = sum(ap_balances.values(), ZERO)
    matched = min(total_ar, total_ap)

    draft: list[dict] = []
    notes: list[str] = []
    if ar_balances and ap_balances and matched > ZERO:
        ar_rep = max(ar_balances, key=lambda c: ar_balances[c])
        ap_rep = max(ap_balances, key=lambda c: ap_balances[c])
        # dr_code=应收(资产，借正→减借) / cr_code=应付(负债，贷正→减贷)：
        # 两者各减 matched，配对归零内部往来（与 _apply_eliminations 的减借/减贷语义一致）。
        draft.append({
            "dr_code": ar_rep, "cr_code": ap_rep,
            "amount": str(matched),
        })
    if total_ar == ZERO or total_ap == ZERO:
        notes.append(
            "集团内未识别到内部应收/应付余额，无需抵消"
            "（或应收、应付在合并口径下均不为零的一方为空）"
        )
    if total_ar != total_ap:
        notes.append(
            f"应收({total_ar})与应付({total_ap})不对称，差额可能为外部往来或非对称"
            f"内部交易，请 Boss 复核后再确认；本建议仅抵消可配对部分 {matched}。"
        )
    notes.append(
        "本配对为集团级净额配对（未逐对手方），抵消建议在确认前请核对具体往来对象。"
    )
    denom = max(total_ar, total_ap, Decimal("1"))
    return {
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": ccy,
        "receivables": {c: str(v) for c, v in ar_balances.items()},
        "payables": {c: str(v) for c, v in ap_balances.items()},
        "total_receivables": str(total_ar),
        "total_payables": str(total_ap),
        "matched": str(matched),
        "unmatched_receivables": str(total_ar - matched),
        "unmatched_payables": str(total_ap - matched),
        "match_ratio": str(matched / denom),
        "draft_eliminations": draft,
        "notes": notes,
    }


def propose_coi_eliminations(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    ownership: dict[str, object] | None = None,
    fx_rates: dict[str, object] | None = None,
    parent_id: str | None = None,
) -> dict:
    """长期股权投资与子公司权益抵销草稿（阶段1 / SAP COI 精神，只读草稿）。

    对母公司账套的「长期股权投资」（``DEFAULT_INVESTMENT_ACCOUNTS``，如 1511）
    与子公司账套的「所有者权益」（按 ``Account.category == 'equity'`` 识别，不依赖
    映射前缀，更贴近真实账套）做配比抵销，并自动推算：
      - 应享权益份额 = 持股 × 子公司权益总额（attributable）；
      - 商誉 = 长期股权投资 − 应享权益份额（正=商誉，负=廉价购买）；
      - 少数股东权益 = (1−持股) × 子公司权益总额（由 ``consolidate`` 的少数股权逻辑
        处理，本草稿仅披露，不重复消除）。

    每个子公司生成一组 ``suggested_eliminations``（借长投 / 贷子公司各权益科目按持股
    比例），母公司长投若有剩余（商誉）且账套有商誉科目则单列一笔商誉消除使长投清零。
    Boss 确认后把 ``all_suggested_eliminations`` 原样喂回 ``consolidate`` 即可。

    ⚠️ 诚实边界：假设母公司长投 1511 **全部**对应所列子公司；多子公司时请分别运行或
    拆分 1511（当前不做层级合并，见增强路线图阶段3）。

    母公司识别：``parent_id`` 优先；否则取 ``ownership`` 中 ==1.0 的唯一账套，缺失或多
    个则抛 ``ConsolidationError`` 要求显式指定。
    """
    ls_list = _ledger_sets(session, ledger_set_ids)
    own = _normalize_ownership(ledger_set_ids, ownership)

    if parent_id:
        if parent_id not in own:
            raise ConsolidationError("parent_id 不在 ledger_set_ids 内")
        parent = parent_id
    else:
        parents = [i for i, o in own.items() if o == Decimal("1")]
        if len(parents) == 1:
            parent = parents[0]
        else:
            # 多个全资（含全资子公司情形）：以「持有长期股权投资」的账套为母公司。
            # 全资子公司场景下母子 ownership 都为 1.0，无法靠持股比例区分，
            # 但母公司账套必然挂有长投余额，子公司不会，故可据此唯一确定。
            inv_map: dict[str, Decimal] = {}
            for ls in ls_list:
                p_amts = amounts_by_code(session, ls.id, year, month)
                inv = ZERO
                for code in DEFAULT_INVESTMENT_ACCOUNTS:
                    if code in p_amts:
                        inv += ending_balance(code, *p_amts[code])
                if inv > ZERO:
                    inv_map[ls.id] = inv
            if len(inv_map) == 1:
                parent = next(iter(inv_map))
            else:
                raise ConsolidationError(
                    "无法确定母公司：请指定 parent_id，或恰好一个账套 ownership=1 "
                    "或持有长期股权投资"
                )

    # 所有非母公司的参与账套都视为被投资方（含全资子公司：持股 1.0 时
    # 少数股权=0、应享权益=100%，长投与权益仍须抵消）。
    subs = [i for i in ledger_set_ids if i != parent]
    parent_ls = next(x for x in ls_list if x.id == parent)
    notes: list[str] = []

    # 母公司长期股权投资（折算后净额，正=借超）
    p_amts = amounts_by_code(session, parent, year, month)
    investment = ZERO
    for code in DEFAULT_INVESTMENT_ACCOUNTS:
        if code in p_amts:
            investment += ending_balance(code, *p_amts[code])
    goodwill_code = _find_goodwill_code(session, parent)

    subs_out: list[dict] = []
    all_elims: list[dict] = []
    if not subs:
        notes.append("未识别到子公司（持股<1 的账套），无需 COI 抵销")
    for sub in subs:
        sub_ls = next(x for x in ls_list if x.id == sub)
        s_accs = {
            a.code: a
            for a in session.scalars(
                select(Account).where(Account.ledger_set_id == sub)
            )
        }
        s_amts = amounts_by_code(session, sub, year, month)
        equity_items: list[tuple[str, Decimal]] = []
        for code, a in s_accs.items():
            if a.category != "equity":
                continue
            if code in s_amts:
                net = ending_balance(code, *s_amts[code])
                if net != ZERO:
                    equity_items.append((code, net))
        sub_equity = sum((net for _, net in equity_items), ZERO)
        o = own[sub]
        attributable = o * sub_equity
        goodwill = investment - attributable          # 假设 1511 全对该子（多子不准）
        minority = (Decimal("1") - o) * sub_equity

        elims: list[dict] = []
        for code, net in equity_items:
            # 借长投（dr_code 减借）、贷子公司权益（cr_code 减贷），按持股比例配比
            elims.append({
                "dr_code": DEFAULT_INVESTMENT_ACCOUNTS[0],
                "cr_code": code,
                "amount": str(o * net),
            })
        # 长投与子公司权益的「配比部分」被抵消清零；剩余 goodwill（长投 − 应享权益）
        # 作为披露项（_apply_eliminations 仅支持「减」余额，无法增记商誉，故不在消除
        # 里造商誉科目，避免引入未定义科目或破坏表平衡）。请 Boss 确认前核对。
        if goodwill != ZERO:
            if goodwill > ZERO:
                hint = (
                    f"母公司账套有商誉科目 {goodwill_code}，可重分类"
                    if goodwill_code else
                    "母公司账套无商誉科目（建议建 1911），请手工重分类"
                )
                notes.append(
                    f"子公司 {sub_ls.name} 存在商誉 {goodwill}（长投>应享权益）："
                    f"{hint}；本草稿仅抵消配比部分，长投剩余额作残值披露"
                )
            else:
                notes.append(
                    f"子公司 {sub_ls.name} 出现负商誉（廉价购买）{abs(goodwill)}，"
                    f"请 Boss 手工处理（本草稿不自动消除）"
                )
        all_elims.extend(elims)
        subs_out.append({
            "ledger_set_id": sub,
            "name": sub_ls.name,
            "ownership": str(o),
            "equity_total": str(sub_equity),
            "investment": str(investment),
            "attributable": str(attributable),
            "goodwill": str(goodwill),
            "minority_interest": str(minority),
            "equity_breakdown": [
                {"code": c, "amount": str(net), "eliminate_amount": str(o * net)}
                for c, net in equity_items
            ],
            "suggested_eliminations": elims,
        })

    if subs:
        notes.append(
            "假设母公司长期股权投资（1511）全部对应所列子公司；多子公司时请分别运行或"
            "拆分 1511（当前不做层级合并，见增强路线图阶段3）。"
        )
    return {
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": parent_ls.functional_currency or "CNY",
        "parent": {
            "ledger_set_id": parent,
            "name": parent_ls.name,
            "investment": str(investment),
        },
        "subsidiaries": subs_out,
        "all_suggested_eliminations": all_elims,
        "notes": notes,
    }


# ---------------------------------------------------------- 阶段2（合并现金流量表）

def _apply_cash_flow_eliminations(
    categories: dict[str, dict], eliminations: list[dict] | None,
) -> list[dict]:
    """应用现金流内部往来抵消（Boss 显式提供，HITL）。

    每个抵消项 ``{category, item, amount}`` 从对应 category 的 item（带符号金额）
    与流入/流出额中冲减；amount 不得超过该 item 绝对值（否则破坏勾稽）。返回明细。
    """
    applied: list[dict] = []
    for i, e in enumerate(eliminations or []):
        if not isinstance(e, dict) or "category" not in e or "item" not in e \
                or "amount" not in e:
            raise ConsolidationError(
                f"现金流抵消项#{i} 必须含 category / item / amount 三键"
            )
        try:
            amt = Decimal(str(e["amount"]))
        except Exception as exc:  # noqa: BLE001
            raise ConsolidationError(f"现金流抵消项#{i} 金额非法：{exc}") from exc
        if amt < ZERO:
            raise ConsolidationError(f"现金流抵消项#{i} 金额必须非负")
        cat = str(e["category"])
        item = str(e["item"])
        if cat not in categories or item not in categories[cat]["items"]:
            raise ConsolidationError(
                f"现金流抵消项#{i} 目标不存在：{cat}/{item}"
            )
        cur = categories[cat]["items"][item]
        if amt > abs(cur):
            raise ConsolidationError(
                f"现金流抵消项#{i} 金额 {amt} 超过该项绝对值 {abs(cur)}"
            )
        if cur >= ZERO:
            categories[cat]["items"][item] = cur - amt
            categories[cat]["in"] -= amt
        else:
            categories[cat]["items"][item] = cur + amt
            categories[cat]["out"] -= amt
        applied.append({"category": cat, "item": item, "amount": str(amt)})
    return applied


def consolidated_cash_flow(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    ownership: dict[str, object] | None = None,
    eliminations: list[dict] | None = None,
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """合并现金流量表（直接法，只读）。

    汇总各参与账套的单体现金流量表（复用 ``statements.cash_flow`` 同一口径），按报告
    币种（平均汇率）折算后加总，并保持勾稽：合并期初现金 + 合并净增加 = 合并期末现金。

    内部现金往来抵消由 **Boss 显式提供** ``eliminations``（与 BS/IS 同 HITL 哲学），
    形如 ``[{"category":"investing","item":"投资支付的现金","amount":"1200"}]``；
    若需自动建议抵消，见 ``propose_cash_flow_eliminations``。
    """
    ls_list = _ledger_sets(session, ledger_set_ids)
    own = _normalize_ownership(ledger_set_ids, ownership)

    categories: dict[str, dict] = {
        "operating": {"in": ZERO, "out": ZERO, "items": {}},
        "investing": {"in": ZERO, "out": ZERO, "items": {}},
        "financing": {"in": ZERO, "out": ZERO, "items": {}},
    }
    entities: list[dict] = []
    opening_cash = ZERO
    for ls in ls_list:
        cf = cash_flow(session, ls.id, year, month, standard)
        rate = _rate_for_fx(fx_rates, ls.id, kind="average")
        for cat in ("operating", "investing", "financing"):
            c = cf["categories"][cat]
            categories[cat]["in"] += c["in"] * rate
            categories[cat]["out"] += c["out"] * rate
            for it in c["items"]:
                key = it["item"]
                categories[cat]["items"][key] = (
                    categories[cat]["items"].get(key, ZERO) + it["amount"] * rate
                )
        opening_cash += cf["reconcile"]["opening_cash"] * rate
        entities.append({
            "ledger_set_id": ls.id,
            "name": ls.name,
            "currency": ls.functional_currency or "CNY",
            "ownership": str(own[ls.id]),
            "operating": cf["operating"],
            "investing": cf["investing"],
            "financing": cf["financing"],
            "net_increase": cf["net_increase"],
        })

    applied = _apply_cash_flow_eliminations(categories, eliminations)

    op = categories["operating"]["in"] - categories["operating"]["out"]
    inv = categories["investing"]["in"] - categories["investing"]["out"]
    fin = categories["financing"]["in"] - categories["financing"]["out"]
    net_increase = op + inv + fin
    closing_cash = opening_cash + net_increase
    _, ccy = _aggregate_amounts(
        session, ledger_set_ids, year, month, fx_rates, kind="average"
    )

    return {
        "ledger_set_ids": ledger_set_ids,
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": ccy,
        "entities": entities,
        "owned": {k: str(v) for k, v in own.items()},
        "eliminations": applied,
        "categories": {
            cat: {
                "in": categories[cat]["in"],
                "out": categories[cat]["out"],
                "net": categories[cat]["in"] - categories[cat]["out"],
                "items": [
                    {"item": k, "amount": v}
                    for k, v in sorted(categories[cat]["items"].items())
                ],
            }
            for cat in ("operating", "investing", "financing")
        },
        "operating": op,
        "investing": inv,
        "financing": fin,
        "net_increase": net_increase,
        "reconcile": {
            "opening_cash": opening_cash,
            "net_increase": net_increase,
            "closing_cash": closing_cash,
        },
        "balanced": (opening_cash + net_increase == closing_cash),
    }


def propose_cash_flow_eliminations(
    session: Session, ledger_set_ids: list[str], year: int, month: int,
    standard: str = "small_business",
    fx_rates: dict[str, object] | None = None,
) -> dict:
    """合并现金流内部往来抵消建议（阶段2，只读草稿）。

    识别集团内**权益性投资**现金流的镜像配对：母公司账套「投资支付的现金」
    （investing 流出）与子公司账套「吸收投资收到的现金」（financing 流入）在合并
    层面应当等额对冲。做**集团级镜像配对**：可抵消额 = min(集团投资支付净额,
    集团吸收投资净流入)，生成两笔建议抵消项（分别对应投资支付与吸收投资）。

    ⚠️ 诚实边界：XErp 当前凭证明细无「对手方账套」维度，无法逐笔确认具体交易对象，
    故这是**集团级近似**；借款、股利类内部现金往来不在本建议内（请 Boss 结合
    ICP/COI 配对结果手工判断）。建议抵消项可直接喂回 ``consolidated_cash_flow`` 的
    eliminations。
    """
    _ledger_sets(session, ledger_set_ids)
    _, ccy = _aggregate_amounts(
        session, ledger_set_ids, year, month, fx_rates, kind="average"
    )

    invest_pay = ZERO    # 集团投资支付净额（流出绝对值，正计量）
    finance_recv = ZERO  # 集团吸收投资/取得借款净流入（正计量）
    for ls_id in ledger_set_ids:
        cf = cash_flow(session, ls_id, year, month, standard)
        rate = _rate_for_fx(fx_rates, ls_id, kind="average")
        for cat in ("operating", "investing", "financing"):
            for it in cf["categories"][cat]["items"]:
                amt = it["amount"] * rate  # 带符号：流入正、流出负
                label = it["item"]
                if cat == "investing" and amt < ZERO and "投资支付" in label:
                    invest_pay += -amt
                if cat == "financing" and amt > ZERO and (
                        "吸收投资" in label or "取得借款" in label):
                    finance_recv += amt

    matched = min(invest_pay, finance_recv)
    suggest: list[dict] = []
    notes: list[str] = []
    if matched > ZERO:
        suggest.append(
            {"category": "investing", "item": "投资支付的现金", "amount": str(matched)}
        )
        suggest.append(
            {"category": "financing", "item": "吸收投资收到的现金", "amount": str(matched)}
        )
        notes.append(
            f"已识别内部权益投资镜像可抵消 {matched}（投资支付 {invest_pay} / "
            f"吸收投资 {finance_recv} 取小）"
        )
        notes.append(
            "⚠️ 诚实边界：凭证明细无对手方账套维度，仅做集团级镜像配对，无法逐笔确认具体"
            "交易对象；借款/股利类内部现金往来未自动识别，请 Boss 结合 ICP/COI 配对结果手工判断。"
        )
    else:
        notes.append(
            "未识别到集团内部权益性投资现金流镜像（母投子：投资支付 ↔ 吸收投资），无需抵消"
        )
    if invest_pay != finance_recv:
        notes.append(
            f"投资支付({invest_pay})与吸收投资({finance_recv})不对称，差额可能为外部投资或"
            f"借款，请 Boss 复核后再确认；本建议仅抵消可配对部分 {matched}。"
        )
    return {
        "period": {"year": year, "month": month},
        "standard": standard,
        "currency": ccy,
        "investing_out": str(invest_pay),
        "financing_in": str(finance_recv),
        "matched": str(matched),
        "suggested_eliminations": suggest,
        "notes": notes,
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

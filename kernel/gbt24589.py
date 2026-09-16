"""GB/T 24589.1-2024 会计核算软件数据接口导出（审计事件导出对齐）。

为什么做：GB/T 24589 是审计署提出、国家市场监管总局发布的**审计数据采集国标**，
审计/税务/监管机关据此从任意财务软件抽取标准化账表（金审工程「Golden Audit」）。
UB 等主流 ERP 均通过该标准认证（UB V8.90/U9/NC 等）。我们把**不可篡改事件链**
直接派生为该标准账表——既合规可审计，又自带密码学可追溯（provenance）。

设计要点：
- 只读、从 POSTED 凭证 + 科目 + 期间 + 事件链派生，**不动账本**；
- 严格继承 ledgerbook.ledger_detail 的口径：**期初是存量不是发生额**，方向按科目
  正常方向 + 净额符号判定（资产/成本正余额=借，负债/权益/损益正余额=贷）；
- 输出 JSON（2024 版附录 E 明确支持）或 XML（附录 C）；每张表标注标准数据元
  标识符（如 020602 记账凭证编号、020501 期初余额方向）；
- provenance 块附带本账套事件数与链尾哈希，把「国标账表」与「不可篡改账本」桥接。

覆盖的核心表（总账类 + 基础档案类，审计最关心的部分）：
    电子账簿 / 会计期间 / 会计科目 / 币种 / 科目余额及发生额 / 记账凭证 / 记账凭证分录
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from kernel.db.models import (
    Account,
    Event,
    LedgerSet,
    Period,
    Subject,
    Voucher,
)
from kernel.events import E
from kernel.opening import is_opening_voucher

ZERO = Decimal("0")

CURRENCY_NAMES = {
    "CNY": "人民币", "USD": "美元", "EUR": "欧元", "HKD": "港币",
    "JPY": "日元", "GBP": "英镑", "MOP": "澳门元", "TWD": "新台币",
}

CATEGORY_MAP = {
    "asset": "资产", "liability": "负债", "equity": "权益",
    "cost": "成本", "pnl": "损益",
}
DIRECTION_MAP = {"debit": "借", "credit": "贷"}

# 标准表描述：name=标准表名，code=GB/T 24589.1 模块/表编码，fields=(标识符, 中文名)
TABLE_SPECS = {
    "account_book": {
        "name": "电子账簿",
        "code": "00",
        "fields": [
            ("0001", "会计软件名称"), ("0002", "会计软件版本"),
            ("0003", "账簿名称"), ("0004", "记账本位币"), ("0005", "会计口径"),
            ("0006", "数据期间起始"), ("0007", "数据期间终止"),
            ("0008", "导出时间"), ("0009", "数据接口标准"),
            ("0010", "事件总数"), ("0011", "链尾哈希"),
        ],
    },
    "accounting_period": {
        "name": "会计期间",
        "code": "01",
        "fields": [
            ("010113", "会计年度"), ("010201", "会计期间号"),
            ("010202", "开始日期"), ("010203", "结束日期"), ("010204", "期间状态"),
        ],
    },
    "chart_of_accounts": {
        "name": "会计科目",
        "code": "02",
        "fields": [
            ("020301", "科目编号"), ("020302", "科目名称"),
            ("020303", "科目类型"), ("020304", "余额方向"),
            ("020305", "是否末级"), ("020306", "辅助核算"),
        ],
    },
    "currency": {
        "name": "币种",
        "code": "03",
        "fields": [
            ("011101", "币种编码"), ("011102", "币种名称"), ("011103", "记账本位币标志"),
        ],
    },
    "account_balance": {
        "name": "科目余额及发生额",
        "code": "05",
        "fields": [
            ("010113", "会计年度"), ("010201", "会计期间号"), ("020301", "科目编号"),
            ("011101", "币种编码"),
            ("020501", "期初余额方向"), ("020506", "期初本币余额"),
            ("020509", "借方本币金额"), ("020512", "贷方本币金额"),
            ("020502", "期末余额方向"), ("020515", "期末本币余额"),
            ("020504", "期初数量"), ("020507", "借方数量"),
            ("020510", "贷方数量"), ("020513", "期末数量"),
        ],
    },
    "voucher": {
        "name": "记账凭证",
        "code": "06",
        "fields": [
            ("010113", "会计年度"), ("010201", "会计期间号"),
            ("020201", "记账凭证类型编号"), ("020602", "记账凭证编号"),
            ("020601", "记账凭证日期"), ("020612", "附件数"),
            ("020613", "制单人"), ("020614", "审核人"), ("020615", "记账人"),
            ("020616", "记账标志"),
        ],
    },
    "voucher_entry": {
        "name": "记账凭证分录",
        "code": "07",
        "fields": [
            ("010113", "会计年度"), ("010201", "会计期间号"),
            ("020602", "记账凭证编号"), ("020603", "记账凭证行号"),
            ("020604", "记账凭证摘要"), ("020301", "科目编号"),
            ("020509", "借方本币金额"), ("020512", "贷方本币金额"),
            ("020401", "辅助项1编号"), ("020503", "计量单位"),
            ("020606", "单价"), ("020504", "数量"),
        ],
    },
}


class GbtError(Exception):
    """GB/T 24589 导出错误：code + 中文信息（ADR-003 错误信封形态，便于 MCP 分类）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message_zh = message


def _fmt(d: Decimal | None) -> str:
    return f"{(d or ZERO):.2f}"


def _ymd(d: date | None) -> str:
    return d.strftime("%Y%m%d") if d else ""


def _opp(direction: str) -> str:
    return "贷" if direction == "借" else "借"


def _signed(dr: Decimal, cr: Decimal, is_debit_dir: bool) -> Decimal:
    return (dr - cr) if is_debit_dir else (cr - dr)


def _direction_of(signed_net: Decimal, is_debit_dir: bool) -> str:
    normal = "借" if is_debit_dir else "贷"
    return normal if signed_net >= ZERO else _opp(normal)


def _voucher_type_no(voucher_no: str) -> str:
    """从凭证号前缀推导凭证类型编号（记/收/付/转），无前缀默认「记」。"""
    if not voucher_no:
        return "记"
    head = voucher_no[0]
    return head if head in ("记", "收", "付", "转") else "记"


def _resolve_names(session, subject_ids: set[str]) -> dict[str, str]:
    if not subject_ids:
        return {}
    subs = session.scalars(
        select(Subject).where(Subject.id.in_(subject_ids))
    ).all()
    return {s.id: s.display_name for s in subs}


def _load_approve_post_actors(session, ledger_set_id: str) -> tuple[dict, dict]:
    """从事件链追溯每张凭证的审核人 / 记账人（审计事件导出核心）。

    返回 (approved_by, posted_by)：aggregate_id(=voucher_id) -> subject_id。

    兼容说明：事件类型以 E 枚举为准（大写，如 VOUCHER_POSTED）；但早期数据
    曾用过小写串（voucher.posted / voucher.approved，枚举重构前），旧账套里仍
    并存。这里两种都认，保证历史账套导出不漏记账人/审核人。
    """
    # 记账动作事件集：常规过账 + L3 自治过账 + 历史小写串
    POST_EVENTS = {E.VOUCHER_POSTED, E.AUTONOMOUS_POSTED, "voucher.posted"}
    APPROVE_EVENTS = {E.VOUCHER_APPROVED, "voucher.approved"}
    evs = session.scalars(
        select(Event).where(
            Event.ledger_set_id == ledger_set_id,
            Event.event_type.in_(list(POST_EVENTS | APPROVE_EVENTS)),
        )
    ).all()
    approved_by: dict[str, str] = {}
    posted_by: dict[str, str] = {}
    for e in evs:
        actor = e.actor or {}
        aid = str(actor.get("id") or "")
        name = actor.get("name") or ""
        if e.event_type in APPROVE_EVENTS:
            approved_by[e.aggregate_id] = name or aid
        else:
            posted_by[e.aggregate_id] = name or aid
    return approved_by, posted_by


def _compute_balances(session, ls: LedgerSet, accounts, periods_all, scope_period_ids,
                      func_ccy: str) -> tuple[list[dict], set[str], set[str]]:
    """按期间滚动计算科目余额及发生额；仅输出 scope 期间。

    返回 (balance_rows, qty_account_ids, fx_currency_codes)。
    """
    acc_dir = {a.id: (a.direction == "debit") for a in accounts}
    acc_ids = [a.id for a in accounts]

    # 所有 POSTED 凭证（含 scope 之前的，用于正确计算期初）
    vouchers = session.scalars(
        select(Voucher)
        .where(Voucher.ledger_set_id == ls.id, Voucher.status == "POSTED")
        .order_by(Voucher.voucher_date, Voucher.voucher_no)
        .options(selectinload(Voucher.lines))
    ).all()
    per_period_lines: dict[str, list[tuple[Voucher, object]]] = {}
    fx_codes: set[str] = set()
    qty_account_ids: set[str] = set()
    for v in vouchers:
        per_period_lines.setdefault(v.period_id, []).append(v)
        for ln in v.lines:
            if ln.currency and ln.currency != func_ccy:
                fx_codes.add(ln.currency)
            if ln.quantity is not None:
                qty_account_ids.add(ln.account_id)

    running: dict[str, Decimal] = {aid: ZERO for aid in acc_ids}
    running_qty: dict[str, Decimal] = {aid: ZERO for aid in acc_ids}
    balance_rows: list[dict] = []

    for p in periods_all:
        before = dict(running)
        before_qty = dict(running_qty)
        gross: dict[str, list[Decimal]] = {}  # acc_id -> [dr, cr]
        gross_qty: dict[str, list[Decimal]] = {}
        for v in per_period_lines.get(p.id, []):
            for ln in v.lines:
                acc = ln.account_id
                dr = Decimal(str(ln.debit))
                cr = Decimal(str(ln.credit))
                g = gross.get(acc, [ZERO, ZERO])
                g[0] += dr
                g[1] += cr
                gross[acc] = g
                is_dd = acc_dir.get(acc, True)
                running[acc] = running.get(acc, ZERO) + _signed(dr, cr, is_dd)
                if acc in qty_account_ids and ln.quantity is not None:
                    q = Decimal(str(ln.quantity))
                    signed_qty = q if dr > ZERO else -q
                    running_qty[acc] = running_qty.get(acc, ZERO) + signed_qty
                    gq = gross_qty.get(acc, [ZERO, ZERO])
                    if dr > ZERO:
                        gq[0] += q
                    else:
                        gq[1] += q
                    gross_qty[acc] = gq
        if p.id not in scope_period_ids:
            continue
        for a in accounts:
            aid = a.id
            is_dd = a.direction == "debit"
            opening = before.get(aid, ZERO)
            dr_g, cr_g = gross.get(aid, (ZERO, ZERO))
            closing = running.get(aid, ZERO)
            row = {
                "会计年度": p.year,
                "会计期间号": p.month,
                "科目编号": a.code,
                "币种编码": func_ccy,
                "期初余额方向": _direction_of(opening, is_dd),
                "期初本币余额": _fmt(abs(opening)),
                "借方本币金额": _fmt(dr_g),
                "贷方本币金额": _fmt(cr_g),
                "期末余额方向": _direction_of(closing, is_dd),
                "期末本币余额": _fmt(abs(closing)),
            }
            if aid in qty_account_ids:
                oq = before_qty.get(aid, ZERO)
                dq, cq = gross_qty.get(aid, (ZERO, ZERO))
                cq_net = running_qty.get(aid, ZERO)
                row["期初数量"] = _fmt(abs(oq))
                row["借方数量"] = _fmt(abs(dq))
                row["贷方数量"] = _fmt(abs(cq))
                row["期末数量"] = _fmt(abs(cq_net))
            balance_rows.append(row)
    return balance_rows, qty_account_ids, fx_codes


def build_export(session, *, ledger_set_id: str, year: int, month: int = 0,
                 fmt: str = "json") -> str:
    """构建 GB/T 24589.1-2024 标准导出（JSON 或 XML 字符串）。

    参数：
        ledger_set_id  账套 id
        year           会计年度（必填）
        month          0 = 全年；非 0 = 仅该月
        fmt            "json" 或 "xml"

    返回序列化字符串。错误以 GbtError 抛出（ok 风格由调用方包装）。
    """
    if fmt not in ("json", "xml"):
        raise GbtError("BAD_FORMAT", f"不支持的格式 {fmt!r}，仅 json/xml")
    if not isinstance(year, int) or year <= 0:
        raise GbtError("BAD_YEAR", f"会计年度非法：{year!r}")

    ls = session.get(LedgerSet, ledger_set_id)
    if ls is None:
        raise GbtError("LEDGER_NOT_FOUND", f"账套 {ledger_set_id} 不存在")

    func_ccy = ls.functional_currency or "CNY"
    accounts = session.scalars(
        select(Account).where(Account.ledger_set_id == ls.id).order_by(Account.code)
    ).all()
    periods_all = session.scalars(
        select(Period).where(Period.ledger_set_id == ls.id)
        .order_by(Period.year, Period.month)
    ).all()
    # scope 期间：限定 year，month!=0 再限定月
    scope_periods = [
        p for p in periods_all
        if p.year == year and (month == 0 or p.month == month)
    ]
    if not scope_periods:
        raise GbtError(
            "PERIOD_NOT_FOUND",
            f"{year} 年" + (f"{month:02d} 月" if month else "") + "无会计期间",
        )
    scope_ids = {p.id for p in scope_periods}
    period_key = {(p.year, p.month) for p in scope_periods}
    span_start = min(period_key)
    span_end = max(period_key)

    # —— 科目余额及发生额 ——
    balance_rows, qty_account_ids, fx_codes = _compute_balances(
        session, ls, accounts, periods_all, scope_ids, func_ccy
    )

    # —— 记账凭证 + 分录 ——
    approved_by, posted_by = _load_approve_post_actors(session, ls.id)
    name_ids = set()
    name_ids.update(approved_by.values(), posted_by.values())
    maker_ids = session.scalars(
        select(Voucher.created_by).where(Voucher.ledger_set_id == ls.id)
    ).all()
    name_ids.update(str(i) for i in maker_ids if i)
    names = _resolve_names(session, name_ids)

    voucher_rows: list[dict] = []
    entry_rows: list[dict] = []
    scope_vouchers = [
        v for v in session.scalars(
            select(Voucher)
            .where(Voucher.ledger_set_id == ls.id, Voucher.status == "POSTED")
            .order_by(Voucher.voucher_date, Voucher.voucher_no)
            .options(selectinload(Voucher.lines))
        ).all()
        if (v.voucher_date.year, v.voucher_date.month) in period_key
    ]
    for v in scope_vouchers:
        maker = names.get(str(v.created_by), str(v.created_by))
        approver = names.get(approved_by.get(v.id, ""), approved_by.get(v.id, ""))
        poster = names.get(posted_by.get(v.id, ""), posted_by.get(v.id, ""))
        voucher_rows.append({
            "会计年度": v.voucher_date.year,
            "会计期间号": v.voucher_date.month,
            "记账凭证类型编号": _voucher_type_no(v.voucher_no),
            "记账凭证编号": v.voucher_no,
            "记账凭证日期": _ymd(v.voucher_date),
            "附件数": "0",
            "制单人": maker,
            "审核人": approver or "",
            "记账人": poster or "",
            "记账标志": "1",
        })
        for ln in sorted(v.lines, key=lambda x: x.line_no):
            acc = next((a for a in accounts if a.id == ln.account_id), None)
            code = acc.code if acc else "?"
            entry = {
                "会计年度": v.voucher_date.year,
                "会计期间号": v.voucher_date.month,
                "记账凭证编号": v.voucher_no,
                "记账凭证行号": ln.line_no,
                "记账凭证摘要": ln.summary or (v.summary or ""),
                "科目编号": code,
                "借方本币金额": _fmt(ln.debit),
                "贷方本币金额": _fmt(ln.credit),
                "辅助项1编号": "",
                "计量单位": ln.unit or "",
                "单价": "",
                "数量": _fmt(ln.quantity) if ln.quantity is not None else "",
            }
            # 辅助核算扁平化：把 aux_dims 的值依次填入辅助项N编号
            if ln.aux_dims:
                vals = [str(x) for x in ln.aux_dims.values() if x not in (None, "")]
                if vals:
                    entry["辅助项1编号"] = vals[0]
            # 单价 = 金额 / 数量
            if ln.quantity:
                q = Decimal(str(ln.quantity))
                amt = ln.debit if ln.debit > ZERO else ln.credit
                if q != ZERO:
                    entry["单价"] = _fmt(amt / q)
            entry_rows.append(entry)

    # —— 币种 ——
    currency_rows = [{
        "币种编码": func_ccy,
        "币种名称": CURRENCY_NAMES.get(func_ccy, func_ccy),
        "记账本位币标志": "1",
    }]
    for c in sorted(fx_codes):
        currency_rows.append({
            "币种编码": c,
            "币种名称": CURRENCY_NAMES.get(c, c),
            "记账本位币标志": "0",
        })

    # —— 会计期间 ——
    period_rows = [{
        "会计年度": p.year,
        "会计期间号": p.month,
        "开始日期": _ymd(date(p.year, p.month, 1)),
        "结束日期": _ymd(_period_end(p.year, p.month)),
        "期间状态": p.status,
    } for p in sorted(scope_periods, key=lambda x: (x.year, x.month))]

    # —— 会计科目 ——
    account_rows = [{
        "科目编号": a.code,
        "科目名称": a.name,
        "科目类型": CATEGORY_MAP.get(a.category, a.category),
        "余额方向": DIRECTION_MAP.get(a.direction, a.direction),
        "是否末级": "1" if a.is_leaf else "0",
        "辅助核算": ",".join(a.aux_dim_defs) if a.aux_dim_defs else "",
    } for a in accounts]

    # —— 电子账簿 + provenance ——
    events = session.scalars(
        select(Event).where(Event.ledger_set_id == ls.id)
        .order_by(Event.id)
    ).all()
    chain_tail = events[-1].hash if events else "0" * 64
    account_book_rows = [{
        "会计软件名称": "XErp",
        "会计软件版本": "0.1.0",
        "账簿名称": ls.name,
        "记账本位币": func_ccy,
        "会计口径": ls.accounting_standard or "small_business",
        "数据期间起始": f"{span_start[0]}{span_start[1]:02d}",
        "数据期间终止": f"{span_end[0]}{span_end[1]:02d}",
        "导出时间": _ymd_date_only(),
        "数据接口标准": "GB/T 24589.1-2024",
        "事件总数": len(events),
        "链尾哈希": chain_tail,
    }]

    tables = {
        "account_book": account_book_rows,
        "accounting_period": period_rows,
        "chart_of_accounts": account_rows,
        "currency": currency_rows,
        "account_balance": balance_rows,
        "voucher": voucher_rows,
        "voucher_entry": entry_rows,
    }

    if fmt == "json":
        return _to_json(tables)
    return _to_xml(tables)


def _period_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    nxt = date(year, month + 1, 1)
    # 上月最后一天：用当月首日减一天
    from datetime import timedelta
    return nxt - timedelta(days=1)


def _ymd_date_only() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y%m%d")


def _to_json(tables: dict) -> str:
    payload = {
        "standard": "GB/T 24589.1-2024",
        "tables": {},
    }
    for key, rows in tables.items():
        spec = TABLE_SPECS[key]
        payload["tables"][spec["name"]] = {
            "code": spec["code"],
            "fields": [{"code": c, "name": n} for c, n in spec["fields"]],
            "records": rows,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _to_xml(tables: dict) -> str:
    root = ET.Element("GB_T_24589_1_2024")
    for key, rows in tables.items():
        spec = TABLE_SPECS[key]
        t_el = ET.SubElement(root, spec["name"], {"标准编码": spec["code"]})
        fields = [n for _, n in spec["fields"]]
        for row in rows:
            r_el = ET.SubElement(t_el, "记录")
            for f in fields:
                v = row.get(f, "")
                if isinstance(v, (int,)):
                    v = str(v)
                ET.SubElement(r_el, f).text = "" if v is None else str(v)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


# ----------------------------- CLI -----------------------------

def _db_url(args) -> str:
    import os
    return args.db or os.environ.get("XERP_DB") or \
        f"sqlite:///{os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ledgeros_dev.db')}"


def _cli_export(session, args) -> int:
    out = build_export(
        session,
        ledger_set_id=args.ledger_set,
        year=args.year,
        month=args.month or 0,
        fmt=args.fmt,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"[gbt24589] 已导出 {args.fmt} → {args.out}", file=__import__("sys").stderr)
    else:
        print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from kernel.db.base import Base

    parser = argparse.ArgumentParser(prog="kernel.gbt24589", description="GB/T 24589.1-2024 导出")
    parser.add_argument("--db", default=None)
    parser.add_argument("ledger_set", help="账套 id")
    parser.add_argument("year", type=int, help="会计年度")
    parser.add_argument("--month", type=int, default=0, help="0=全年，非0=仅该月")
    parser.add_argument("--fmt", default="json", choices=["json", "xml"])
    parser.add_argument("--out", default=None, help="输出文件，缺省打印到 stdout")
    args = parser.parse_args(argv)

    engine = create_engine(_db_url(args))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        try:
            return _cli_export(session, args)
        except GbtError as e:
            print(f"GB/T 24589 导出失败：{e}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())

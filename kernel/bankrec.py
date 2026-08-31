"""银行对账（P2-04）：流水导入 → 自动勾对 → 未达账项报告。

存储哲学与账本一致——**流水也是事件**：
- 流水以 ``bank.txn.imported`` 事件落链（append-only，流水号为幂等键，
  同一流水号重复导入直接跳过），零新表零迁移；
- 勾对结果以 ``bank.txn.matched`` 事件落链（payload 含流水事件 id 与凭证 id），
  任何人可回放「这条银行流水对应哪张凭证」。

勾对算法（诚实版，一对一贪心）：
金额相等 + 方向一致 + 日期差最小者优先；已匹配条目退出候选池。
不做一对多/多对一拆分合并——那是清分引擎的事，这里宁可留在未达账项里
让人工处理，也不做「看起来自动其实错配」的聪明事。
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account, Event, Voucher, VoucherLine

ZERO = Decimal("0.00")
# 勾对默认银行科目（基本户）
DEFAULT_BANK_ACCOUNT = "100201"
# 账侧日期与流水日期的最大间隔（天）
MATCH_WINDOW_DAYS = 15


class BankRecError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _parse_date(raw: str, row_no: int) -> date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise BankRecError(
            "BAD_DATE", f"第 {row_no} 行日期无法解析：{raw!r}"
        ) from None


def _parse_amount(raw: str, row_no: int) -> Decimal:
    try:
        d = Decimal((raw or "0").strip().replace(",", ""))
    except InvalidOperation as exc:
        raise BankRecError(
            "BAD_AMOUNT", f"第 {row_no} 行金额非法：{raw!r}"
        ) from exc
    if not d.is_finite():
        raise BankRecError("BAD_AMOUNT", f"第 {row_no} 行金额非有限数：{raw!r}")
    return d.quantize(Decimal("0.01"))


def import_csv(
    session: Session,
    *,
    ledger_set_id: str,
    csv_text: str,
    actor: dict,
) -> dict:
    """导入银行流水 CSV（表头：date,amount,counterparty,summary,txn_id）。

    amount 正 = 银行收到（企业存款增加），负 = 银行支出。
    txn_id 缺省按行内容生成序号；重复流水号跳过（幂等）。
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    required = {"date", "amount"}
    if not reader.fieldnames or not required.issubset({f.strip() for f in reader.fieldnames}):
        raise BankRecError(
            "BAD_CSV_HEADER",
            "CSV 表头须包含 date 与 amount 列（可选 counterparty/summary/txn_id）",
            {"got": reader.fieldnames},
        )

    imported = skipped = 0
    seen_in_file: set[str] = set()
    for row_no, row in enumerate(reader, start=2):
        txn_id = (row.get("txn_id") or "").strip() or f"ROW-{row_no:04d}"
        if txn_id in seen_in_file:
            raise BankRecError("DUP_IN_FILE", f"文件内重复流水号：{txn_id}")
        seen_in_file.add(txn_id)
        if session.scalars(
            select(Event).where(
                Event.event_type == "bank.txn.imported",
                Event.aggregate_id == txn_id,
            )
        ).first():
            skipped += 1
            continue
        append = {
            "date": _parse_date(row.get("date", ""), row_no).isoformat(),
            "amount": str(_parse_amount(row.get("amount", ""), row_no)),
            "counterparty": (row.get("counterparty") or "").strip(),
            "summary": (row.get("summary") or "").strip(),
        }
        append_event_txn(session, ledger_set_id, txn_id, append, actor)
        imported += 1
    session.flush()
    return {"imported": imported, "skipped": skipped}


def append_event_txn(
    session: Session, ledger_set_id: str, txn_id: str, payload: dict, actor: dict
) -> None:
    from kernel.ledger import append_event

    append_event(
        session,
        ledger_set_id=ledger_set_id,
        event_type="bank.txn.imported",
        aggregate_id=txn_id,
        payload=payload,
        actor=actor,
    )


def _load_txns(session: Session, ledger_set_id: str) -> list[dict]:
    """已导入且未勾对的流水（按事件流回放）。"""
    matched_ids = {
        (e.payload or {}).get("txn_event_id")
        for e in session.scalars(
            select(Event).where(
                Event.ledger_set_id == ledger_set_id,
                Event.event_type == "bank.txn.matched",
            )
        )
    }
    out = []
    for e in session.scalars(
        select(Event).where(
            Event.ledger_set_id == ledger_set_id,
            Event.event_type == "bank.txn.imported",
        ).order_by(Event.id)
    ):
        if e.id in matched_ids:
            continue
        p = e.payload or {}
        out.append({
            "txn_event_id": e.id,
            "txn_id": e.aggregate_id,
            "date": p.get("date", ""),
            "amount": Decimal(p.get("amount", "0")),
            "counterparty": p.get("counterparty", ""),
            "summary": p.get("summary", ""),
        })
    return out


def _load_book_entries(
    session: Session, ledger_set_id: str, bank_code: str
) -> list[dict]:
    """账侧银行条目：已 POSTED 凭证中银行科目的分录（含 PUSHED/APPROVED 在途可选）。"""
    acc = session.scalars(
        select(Account).where(
            Account.ledger_set_id == ledger_set_id, Account.code == bank_code
        )
    ).first()
    if acc is None:
        raise BankRecError(
            "BANK_ACCOUNT_MISSING",
            f"账套缺少银行科目 {bank_code}",
            {"bank_code": bank_code},
        )
    rows = session.execute(
        select(VoucherLine, Voucher)
        .join(Voucher, VoucherLine.voucher_id == Voucher.id)
        .where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.status.in_(("PUSHED", "APPROVED", "POSTED")),
            VoucherLine.account_id == acc.id,
            # 期初导入/期末结转是系统规则凭证，不是银行交易，不参与勾对
            ~Voucher.voucher_no.like("期初-%"),
            ~Voucher.voucher_no.like("结转-%"),
        )
        .order_by(Voucher.voucher_date)
    ).all()
    entries = []
    for ln, v in rows:
        debit = Decimal(str(ln.debit))
        credit = Decimal(str(ln.credit))
        if debit == ZERO and credit == ZERO:
            continue
        entries.append({
            "voucher_id": v.id,
            "voucher_no": v.voucher_no,
            "date": v.voucher_date.isoformat(),
            "amount": debit - credit,   # 借（存款增加）为正
            "summary": v.summary or "",
            "line_id": ln.id,
        })
    return entries


def reconcile(
    session: Session,
    *,
    ledger_set_id: str,
    actor: dict,
    bank_code: str = DEFAULT_BANK_ACCOUNT,
    window_days: int = MATCH_WINDOW_DAYS,
    persist: bool = True,
) -> dict[str, Any]:
    """自动勾对并输出未达账项报告。

    persist=False 时只试算不落 matched 事件（供预览）。
    """
    txns = _load_txns(session, ledger_set_id)
    entries = _load_book_entries(session, ledger_set_id, bank_code)

    used_entries: set[int] = set()
    matched: list[dict] = []
    bank_only: list[dict] = []

    # 贪心：对每条流水找 金额相等 + 日期窗口内 的最小日期差账侧条目
    for t in txns:
        best_idx, best_gap = None, None
        for idx, e in enumerate(entries):
            if idx in used_entries or e["amount"] != t["amount"]:
                continue
            gap = abs((date.fromisoformat(e["date"])
                       - date.fromisoformat(t["date"])).days)
            if gap <= window_days and (best_gap is None or gap < best_gap):
                best_idx, best_gap = idx, gap
        if best_idx is None:
            bank_only.append(t)
        else:
            used_entries.add(best_idx)
            e = entries[best_idx]
            matched.append({
                "txn_id": t["txn_id"],
                "txn_event_id": t["txn_event_id"],
                "voucher_no": e["voucher_no"],
                "amount": f"{t['amount']:,.2f}",
                "date_gap_days": best_gap,
            })

    book_only = [entries[i] for i in range(len(entries)) if i not in used_entries]

    if persist:
        from kernel.ledger import append_event

        for m in matched:
            append_event(
                session,
                ledger_set_id=ledger_set_id,
                event_type="bank.txn.matched",
                aggregate_id=m["txn_id"],
                payload={
                    "txn_event_id": m["txn_event_id"],
                    "voucher_no": m["voucher_no"],
                    "amount": m["amount"],
                    "date_gap_days": m["date_gap_days"],
                },
                actor=actor,
            )
        session.flush()

    def _fmt_amount(d: Decimal) -> str:
        return f"{d.quantize(Decimal('0.01')):,.2f}"

    return {
        "bank_account": bank_code,
        "matched": matched,
        "bank_only": [
            {"txn_id": t["txn_id"], "date": t["date"],
             "amount": _fmt_amount(t["amount"]),
             "counterparty": t["counterparty"], "summary": t["summary"]}
            for t in bank_only
        ],
        "book_only": [
            {"voucher_no": e["voucher_no"], "date": e["date"],
             "amount": _fmt_amount(e["amount"]), "summary": e["summary"]}
            for e in book_only
        ],
        "summary": {
            "matched_count": len(matched),
            "bank_only_count": len(bank_only),   # 银行已收付、企业未记账
            "book_only_count": len(book_only),   # 企业已记账、银行未到账（在途）
        },
        "note": "一对多/多对一流水不做自动拆分，留在未达账项人工处理",
    }

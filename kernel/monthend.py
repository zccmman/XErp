"""关账 Agent（P3-01）：月度结账的无人值守编排。

流程（每步产出都进报告与事件流，可回放）：

1. **检查**——期间状态、未审凭证分布、账账核对（四项校验）；
2. **催办**——DRAFT 未提交 / PUSHED 未审批 清单 → 通知渠道（可插拔）；
3. **结转**——close_period（系统规则执行，POSTED，沿用 P1-02 口径）；
4. **试算**——结转后再次账账核对 + 试算平衡；
5. **报表草稿**——三大报表（P1-01 投影，标注「草稿待审」）；
6. **开下期**——open_next_period（幂等）。

分层原则：**本模块只提供单次执行入口，调度归外部 cron / 任务计划**。
Agent 永不自动过账未审凭证——未审的留在未审，催办而非代劳；
凭证状态机的人工闸门（HITL）不因自动化而绕过。

dry_run=True 时只做 1-2 步（检查+催办），不动账。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.bankrec import BankRecError
from kernel.bankrec import reconcile as bank_reconcile
from kernel.carryforward import open_next_period
from kernel.closing import close_period
from kernel.db.models import Period, Voucher
from kernel.events import E
from kernel.ledger import append_event
from kernel.posting import PostingError
from kernel.reconcile import reconcile_ledger
from kernel.reporting.statements import balance_sheet, cash_flow, income_statement

ZERO = Decimal("0.00")


class Notifier(Protocol):
    """催办通知渠道协议：飞书 webhook / 邮件 / 控制台均可实现。"""

    def send(self, subject: str, body: str) -> None: ...


class ConsoleNotifier:
    """默认渠道：打印到 stdout（无人值守时落日志文件）。"""

    def send(self, subject: str, body: str) -> None:
        print(f"[月度关账 Agent] {subject}\n{body}")


class MonthendError(ValueError):
    def __init__(self, code: str, message_zh: str, details: dict | None = None):
        super().__init__(message_zh)
        self.code = code
        self.message_zh = message_zh
        self.details = details or {}


def _pick_period(session: Session, ledger_set_id: str, year: int, month: int) -> Period:
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year, Period.month == month,
        )
    ).first()
    if period is None:
        raise MonthendError(
            "PERIOD_NOT_FOUND",
            f"账套不存在 {year}-{month:02d} 期间",
            {"year": year, "month": month},
        )
    return period


def _pending_review(session: Session, ledger_set_id: str,
                    period_id: str) -> dict[str, list[dict]]:
    """未审凭证分组：draft（未提交）/ pushed（待人审批）。"""
    groups: dict[str, list[dict]] = {"draft": [], "pushed": [], "approved": []}
    for v in session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id, Voucher.period_id == period_id
        )
    ):
        groups.setdefault(v.status.lower(), []).append({
            "voucher_no": v.voucher_no,
            "status": v.status,
            "summary": v.summary or "",
        })
    return groups


def run_monthend(
    session: Session,
    *,
    ledger_set_id: str,
    year: int,
    month: int,
    actor: dict,
    notifier: Notifier | None = None,
    dry_run: bool = False,
    bank_code: str = "100201",
) -> dict[str, Any]:
    """执行月度关账编排。dry_run 只检查+催办，不动账。"""
    notifier = notifier or ConsoleNotifier()
    report: dict[str, Any] = {
        "ledger_set_id": ledger_set_id,
        "period": {"year": year, "month": month},
        "dry_run": dry_run,
        "steps": {},
    }

    def step(name: str, payload: Any) -> None:
        report["steps"][name] = payload

    # ---------- 1. 检查 ----------
    period = _pick_period(session, ledger_set_id, year, month)
    if period.status != "OPEN":
        raise MonthendError(
            "PERIOD_NOT_OPEN",
            f"期间 {year}-{month:02d} 状态为 {period.status}，仅 OPEN 期间可关账",
        )
    pending = _pending_review(session, ledger_set_id, period.id)
    try:
        pre_rec = reconcile_ledger(session, ledger_set_id, year, month)
        pre_ok = pre_rec["ok"]
        pre_issues = pre_rec["issues"]
    except Exception as e:  # noqa: BLE001
        pre_ok, pre_issues = False, [{"kind": "RECONCILE_CRASH", "detail": str(e)}]
    step("check", {
        "status_counts": {k: len(v) for k, v in pending.items() if v},
        "reconcile_ok": pre_ok,
        "reconcile_issues": pre_issues,
    })

    # ---------- 2. 催办 ----------
    needs_chase = pending["draft"] + pending["pushed"]
    chase_body = "\n".join(
        f"  [{v['status']}] {v['voucher_no']} {v['summary']}" for v in needs_chase
    ) or "  （无）"
    if needs_chase and not dry_run:
        notifier.send(
            f"【XErp 月度关账催办】{year}-{month:02d} 有 {len(needs_chase)} 张凭证待处理",
            "以下凭证尚未完成审批流，请及时处理（Agent 不会代为审批）：\n" + chase_body,
        )
    step("chase", {
        "pending_count": len(needs_chase),
        "items": needs_chase,
        "notified": bool(needs_chase) and not dry_run,
    })

    if dry_run:
        report["conclusion"] = (
            f"dry-run：发现 {len(needs_chase)} 张待处理凭证，"
            f"账账核对{'通过' if pre_ok else '存在问题 ' + str(len(pre_issues)) + ' 项'}"
            "（dry-run 未动账）"
        )
        return report

    # 关账闸门：存在未审凭证时中止（催办已发），不强制结转
    if needs_chase:
        raise MonthendError(
            "PENDING_VOUCHERS",
            f"期间 {year}-{month:02d} 尚有 {len(needs_chase)} 张凭证未完成审批，"
            "已发送催办；处理完毕后重新执行关账",
            {"pending": needs_chase},
        )
    if not pre_ok:
        raise MonthendError(
            "RECONCILE_FAILED",
            "关账前账账核对未通过，请先处理对账问题",
            {"issues": pre_issues},
        )

    # ---------- 3. 结转 ----------
    try:
        closing_v = close_period(session, ledger_set_id=ledger_set_id,
                                 year=year, month=month, actor=actor)
        step("closing", {"voucher_no": closing_v.voucher_no,
                         "status": closing_v.status})
    except PostingError as e:
        if e.code == "ALREADY_CLOSED":
            step("closing", {"voucher_no": None, "status": "ALREADY_CLOSED"})
        else:
            raise MonthendError(e.code, e.message_zh, e.details) from e

    # ---------- 4. 试算（结转后复核） ----------
    post_rec = reconcile_ledger(session, ledger_set_id, year, month)
    step("trial_balance", {"reconcile_ok": post_rec["ok"],
                           "issues": post_rec["issues"]})
    if not post_rec["ok"]:
        raise MonthendError(
            "POST_CLOSING_UNBALANCED",
            "结转后账账核对未通过——结转引入了不平衡，需立即人工检查",
            {"issues": post_rec["issues"]},
        )
    try:
        bank = bank_reconcile(session, ledger_set_id=ledger_set_id,
                              actor=actor, bank_code=bank_code, persist=False)
        step("bank_reconcile", bank["summary"])
    except BankRecError:
        step("bank_reconcile", {"note": "银行科目缺失，跳过对账"})

    # ---------- 5. 报表草稿（草稿=投影输出，非正式披露） ----------
    bs = balance_sheet(session, ledger_set_id, year, month)
    inc = income_statement(session, ledger_set_id, year, month)
    cf = cash_flow(session, ledger_set_id, year, month)

    def _d(x: Decimal) -> str:
        return f"{x.quantize(Decimal('0.01')):,.2f}"

    step("reports_draft", {
        "balance_sheet": {
            "assets_total": _d(bs["assets"]["total"]),
            "liabilities_total": _d(bs["liabilities"]["total"]),
            "equity_total": _d(bs["equity"]["total"]),
            "balanced": bs["balanced"],
        },
        "income_statement": {
            "net_profit": _d(inc["net_profit"]),
        },
        "cash_flow": {
            "net_increase": _d(cf["net_increase"]),
            "reconciled": cf["reconcile"],
        },
        "note": "草稿待审：正式报表以人工复核后的数据为准",
    })

    # ---------- 6. 开下期 ----------
    try:
        next_v = open_next_period(session, ledger_set_id=ledger_set_id,
                                  year=year, month=month, actor=actor)
        step("open_next", {"voucher_no": next_v.voucher_no})
    except PostingError as e:
        if e.code == "ALREADY_OPENED":
            step("open_next", {"voucher_no": None, "status": "ALREADY_OPENED"})
        else:
            raise MonthendError(e.code, e.message_zh, e.details) from e

    # ---------- 全程事件 ----------
    append_event(
        session, ledger_set_id=ledger_set_id,
        event_type=E.AGENT_MONTHEND_RUN, aggregate_id=f"{year}{month:02d}",
        payload={
            "steps": list(report["steps"].keys()),
            "dry_run": dry_run,
            "pending_at_start": len(needs_chase),
            "net_profit": report["steps"].get("reports_draft", {})
            .get("income_statement", {}).get("net_profit"),
        },
        actor=actor,
    )
    session.flush()

    report["conclusion"] = (
        f"{year}-{month:02d} 关账完成：结转/试算/报表草稿/开下期 全部就绪，"
        "产出均为草稿待人审"
    )
    return report

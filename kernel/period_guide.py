"""账套状态引导（超级AI总账 · 阶段0）：把内核状态"讲给人听"。

单一职责：读某账套某期间的真实状态，产出一份**新人能照着做的中文引导**——
"你现在处于哪个阶段、下一步该干嘛、还差哪几项"，而不是一堆孤立的状态码。

为什么需要它（区别于 precheck_close）：
    precheck_close 是「结账前体检」，只回答"能不能结账"，它隐含假设账里已经有数据。
    但一个刚建账、本期还没记过任何凭证的账套，precheck 会给出"损益尚未结转、不能结账"
    的误导结论——可它根本没东西可结。
    month_end_guide 在 precheck 之上加了一层**阶段推断**，避免这种误导：
        没记过账 → 引导去记账/录期初，而不是催结转；
        有已记账但未处理完 → 先处理待审批/待记账凭证；
        都处理完 → 才落到 precheck 的结账闸门。

确定性铁律：本模块**只读、不写、不抛账务错误**，全部结论来自
Voucher/Period 真实状态 + 复用 precheck_close；不引入任何新账务真源，
也不编造"该做什么"之外的业务建议。AI 引导层只消费它的输出，不做二次发明。
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.classic import period_zh, precheck_close, status_zh
from kernel.db.models import Period, Voucher
from kernel.opening import OPENING_PREFIX

#: 各非终态对应的中文动作提示（"下一步该谁动"）。
_STATE_ACTION: dict[str, str] = {
    "DRAFT": "草稿待补全/待提交，先去制单页面把草稿补齐并推送审核",
    "PUSHED": "待审核，需由非制单人的审批人处理",
    "APPROVED": "已审核待记账，执行过账完成入账",
}

#: 引导阶段的稳定枚举（机器可读，供上层做跳转/样式）。
PHASE_NONE = "no_period"
PHASE_CLOSED = "closed"
PHASE_EMPTY = "empty"           # 本期还没记过任何凭证（可能也没录期初）
PHASE_DAILY = "daily_pending"   # 有已记账，但还有未处理完的凭证
PHASE_CLOSING = "closing_pending"  # 凭证都处理完了，进入结账闸门（含可结账/差几项）
PHASE_CLOSING_READY = "closing_ready"  # 结账条件已满足

_PHASE_ZH: dict[str, str] = {
    PHASE_NONE: "本期尚未建立",
    PHASE_CLOSED: "本期已结账",
    PHASE_EMPTY: "本期尚无记账，先录期初或开始记账",
    PHASE_DAILY: "本期还有凭证未处理完",
    PHASE_CLOSING: "凭证已处理完，进入期末结账流程",
    PHASE_CLOSING_READY: "凭证已处理完且结账条件满足",
}


def _phase_zh(phase: str) -> str:
    return _PHASE_ZH.get(phase, phase)


#: 月末结账向导卡的固定步骤序列（机器可读，Web / MCP 同源共享）。
GUIDE_STEPS: list[tuple[str, str]] = [
    ("open_period", "建立本期"),
    ("opening", "录入期初余额"),
    ("daily", "日常记账"),
    ("clear_pending", "处理待办凭证"),
    ("close", "月末结账（含损益结转）"),
]

_STATUS_ZH = {"done": "已完成", "active": "下一步", "blocked": "受阻", "pending": "未开始"}


def _step(key, label, status, detail, actions=None, gates=None):
    return {
        "key": key,
        "label": label,
        "status": status,
        "status_zh": _STATUS_ZH.get(status, status),
        "detail": detail,
        "actions": actions or [],
        "gates": gates,
    }


def _build_steps(*, has_period, period_status, has_opening, counts, close, ls_id):
    """构造月末结账向导卡的步骤状态机（确定性、只读）。

    每个步骤给出 status(done/active/blocked/pending) + 中文 detail + 可选动作
    （Web 直链、MCP 给 AI 指路），Web 与 MCP 消费同一份，口径永远一致。
    """
    if not has_period:
        return [
            _step("open_period", "建立本期", "blocked",
                  "本期会计期间尚未建立，需初始化期间（首次建账）后再记账。"),
            _step("opening", "录入期初余额", "pending", "待建立本期后再录入。"),
            _step("daily", "日常记账", "pending", "待建立本期后再记账。"),
            _step("clear_pending", "处理待办凭证", "pending", "待建立本期。"),
            _step("close", "月末结账（含损益结转）", "pending", "待建立本期。"),
        ]

    if period_status == "CLOSED":
        # 已结账期间：结账即终点，所有步骤视为完成。
        return [
            _step("open_period", "建立本期", "done", "本期已结账。"),
            _step("opening", "录入期初余额", "done", "本期已结账。"),
            _step("daily", "日常记账", "done", "本期已结账。"),
            _step("clear_pending", "处理待办凭证", "done", "本期已结账。"),
            _step("close", "月末结账（含损益结转）", "done", "本期已结账。"),
        ]

    posted = counts.get("posted", 0)
    draft = counts.get("draft", 0)
    pushed = counts.get("pushed", 0)
    approved = counts.get("approved", 0)
    unfinished_n = draft + pushed + approved

    steps = [
        _step("open_period", "建立本期", "done", "本期已开账，可正常记账。"),
    ]

    if has_opening or posted > 0:
        steps.append(_step("opening", "录入期初余额", "done",
                           "期初已就绪（已录期初或已有业务凭证）。"))
    else:
        steps.append(_step("opening", "录入期初余额", "active",
                           "尚未录入期初余额，建议先录入再开始记账。",
                           [{"label": "导入期初余额",
                             "href": f"/ledger/{ls_id}#opening"}]))

    if posted > 0:
        steps.append(_step("daily", "日常记账", "done", f"已记账 {posted} 张。"))
    elif has_opening:
        steps.append(_step("daily", "日常记账", "active",
                           "本期尚无业务凭证，请开始日常记账。",
                           [{"label": "用业务向导记账",
                             "href": f"/ledger/{ls_id}/wizard"},
                            {"label": "继续制单",
                             "href": f"/ledger/{ls_id}/voucher/new"}]))
    else:
        steps.append(_step("daily", "日常记账", "pending", "待录入期初后再开始记账。"))

    if posted == 0:
        steps.append(_step("clear_pending", "处理待办凭证", "pending", "先完成日常记账。"))
    elif unfinished_n > 0:
        steps.append(_step("clear_pending", "处理待办凭证", "blocked",
                           f"还有 {unfinished_n} 张凭证未处理完：未审核草稿 {draft} 张 · "
                           f"待审核 {pushed} 张 · 已审待记账 {approved} 张 · "
                           f"已记账 {posted} 张。",
                           [{"label": "去审批待办", "href": "/todo"},
                            {"label": "继续制单",
                             "href": f"/ledger/{ls_id}/voucher/new"}]))
    else:
        steps.append(_step("clear_pending", "处理待办凭证", "done", "所有凭证均已记账，无待办。"))

    if period_status == "CLOSED":
        steps.append(_step("close", "月末结账（含损益结转）", "done", "本期已结账。"))
    elif posted == 0 or unfinished_n > 0:
        steps.append(_step("close", "月末结账（含损益结转）", "pending",
                           "完成前面步骤后再做月末结账。"))
    else:
        checks = (close or {}).get("checks")
        if close and close.get("can_close"):
            steps.append(_step("close", "月末结账（含损益结转）", "active",
                               "结账条件已满足，可执行月末结账（含损益结转）。",
                               [{"label": "去月末结账",
                                 "href": f"/ledger/{ls_id}/close"}],
                               gates=checks))
        else:
            failed = [c for c in (checks or []) if not c["passed"]]
            steps.append(_step("close", "月末结账（含损益结转）", "blocked",
                               "结账前还差 "
                               + (f"{len(failed)} 项："
                                  + "；".join(c["item"] for c in failed)
                                  if failed else "若干项，请查看结账体检。"),
                               [{"label": "查看结账体检",
                                 "href": f"/ledger/{ls_id}/close"}],
                               gates=checks))
    return steps


def month_end_guide(
    session: Session,
    *,
    ledger_set_id: str,
    year: int,
    month: int,
) -> dict:
    """生成某账套某期间的中文分阶段引导。

    返回（全部确定性，无副作用）：
        {year, month, period_status_zh, phase, phase_zh,
         counts:{draft,pushed,approved,posted,total},
         next_action,   # 一句话：现在最该做的一件事
         close}         # precheck_close 结果（期间存在且有凭证处理完时才有意义）
    """
    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()

    if period is None:
        return {
            "year": year, "month": month,
            "period_status_zh": "期间不存在",
            "phase": PHASE_NONE,
            "phase_zh": _phase_zh(PHASE_NONE),
            "counts": {},
            "next_action": f"{year}-{month:02d} 会计期间尚未建立，请先初始化期间（首次建账）再开始记账。",
            "close": None,
            "steps": _build_steps(has_period=False, period_status=None,
                                  has_opening=False, counts={}, close=None,
                                  ls_id=ledger_set_id),
        }

    if period.status != "OPEN":
        _v = session.scalars(
            select(Voucher).where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.period_id == period.id,
            )
        ).all()
        _c = Counter(v.status for v in _v)
        _counts = {"draft": _c.get("DRAFT", 0), "pushed": _c.get("PUSHED", 0),
                   "approved": _c.get("APPROVED", 0), "posted": _c.get("POSTED", 0),
                   "total": len(_v)}
        _opening = bool(
            session.scalars(
                select(Voucher.id).where(
                    Voucher.ledger_set_id == ledger_set_id,
                    Voucher.period_id == period.id,
                    Voucher.voucher_no.like(OPENING_PREFIX + "%"),
                )
            ).first()
        )
        return {
            "year": year, "month": month,
            "period_status_zh": period_zh(period.status),
            "phase": PHASE_CLOSED,
            "phase_zh": _phase_zh(PHASE_CLOSED),
            "counts": _counts,
            "next_action": (
                f"{year}-{month:02d} 已{period_zh(period.status)}，本期不再接受记账；"
                "需要新业务请打开下一期间。"
            ),
            "close": None,
            "steps": _build_steps(has_period=True, period_status=period.status,
                                  has_opening=_opening, counts=_counts,
                                  close=None, ls_id=ledger_set_id),
        }

    vouchers = session.scalars(
        select(Voucher).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.period_id == period.id,
        )
    ).all()
    counter = Counter(v.status for v in vouchers)
    counts = {
        "draft": counter.get("DRAFT", 0),
        "pushed": counter.get("PUSHED", 0),
        "approved": counter.get("APPROVED", 0),
        "posted": counter.get("POSTED", 0),
        "total": len(vouchers),
    }
    has_opening = bool(
        session.scalars(
            select(Voucher.id).where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.period_id == period.id,
                Voucher.voucher_no.like(OPENING_PREFIX + "%"),
            )
        ).first()
    )

    # 本期有已记账凭证才谈"期末收尾/结账"；一张都没记过 → 先记账/录期初，别催结转。
    if counts["posted"] == 0:
        hint = (
            "本期尚无任何记账。未发现期初凭证——请先录入期初余额"
            "（或直接开始日常记账）。"
            if not has_opening
            else "期初已录入但尚无业务凭证，请开始日常记账。"
        )
        return {
            "year": year, "month": month,
            "period_status_zh": period_zh(period.status),
            "phase": PHASE_EMPTY,
            "phase_zh": _phase_zh(PHASE_EMPTY),
            "counts": counts,
            "next_action": hint,
            "close": None,
            "steps": _build_steps(has_period=True, period_status="OPEN",
                                  has_opening=has_opening, counts=counts,
                                  close=None, ls_id=ledger_set_id),
        }

    # 有已记账凭证，但仍存在未处理完的（草稿/待审/待记账）→ 先收尾
    unfinished = [st for st, n in
                  (("DRAFT", counts["draft"]), ("PUSHED", counts["pushed"]),
                   ("APPROVED", counts["approved"])) if n > 0]
    if unfinished:
        parts = []
        for st in unfinished:
            n = counts[st.lower()]
            parts.append(f"{status_zh(st)} {n} 张（{_STATE_ACTION[st]}）")
        return {
            "year": year, "month": month,
            "period_status_zh": period_zh(period.status),
            "phase": PHASE_DAILY,
            "phase_zh": _phase_zh(PHASE_DAILY),
            "counts": counts,
            "next_action": "本月还有未处理完的凭证：" + "；".join(parts) + "。请先处理完再进入期末结账。",
            "close": None,
            "steps": _build_steps(has_period=True, period_status="OPEN",
                                  has_opening=has_opening, counts=counts,
                                  close=None, ls_id=ledger_set_id),
        }

    # 全部凭证已记账 → 进入期末结账闸门（复用 precheck_close，不重复造轮子）
    close = precheck_close(session, ledger_set_id=ledger_set_id, year=year, month=month)
    phase = PHASE_CLOSING_READY if close["can_close"] else PHASE_CLOSING
    return {
        "year": year, "month": month,
        "period_status_zh": period_zh(period.status),
        "phase": phase,
        "phase_zh": _phase_zh(phase),
        "counts": counts,
        "next_action": (
            "凭证已全部记账且结账条件满足，可以执行期末结转并结账了。"
            if close["can_close"]
            else f"凭证已全部记账，但结账前还差 {sum(1 for c in close['checks'] if not c['passed'])} 项："
                 + "；".join(f"{c['item']}（{c['hint'] or c['detail']}）"
                             for c in close["checks"] if not c["passed"])
        ),
        "close": close,
        "steps": _build_steps(has_period=True, period_status="OPEN",
                              has_opening=has_opening, counts=counts,
                              close=close, ls_id=ledger_set_id),
    }

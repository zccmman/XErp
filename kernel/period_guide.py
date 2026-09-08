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
        }

    if period.status != "OPEN":
        return {
            "year": year, "month": month,
            "period_status_zh": period_zh(period.status),
            "phase": PHASE_CLOSED,
            "phase_zh": _phase_zh(PHASE_CLOSED),
            "counts": {},
            "next_action": (
                f"{year}-{month:02d} 已{period_zh(period.status)}，本期不再接受记账；"
                "需要新业务请打开下一期间。"
            ),
            "close": None,
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

    # 本期有已记账凭证才谈"期末收尾/结账"；一张都没记过 → 先记账/录期初，别催结转。
    if counts["posted"] == 0:
        has_opening = bool(
            session.scalars(
                select(Voucher.id).where(
                    Voucher.ledger_set_id == ledger_set_id,
                    Voucher.period_id == period.id,
                    Voucher.voucher_no.like(OPENING_PREFIX + "%"),
                )
            ).first()
        )
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
    }

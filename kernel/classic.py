"""怀旧兼容层（classic mode）。

把经典桌面财务软件（下称"经典模式"）的交互惯例映射到 XErp 本体之上。

设计原则：**只翻译，不改造**。
状态枚举、事件链、账面数值一律保持原样，本模块只在接口层提供
「老会计读得懂」的符号与话术。因此它是纯新增的——关掉它系统照常工作，
不产生任何迁移成本，也不改变任何一张凭证的真实含义。

三件真正有用的事：
    1. 凭证分类编号：收 / 付 / 转（见 classify_voucher_type）
    2. 状态术语：POSTED →「已记账」等（见 STATUS_ZH）
    3. 结账体检清单：把"能不能结账"变成人能照做的待办（见 precheck_close）

刻意不做的：复刻界面、快捷键、报表版式。那是皮肤，不是肌肉记忆，
而且会拖累"极简易交付"这条底线。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

# ---------------------------------------------------------------- 1. 状态术语

#: 凭证状态 → 中文术语。取值刻意沿用经典财务软件的说法：
#: 老会计听到"已记账"三个字不需要任何解释，而"POSTED"需要。
STATUS_ZH: dict[str, str] = {
    "DRAFT": "未审核",
    "PUSHED": "待审核",
    "APPROVED": "已审核",
    "POSTED": "已记账",
    "REJECTED": "已驳回",
    "WITHDRAWN": "已撤回",
}

#: 期间状态 → 中文术语。
PERIOD_ZH: dict[str, str] = {
    "OPEN": "未结账",
    "CLOSING": "结账中",
    "CLOSED": "已结账",
}


def status_zh(status: str) -> str:
    """状态转中文术语。未登记的枚举原样返回，绝不静默吞掉新状态。"""
    return STATUS_ZH.get(status, status)


def period_zh(status: str) -> str:
    return PERIOD_ZH.get(status, status)


# ------------------------------------------------------------ 2. 凭证分类编号

#: 现金及现金等价物科目前缀。判断一笔业务是"收"还是"付"，看的就是
#: 现金流方向——这是分类编号的全部依据，不引入任何业务规则。
CASH_BANK_CODES: tuple[str, ...] = ("1001", "1002", "1012")

#: 分类编号前缀。None 表示统一编号（记-）。
TYPE_PREFIX: dict[str, str] = {
    "收": "收-",
    "付": "付-",
    "转": "转-",
}


def is_cash_bank(account_code: str) -> bool:
    """是否为现金/银行存款类科目（含下级明细科目，如 100201 工行存款）。"""
    code = (account_code or "").strip()
    return any(code.startswith(p) for p in CASH_BANK_CODES)


def classify_voucher_type(lines: list[dict]) -> str:
    """按资金流向判定凭证类别，返回 "收" / "付" / "转"。

    规则（与经典财务软件一致）：
        - 借方出现现金/银行科目  → 收款凭证（钱进来）
        - 贷方出现现金/银行科目  → 付款凭证（钱出去）
        - 两侧都没有            → 转账凭证
        - 两侧都有（如提现、银行互转）→ 付款凭证优先

    lines 形如 [{"account_code": "1001", "debit": "100", "credit": ""}]。
    金额允许字符串或数字，空值按 0 处理。
    """
    dr_hit = False
    cr_hit = False
    for ln in lines or []:
        code = str(ln.get("account_code") or "").strip()
        if not is_cash_bank(code):
            continue
        if _as_decimal(ln.get("debit")) > 0:
            dr_hit = True
        if _as_decimal(ln.get("credit")) > 0:
            cr_hit = True

    # 两侧都命中时归为"付"：实务中提现/内部划转习惯用付款凭证，
    # 且"付"比"收"更需要被单独盯住。
    if cr_hit:
        return "付"
    if dr_hit:
        return "收"
    return "转"


def voucher_prefix(voucher_type: str | None) -> str:
    """类别 → 编号前缀。None 或未知类别退化为统一编号 "记-"。"""
    if not voucher_type:
        return "记-"
    return TYPE_PREFIX.get(str(voucher_type).strip(), "记-")


# -------------------------------------------------------------- 3. 结账体检


def precheck_close(
    session,
    *,
    ledger_set_id: str,
    year: int,
    month: int,
) -> dict:
    """结账前体检，返回一份"照着做就能结账"的清单。

    经典财务软件的结账之所以有仪式感，不是因为点了什么按钮，而是因为
    它会**明确告诉你还差什么**。这里把四道闸门一次查完，逐条给出结论，
    而不是在第一个失败处抛异常让人来回试错。

    四道闸门：
        1. 上月是否已结账（会计期间必须连续闭合）
        2. 本月是否还有未记账凭证
        3. 本月试算是否平衡
        4. 本月损益是否已结转

    返回 {can_close, checks:[{item, passed, detail, hint}], summary}。
    """
    from kernel.db.models import Balance, Period, Voucher

    checks: list[dict] = []

    period = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == year,
            Period.month == month,
        )
    ).first()
    if period is None:
        return {
            "can_close": False,
            "checks": [
                {
                    "item": "期间存在",
                    "passed": False,
                    "detail": f"{year}-{month:02d} 期间不存在",
                    "hint": "请先初始化该会计期间",
                }
            ],
            "summary": f"{year}-{month:02d} 期间不存在，无法结账",
        }

    # 闸门 1：上月已结账
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    prev = session.scalars(
        select(Period).where(
            Period.ledger_set_id == ledger_set_id,
            Period.year == prev_year,
            Period.month == prev_month,
        )
    ).first()
    if prev is None:
        # 首期无上月，视为通过（建账首月不应被卡住）
        checks.append(
            {
                "item": "上月已结账",
                "passed": True,
                "detail": f"{prev_year}-{prev_month:02d} 无期间（建账首月）",
                "hint": "",
            }
        )
    else:
        ok = prev.status == "CLOSED"
        checks.append(
            {
                "item": "上月已结账",
                "passed": ok,
                "detail": f"{prev_year}-{prev_month:02d} 状态：{period_zh(prev.status)}",
                "hint": "" if ok else f"请先结账 {prev_year}-{prev_month:02d}",
            }
        )

    # 闸门 2：本月无未记账凭证
    vouchers = list(
        session.scalars(
            select(Voucher).where(
                Voucher.ledger_set_id == ledger_set_id,
                Voucher.period_id == period.id,
            )
        ).all()
    )
    unposted = [v for v in vouchers if v.status != "POSTED"]
    checks.append(
        {
            "item": "本月凭证已全部记账",
            "passed": not unposted,
            "detail": (
                f"共 {len(vouchers)} 张，未记账 {len(unposted)} 张"
                + (f"（{'、'.join(v.voucher_no for v in unposted[:5])}"
                   f"{'…' if len(unposted) > 5 else ''}）" if unposted else "")
            ),
            "hint": "" if not unposted else "先审核并记账上述凭证，或取消不需要的",
        }
    )

    # 闸门 3：试算平衡
    balances = list(
        session.scalars(select(Balance).where(Balance.period_id == period.id)).all()
    )
    dr = sum((Decimal(str(b.debit_total)) for b in balances), Decimal("0"))
    cr = sum((Decimal(str(b.credit_total)) for b in balances), Decimal("0"))
    balanced = dr == cr
    if not balances:
        # 空账时 0 == 0 恒真，直接打勾会给出虚假的安全感。
        # 必须说清"没有数据"和"数据平衡"是两回事。
        detail = "本月尚无已记账凭证，无发生额可试算"
        hint = "记账后该项才会给出真实结论"
    else:
        detail = f"借方合计 {dr} / 贷方合计 {cr}"
        hint = "" if balanced else f"借贷差额 {dr - cr}，请检查本月凭证"
    checks.append(
        {
            "item": "试算平衡",
            "passed": balanced,
            "detail": detail,
            "hint": hint,
        }
    )

    # 闸门 4：损益已结转
    cf_prefix = f"结转-{year}{month:02d}-"
    has_closing = session.scalars(
        select(Voucher.id).where(
            Voucher.ledger_set_id == ledger_set_id,
            Voucher.voucher_no.like(cf_prefix + "%"),
        )
    ).first()
    checks.append(
        {
            "item": "损益已结转",
            "passed": has_closing is not None,
            "detail": "已生成结转凭证" if has_closing else "尚未结转",
            "hint": "" if has_closing else "请先执行期末结转",
        }
    )

    failed = [c for c in checks if not c["passed"]]
    return {
        "can_close": not failed,
        "period": f"{year}-{month:02d}",
        "period_status_zh": period_zh(period.status),
        "checks": checks,
        "summary": (
            "结账条件已满足" if not failed
            else f"还有 {len(failed)} 项未完成：" + "；".join(
                f"{c['item']}（{c['detail']}）" for c in failed
            )
        ),
    }


# ------------------------------------------------------------ 4. 摘要记忆


def suggest_summaries(
    session,
    *,
    ledger_set_id: str,
    account_code: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """常用摘要推荐：统计该科目历史上用得最多的摘要。

    经典财务软件里这是个 F2 快捷键——老会计不重新打字，只从常用摘要里挑。
    这里**不加表不加缓存**，直接从既有凭证统计：摘要本来就是凭证的一部分，
    历史凭证就是最好的摘要库。
    """
    from kernel.db.models import Account, Voucher, VoucherLine

    # 明细行摘要优先，为空则回退到凭证头摘要——这正是老会计的习惯：
    # 制单时只在第一行写摘要，其余行留空、默认同头。
    # （create_draft_voucher 目前不往明细行写 summary，故必须回退，否则全空。）
    stmt = (
        select(Voucher.id, VoucherLine.summary, Voucher.summary)
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.ledger_set_id == ledger_set_id)
    )
    if account_code:
        acc = session.scalars(
            select(Account).where(
                Account.ledger_set_id == ledger_set_id,
                Account.code == str(account_code).strip(),
            )
        ).first()
        if acc is None:
            return []
        stmt = stmt.where(VoucherLine.account_id == acc.id)

    counter: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()  # (凭证, 摘要) —— 一张凭证对同一摘要只计一次
    for voucher_id, line_summary, voucher_summary in session.execute(stmt).all():
        key = str(line_summary or voucher_summary or "").strip()
        if not key:
            continue
        if (voucher_id, key) in seen:
            continue
        seen.add((voucher_id, key))
        counter[key] = counter.get(key, 0) + 1

    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[: max(0, limit)]
    return [{"summary": s, "used_count": n} for s, n in ranked]


# ------------------------------------------------------------------ 内部工具


def _as_decimal(value) -> Decimal:
    """金额归一：空值/None → 0，字符串/数字 → Decimal。解析失败按 0 处理。"""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")

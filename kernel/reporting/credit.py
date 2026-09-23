"""信用管理 + 智能催收（Phase B / G2 / G3）：只读敞口 + 催收草稿。

设计铁律（事件溯源 + ADR-002）：
- 授信额度是 Boss 对客户的配置（存 Party.credit_limit），**不是账本余额投影**，
  绝不新增会漂移的余额表；敞口完全由 open_items（凭证 + arap_clearing 重建）派生。
- credit_exposure / collections_draft 全部只读，不改账、不制单、不推送执行；
  仅输出「该催谁、催多少、额度还剩多少」给 Boss 确认（HITL）。
- 催收动作（发函/电话/法务）XErp 不代发——只产出草稿话术与待办清单，
  终态（外呼/发函）由 Boss 在 Web/IM 里人工执行。

与 AI Runtime 的接合点：credit_exposure / collections_draft 被 sprite_push
消费，成为账本精灵「主动提醒」的 credit / collections 项（推送 ≠ 执行）。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import Party
from kernel.reporting.arap import DEFAULT_BUCKETS, open_items

ZERO = Decimal("0")
NEAR_RATIO = Decimal("0.8")  # 利用率 ≥ 80% 预警（临近额度）


class CreditError(ValueError):
    """信用管理业务错误：信息可直接展示给用户。"""


def get_credit_limit(
    session, *, ledger_set_id: str, dim_key: str, partner: str
) -> Decimal:
    """取某往来单位授信额度（按 name 解析 Party）。无记录/无额度 → 0（不限）。"""
    party = session.scalars(
        select(Party).where(
            Party.ledger_set_id == ledger_set_id,
            Party.party_type == dim_key,
            Party.name == partner,
        )
    ).first()
    if party is None:
        return ZERO
    return Decimal(str(party.credit_limit or 0))


def set_credit_limit(
    session, *, ledger_set_id: str, dim_key: str, partner: str,
    limit: str | Decimal, actor: dict | None = None,
) -> dict:
    """设置/更新某往来单位授信额度（Boss 配置写；非账本写）。

    Party 不存在则按 (ledger_set_id, dim_key, name) 自动建一行，
    保证额度可持久化（AR 客户由 aux_dims 名驱动，未必有 Party 行）。
    调用方负责 commit（与 ingest_event 一致）。
    """
    try:
        lim = Decimal(str(limit))
    except (TypeError, ValueError):
        raise CreditError("BAD_LIMIT", f"授信额度非法：{limit}")
    if lim < ZERO:
        raise CreditError("BAD_LIMIT", "授信额度不能为负")
    party = session.scalars(
        select(Party).where(
            Party.ledger_set_id == ledger_set_id,
            Party.party_type == dim_key,
            Party.name == partner,
        )
    ).first()
    if party is None:
        party = Party(
            ledger_set_id=ledger_set_id, party_type=dim_key, name=partner,
        )
        session.add(party)
    party.credit_limit = lim
    session.flush()
    return {
        "partner": party.name, "dim_key": dim_key,
        "credit_limit": f"{party.credit_limit:.2f}",
    }


def credit_exposure(
    session, *, ledger_set_id: str, dim_key: str = "customer",
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """信用敞口扫描（只读）：按未清应收/应付聚合每个往来单位的敞口、额度、利用率、超额。

    敞口 = open_items 的未清金额合计（由凭证 + arap_clearing 重建，ADR-002）。
    超额 = 额度 > 0 且敞口 > 额度；临近 = 额度 > 0 且利用率 ≥ NEAR_RATIO。
    返回 breaches 列表供 sprite_push 推 alert。
    """
    oi = open_items(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key,
        as_of_date=as_of_date,
    )
    by_partner: dict[str, Decimal] = {}
    for it in oi.get("items", []):
        p = it["partner"]
        by_partner[p] = by_partner.get(p, ZERO) + Decimal(it["open_amount"])

    rows: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []
    total_exposure = ZERO
    total_limit = ZERO
    for p, exposure in sorted(by_partner.items(), key=lambda kv: (-kv[1], kv[0])):
        limit = get_credit_limit(
            session, ledger_set_id=ledger_set_id, dim_key=dim_key, partner=p
        )
        util = (exposure / limit) if limit > ZERO else None
        breach = limit > ZERO and exposure > limit
        near = (util is not None) and (util >= NEAR_RATIO)
        total_exposure += exposure
        total_limit += limit
        rows.append({
            "partner": p,
            "exposure": f"{exposure:.2f}",
            "credit_limit": f"{limit:.2f}",
            "utilization": (f"{util * 100:.1f}%" if util is not None else None),
            "breach": breach,
            "near_limit": near,
        })
        if breach:
            breaches.append({
                "partner": p, "exposure": f"{exposure:.2f}",
                "credit_limit": f"{limit:.2f}",
                "over_by": f"{(exposure - limit):.2f}",
            })
    total_util = (total_exposure / total_limit) if total_limit > ZERO else None
    return {
        "dim_key": dim_key,
        "as_of_date": oi.get("as_of_date"),
        "rows": rows,
        "breaches": breaches,
        "totals": {
            "exposure": f"{total_exposure:.2f}",
            "credit_limit": f"{total_limit:.2f}",
            "utilization": (f"{total_util * 100:.1f}%" if total_util is not None else None),
        },
        "basis": "敞口=未清项合计（open_items 由凭证+arap_clearing 重建）；额度=Party.credit_limit 配置",
    }


def collections_draft(
    session, *, ledger_set_id: str,
    as_of_date: date | None = None,
    bucket_days: tuple[int, ...] = DEFAULT_BUCKETS,
) -> dict[str, Any]:
    """催收草稿（只读）：逾期未清应收，按账龄升级 + 生成催收话术草稿。

    逾期定义：未清发票账龄 > bucket_days[0]（默认 >30 天，即超出常规信用期）。
    升级级别：30-60 → L1（提醒）；60-90 → L2（提醒+电话）；90+ → L3（最后通牒/法务）。
    催收话术仅为草稿文本，XErp 不代发——由 Boss 在 Web/IM 人工执行。
    """
    oi = open_items(
        session, ledger_set_id=ledger_set_id, dim_key="customer",
        as_of_date=as_of_date, bucket_days=bucket_days,
    )
    overdue_buckets = (
        f"b{bucket_days[0]}_{bucket_days[1]}",
        f"b{bucket_days[1]}_{bucket_days[2]}",
        f"b{bucket_days[2]}_plus",
    )
    by_partner: dict[str, list[dict]] = {}
    for it in oi.get("items", []):
        if it["bucket"] in overdue_buckets:
            by_partner.setdefault(it["partner"], []).append(it)

    rows: list[dict[str, Any]] = []
    counts = {"L1": 0, "L2": 0, "L3": 0}
    total_overdue = ZERO
    for p, items in sorted(
        by_partner.items(),
        key=lambda kv: -sum(Decimal(i["open_amount"]) for i in kv[1]),
    ):
        amt = sum(Decimal(i["open_amount"]) for i in items)
        oldest = max(int(i["days"]) for i in items)
        level = _escalation_level(oldest, bucket_days)
        counts[level] += 1
        total_overdue += amt
        rows.append({
            "partner": p,
            "overdue_amount": f"{amt:.2f}",
            "oldest_days": oldest,
            "level": level,
            "items": items,
            "draft_message": _dunning_text(p, amt, oldest, level),
        })
    return {
        "as_of_date": oi.get("as_of_date"),
        "bucket_days": list(bucket_days),
        "rows": rows,
        "counts": counts,
        "totals": {"overdue_amount": f"{total_overdue:.2f}", "customers": len(rows)},
        "basis": "逾期=未清账龄 > 首桶（默认>30天）；升级 L1/L2/L3 按最大账龄；话术为草稿不代发",
    }


def _escalation_level(days: int, bucket_days: tuple[int, ...]) -> str:
    if days > bucket_days[2]:
        return "L3"
    if days > bucket_days[1]:
        return "L2"
    return "L1"


def _dunning_text(partner: str, amount: Decimal, oldest: int, level: str) -> str:
    amt_s = f"{amount:,.2f}"
    if level == "L1":
        return (
            f"【友情提醒】{partner} 您好，贵司尚有应收 ¥{amt_s} 未结清"
            f"（最早一笔已 {oldest} 天）。请于本周内安排付款，谢谢合作。"
        )
    if level == "L2":
        return (
            f"【催收跟进】{partner} 您好，贵司应收 ¥{amt_s} 已逾期 {oldest} 天。"
            f"请尽快安排付款；如已付请回传水单，逾期未处理将影响后续合作。"
        )
    return (
        f"【最后通牒】{partner} 您好，贵司应收 ¥{amt_s} 已严重逾期 {oldest} 天。"
        f"请于 3 个工作日内付清，否则我方将暂停供货并保留法律追索权利。"
    )

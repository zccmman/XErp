"""运营财务本体层（Phase E / E1）：动态重建运营财务图谱 + 往来单位一站式画像 + 图谱指标。

设计铁律（事件溯源 + ADR-002 单一真源）：
- 运营财务关系（客户↔发票↔回款↔科目）没有独立实体，全部隐式编码在
  ``VoucherLine.aux_dims``（键 customer/supplier 的往来单位名）+
  ``ArapClearing`` 核销记录。本模块**不建表、不建投影、不复制配平逻辑**；
  只读复用 ``arap._collect_arap_lines`` / ``arap.open_items`` /
  ``credit.credit_exposure`` / ``credit.collections_draft`` /
  ``arap.subledger_gl_reconcile`` 等既有内核，把分散的取数切片重组成
  「图谱(node/edge) + 画像(per-partner) + 指标(aggregate)」三种视图。
- 每条数字都可溯源到凭证明细行 / 核销记录（ADR-002），便于 Copilot（E2）逐条解释。
- 全部只读，绝不写账、不制单、不触发任何终态动作（HITL 终态永远由 Boss 确认）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from kernel.db.models import ArapClearing
from kernel.reporting.arap import (
    ArapError,
    _collect_arap_lines,
    _is_invoice_side,
    _resolve_accounts,
    aging_analysis,
    open_items,
    subledger_gl_reconcile,
    unmatched_receipts,
)
from kernel.reporting.credit import collections_draft, credit_exposure

ZERO = Decimal("0.00")


# ============================================================ E1-a：运营财务图谱
# 把隐式在凭证里的「客户↔发票↔回款↔科目」关系物化成节点 + 边，供图谱渲染 / 关联分析。


def build_graph(
    session,
    *,
    ledger_set_id: str,
    dim_key: str = "customer",
    partner: str | None = None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """动态重建运营财务图谱（只读）：由 ``VoucherLine.aux_dims`` + ``ArapClearing`` 重建。

    节点类型（node.type）：
      - ``partner``  往来单位（customer / supplier 名）
      - ``account``  往来科目（1122 / 2202 ...）
      - ``voucher``  记账凭证
      - ``invoice``  发票侧凭证明细行（余额增加侧）
      - ``payment``  回款/付款侧凭证明细行（余额减少侧）
    边类型（edge.type）：
      - ``has_invoice`` / ``has_payment``  partner → 单据
      - ``uses_account``                  单据 → 科目
      - ``from_voucher``                  单据 → 凭证
      - ``cleared_by``                    invoice → payment（依据 ArapClearing，weight=核销额）

    完全由凭证 + 核销记录重建（ADR-002），不建新表 / 投影。
    返回 {dim_key, as_of_date, nodes, edges, stats, basis}。
    """
    accounts = _resolve_accounts(session, ledger_set_id, dim_key)
    if not accounts:
        return {
            "dim_key": dim_key,
            "as_of_date": (as_of_date.isoformat() if as_of_date else date.today().isoformat()),
            "nodes": [],
            "edges": [],
            "stats": {"partners": 0, "accounts": 0, "vouchers": 0,
                      "invoices": 0, "payments": 0, "clearings": 0},
            "basis": "无往来科目，图谱为空",
        }
    if as_of_date is None:
        as_of_date = date.today()

    lines = _collect_arap_lines(
        session, ledger_set_id, accounts, dim_key, partner, as_of_date
    )

    nodes: dict[str, dict] = {}
    edge_keys: set[tuple[str, str, str]] = set()
    edges: list[dict] = []
    line_node: dict[str, str] = {}  # line_id -> node id

    def ensure_node(nid: str, ntype: str, **attrs) -> None:
        if nid not in nodes:
            nodes[nid] = {"id": nid, "type": ntype, **attrs}

    def add_edge(src: str, tgt: str, etype: str, **attrs) -> None:
        key = (src, tgt, etype)
        if key in edge_keys:
            return
        edge_keys.add(key)
        edges.append({"source": src, "target": tgt, "type": etype, **attrs})

    for ln in lines:
        pid = f"partner:{ln['partner']}"
        aid = f"account:{ln['account_code']}"
        vid = f"voucher:{ln['voucher_no']}"
        lid = f"line:{ln['line_id']}"
        is_inv = _is_invoice_side(ln)
        ltype = "invoice" if is_inv else "payment"
        gross = (ln["debit"] if ln["direction"] == "debit" else ln["credit"])

        ensure_node(pid, "partner", label=ln["partner"], dim_key=dim_key)
        ensure_node(aid, "account", label=ln["account_code"],
                    direction=ln["direction"])
        ensure_node(vid, "voucher", label=ln["voucher_no"],
                    date=ln["date"].isoformat())
        ensure_node(
            lid, ltype,
            label=f"{ln['voucher_no']} {ln['account_code']}",
            partner=ln["partner"], voucher_no=ln["voucher_no"],
            date=ln["date"].isoformat(), account_code=ln["account_code"],
            amount=f"{gross:.2f}",
        )
        line_node[ln["line_id"]] = lid

        add_edge(pid, lid, "has_invoice" if is_inv else "has_payment")
        add_edge(lid, aid, "uses_account")
        add_edge(lid, vid, "from_voucher")

    # 核销关系（invoice → payment），依据 ArapClearing 逐条重建
    q = select(ArapClearing).where(
        ArapClearing.ledger_set_id == ledger_set_id,
        ArapClearing.dim_key == dim_key,
    )
    if partner:
        q = q.where(ArapClearing.partner == partner)
    clearing_records = session.scalars(q).all()
    clear_count = 0
    for c in clearing_records:
        src = line_node.get(c.invoice_line_id)
        tgt = line_node.get(c.payment_line_id)
        if src is None or tgt is None:
            continue  # 不在本次切片内（如未达 as_of_date 或不在 partner 范围）
        add_edge(src, tgt, "cleared_by", amount=f"{c.amount:.2f}")
        clear_count += 1

    stats = {
        "partners": sum(1 for n in nodes.values() if n["type"] == "partner"),
        "accounts": sum(1 for n in nodes.values() if n["type"] == "account"),
        "vouchers": sum(1 for n in nodes.values() if n["type"] == "voucher"),
        "invoices": sum(1 for n in nodes.values() if n["type"] == "invoice"),
        "payments": sum(1 for n in nodes.values() if n["type"] == "payment"),
        "clearings": clear_count,
    }
    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": stats,
        "basis": ("由 VoucherLine.aux_dims（往来单位名）+ ArapClearing（核销）重建；"
                  "不建新表/投影，守 ADR-002"),
    }


# ============================================================ E1-b：往来单位一站式画像
# 把分散在多份报表里的某个往来单位信息聚合成一张卡，供 Copilot / Boss 一屏看全。


def partner_profile(
    session,
    *,
    ledger_set_id: str,
    dim_key: str,
    partner: str,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """往来单位一站式画像（只读）：汇聚该单位的敞口 / 账龄 / 未清 / 待匹配 / 催收 / 对账。

    聚合来源（全部既有只读内核，ADR-002 单一真源）：
      - ``summary``  信用敞口 + 授信额度 + 利用率 + 超额/临近（credit_exposure）
      - ``aging``    账龄分桶（aging_analysis，筛该单位）
      - ``open_items`` 未清项总额 + 笔数（open_items，筛该单位）
      - ``unmatched_receipts`` 待匹配回款总额 + 笔数（unmatched_receipts，筛该单位）
      - ``collections`` 逾期金额 + 升级级别（collections_draft，仅 customer）
      - ``reconcile`` 该单位是否完整挂接往来维度（subledger_gl_reconcile 视角）
    返回 {dim_key, partner, as_of_date, summary, aging, open_items, unmatched_receipts,
          collections, reconcile, basis}。
    """
    if as_of_date is None:
        as_of_date = date.today()

    # —— 信用敞口 + 额度 ——
    exp = credit_exposure(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    row = next((r for r in exp["rows"] if r["partner"] == partner), None)
    summary = {
        "exposure": row["exposure"] if row else "0.00",
        "credit_limit": row["credit_limit"] if row else "0.00",
        "utilization": row["utilization"] if row else None,
        "breach": row["breach"] if row else False,
        "near_limit": row["near_limit"] if row else False,
    }

    # —— 账龄（筛该单位）——
    ag = aging_analysis(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    aging_items = [it for it in ag["items"] if it["partner"] == partner]
    aging = {
        "items": aging_items,
        "bucket_days": ag["bucket_days"],
        "as_of_date": ag["as_of_date"],
    }

    # —— 未清项（筛该单位）——
    oi = open_items(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key,
        partner=partner, as_of_date=as_of_date,
    )
    open_block = {
        "count": len(oi["items"]),
        "totals": oi["totals"],
        "as_of_date": oi["as_of_date"],
    }

    # —— 待匹配回款（筛该单位）——
    ur = unmatched_receipts(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key,
        partner=partner, as_of_date=as_of_date,
    )
    unmatched = {
        "count": ur["totals"]["count"],
        "remaining": ur["totals"]["remaining"],
        "as_of_date": ur["as_of_date"],
    }

    # —— 催收（仅 customer 维度有意义）——
    collections: dict[str, Any] = {"available": False}
    if dim_key == "customer":
        col = collections_draft(session, ledger_set_id=ledger_set_id, as_of_date=as_of_date)
        crow = next((r for r in col["rows"] if r["partner"] == partner), None)
        collections = {
            "available": True,
            "overdue_amount": crow["overdue_amount"] if crow else "0.00",
            "oldest_days": crow["oldest_days"] if crow else 0,
            "level": crow["level"] if crow else None,
            "draft_message": crow["draft_message"] if crow else None,
            "as_of_date": col["as_of_date"],
        }

    # —— 对账（该单位是否完整挂接往来维度）——
    rec = subledger_gl_reconcile(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    partner_assigned = any(p["partner"] == partner for p in rec["partners"])
    reconcile = {
        "dimension_ok": rec["ok"],
        "dimension_difference": rec["difference"],
        "partner_assigned": partner_assigned,
    }

    return {
        "dim_key": dim_key,
        "partner": partner,
        "as_of_date": as_of_date.isoformat(),
        "summary": summary,
        "aging": aging,
        "open_items": open_block,
        "unmatched_receipts": unmatched,
        "collections": collections,
        "reconcile": reconcile,
        "basis": "聚合 credit_exposure / aging_analysis / open_items / unmatched_receipts / "
                 "collections_draft / subledger_gl_reconcile；全只读，守 ADR-002",
    }


# ============================================================ E1-c：图谱指标
# 一屏看全：AR·AP 总额、敞口 TopN、HHI 集中度、对账健康。


def graph_metrics(
    session,
    *,
    ledger_set_id: str,
    dim_key: str = "customer",
    as_of_date: date | None = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """图谱指标（只读）：应收/应付总额、敞口 TopN、HHI 集中度、对账健康。

    - ``ar_total`` / ``ap_total``：按 dim_key 取 open_items 总额（应收=customer、
      应付=supplier）；同时把对方维度一并算出，便于「AR·AP 总额」一屏看全。
    - ``exposure_topn``：该维度敞口最大的 N 个往来单位（来自 credit_exposure）。
    - ``hhi``：敞口集中度 Herfindahl-Hirschman 指数（标准刻度 0-10000；<1500 低集中、
      1500-2500 中、>2500 高集中，反垄断口径），基于该维度逐客户敞口份额平方和。
    - ``reconcile_health``：该维度子账↔总账对账 ok + 漏挂笔数 + 漏挂金额。
    全部复用既有只读内核，不建投影（ADR-002）。
    """
    if as_of_date is None:
        as_of_date = date.today()

    ar = open_items(session, ledger_set_id=ledger_set_id, dim_key="customer", as_of_date=as_of_date)
    ap = open_items(session, ledger_set_id=ledger_set_id, dim_key="supplier", as_of_date=as_of_date)
    ar_total = Decimal(ar["totals"]["balance"])
    ap_total = Decimal(ap["totals"]["balance"])

    exp = credit_exposure(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )
    total_exposure = Decimal(exp["totals"]["exposure"]) if exp["totals"]["exposure"] else ZERO
    hhi = ZERO
    if total_exposure > ZERO:
        hhi = sum(
            ((Decimal(r["exposure"]) / total_exposure) ** 2) for r in exp["rows"]
        )
    hhi_scaled = (hhi * Decimal("10000")).quantize(Decimal("1"))
    hhi_level = ("高集中" if hhi_scaled > 2500 else
                 "中集中" if hhi_scaled >= 1500 else "低集中")

    rec = subledger_gl_reconcile(
        session, ledger_set_id=ledger_set_id, dim_key=dim_key, as_of_date=as_of_date
    )

    return {
        "dim_key": dim_key,
        "as_of_date": as_of_date.isoformat(),
        "ar_total": f"{ar_total:.2f}",
        "ap_total": f"{ap_total:.2f}",
        "exposure_topn": exp["rows"][:top_n],
        "exposure_total": exp["totals"]["exposure"],
        "exposure_credit_limit": exp["totals"]["credit_limit"],
        "exposure_utilization": exp["totals"]["utilization"],
        "breaches": exp["breaches"],
        "hhi": {
            "value": f"{hhi_scaled:.0f}",
            "level": hhi_level,
            "partner_count": len(exp["rows"]),
        },
        "reconcile_health": {
            "ok": rec["ok"],
            "difference": rec["difference"],
            "unassigned_count": rec["unassigned_count"],
            "unassigned_total": rec["unassigned_total"],
            "partner_count": rec["partner_count"],
        },
        "basis": "聚合 open_items(AR/AP) / credit_exposure / subledger_gl_reconcile；全只读，守 ADR-002",
    }

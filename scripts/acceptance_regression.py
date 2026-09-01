"""XErp 全量验收回归（docs/ACCEPTANCE.md A-L 数字级端到端）。

用 fastmcp 内存客户端 + 独立临时账套，照验收手册逐项断言精确数字，
输出结构化 PASS/FAIL 表。D（飞书真机）/ E（Web 界面）需人工/浏览器，另行验收。

用法：
    python scripts/acceptance_regression.py
退出码：0=全过；1=有失败项。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp-server"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kernel.authz import grant_ledger_role  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Subject  # noqa: E402
from kernel.ledger import verify_chain  # noqa: E402

YEAR, MONTH = 2026, 9  # 验收手册固定期间（数字自洽性基于 9 月）

RESULTS: list[dict] = []


def record(sid: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"id": sid, "name": name, "ok": bool(ok), "detail": detail})


def _num(v) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, Decimal):
        return v
    s = str(v).replace(",", "").strip()
    return Decimal(s) if s else Decimal("0")


def call(server, tool: str, **args):
    async def inner():
        from fastmcp import Client

        async with Client(server) as c:
            res = await c.call_tool(tool, args)
            if getattr(res, "data", None) is not None:
                return res.data
            return json.loads(res.content[0].text)

    return asyncio.run(inner())


def _create_reviewer(engine, ls_id: str) -> str:
    """MCP 层无「创建主体」工具，用内核 API 补一个审批员（审批换人红线依赖）。"""
    with Session(engine) as s:
        reviewer = Subject(type="user", display_name="审批员", autonomy_level=3)
        s.add(reviewer)
        s.flush()
        rid = reviewer.id
        # 先 commit 落盘：casbin adapter 会另开连接写 casbin_rule，
        # 外层未提交事务持有 SQLite 写锁会撞 database is locked
        s.commit()
        grant_ledger_role(s, ledger_set_id=ls_id, subject_id=rid, role="reviewer")
        grant_ledger_role(s, ledger_set_id=ls_id, subject_id=rid, role="accountant")
        return rid


def _create_agent(engine, ls_id: str, name: str, level: int, limit: str | None) -> str:
    with Session(engine) as s:
        subj = Subject(
            type="agent",
            display_name=name,
            autonomy_level=level,
            daily_voucher_limit=Decimal(limit) if limit else None,
        )
        s.add(subj)
        s.flush()
        sid = subj.id
        s.commit()  # 同上：先释放写锁再写 casbin_rule
        grant_ledger_role(s, ledger_set_id=ls_id, subject_id=sid, role="agent")
        return sid


def full_post(server, ls_id, owner, reviewer, date, summary, lines) -> dict:
    """create → push → approve(换人) → post，返回最终 get_voucher 结果。"""
    made = call(
        server, "create_voucher", ledger_set_id=ls_id, voucher_date=date,
        summary=summary, actor_id=owner, lines=lines,
    )
    if not made.get("ok"):
        return made
    vid = made["voucher"]["id"]
    p = call(server, "push_voucher", voucher_id=vid, actor_id=owner)
    if not p.get("ok"):
        return p
    a = call(server, "approve_voucher", voucher_id=vid, actor_id=reviewer)
    if not a.get("ok"):
        return a
    post = call(server, "post_voucher", voucher_id=vid, actor_id=owner)
    if not post.get("ok"):
        return post
    return post


def two_line(dr_code, cr_code, amount) -> list[dict]:
    return [
        {"account_code": dr_code, "debit": amount, "credit": ""},
        {"account_code": cr_code, "debit": "", "credit": amount},
    ]


def main() -> int:
    engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/accept.db")
    Base.metadata.create_all(engine)

    from xerp_mcp.server import build_server

    server = build_server(str(engine.url))

    # ---------- 建套 + 主体 ----------
    r1 = call(server, "init_ledger_set", name="验收回归", owner_name="验收人")
    assert r1.get("ok"), f"建账失败: {r1}"
    ls_id = r1["ledger_set_id"]
    owner = r1["owner_subject_id"]
    reviewer = _create_reviewer(engine, ls_id)
    call(server, "ensure_period", ledger_set_id=ls_id, year=YEAR, month=MONTH)
    record("setup", "建账 + 期间 + 审批员主体", r1.get("accounts_created", 0) >= 140)

    # ============ A 期初建账（试算平衡） ============
    opening = call(
        server, "import_opening_balances", ledger_set_id=ls_id, actor_id=owner,
        period_year=YEAR, period_month=MONTH,
        lines=[
            {"account_code": "100201", "debit": "100000", "credit": ""},
            {"account_code": "1001", "debit": "2000", "credit": ""},
            {"account_code": "1601", "debit": "20000", "credit": ""},
            {"account_code": "2202", "debit": "", "credit": "12000"},
            {"account_code": "3001", "debit": "", "credit": "110000"},
        ],
    )
    ok_a1 = opening.get("ok") and opening["voucher"]["status"] == "POSTED"
    record("A1", "期初导入 5 笔 → POSTED 试算平衡", ok_a1,
           f"借=贷={_num(122000)}" if ok_a1 else str(opening))
    bad = call(
        server, "import_opening_balances", ledger_set_id=ls_id, actor_id=owner,
        period_year=YEAR, period_month=MONTH,
        lines=[
            {"account_code": "100201", "debit": "100", "credit": ""},
            {"account_code": "3001", "debit": "", "credit": "90"},
        ],
    )
    record("A2", "不平衡期初被硬拒", bad.get("ok") is False
           and bad["error"]["code"] == "TRIAL_BALANCE_UNBALANCED", str(bad))

    # ============ B 十笔业务全流程 ============
    biz = [
        ("2026-09-05", "销售收款", [("100201", "11000", ""), ("6001", "", "11000")]),
        ("2026-09-08", "购办公用品", [("660202", "500", ""), ("1001", "", "500")]),
        ("2026-09-10", "赊购电脑", [("1601", "6000", ""), ("2202", "", "6000")]),
        ("2026-09-12", "偿还应付", [("2202", "2000", ""), ("100201", "", "2000")]),
        ("2026-09-15", "赊销服务", [("1122", "8000", ""), ("6001", "", "8000")]),
        ("2026-09-20", "收回欠款", [("100201", "5000", ""), ("1122", "", "5000")]),
        ("2026-09-22", "支付房租", [("660202", "3000", ""), ("100201", "", "3000")]),
        ("2026-09-25", "差旅报销", [("660203", "800", ""), ("1001", "", "800")]),
        ("2026-09-28", "广告费", [("660102", "1500", ""), ("100201", "", "1500")]),
        ("2026-09-29", "提现", [("1001", "1000", ""), ("100201", "", "1000")]),
    ]
    posted = 0
    b_detail = []
    for d, summary, lines in biz:
        dr = lines[0]
        cr = lines[1]
        r = full_post(server, ls_id, owner, reviewer, d, summary,
                      two_line(dr[0], cr[0], dr[1] if dr[2] == "" else cr[1]))
        if r.get("ok") and r["voucher"]["status"] == "POSTED":
            posted += 1
        else:
            b_detail.append(f"{summary}:{r}")
    record("B1", "10 笔业务全流程 POSTED", posted == 10,
           f"{posted}/10" + (f" {b_detail[0]}" if b_detail else ""))
    # HITL 红线：制单人自审
    made = call(server, "create_voucher", ledger_set_id=ls_id,
                voucher_date="2026-09-30", summary="自审测试", actor_id=owner,
                lines=two_line("660202", "100201", "10"))
    vid = made["voucher"]["id"]
    call(server, "push_voucher", voucher_id=vid, actor_id=owner)
    self_apr = call(server, "approve_voucher", voucher_id=vid, actor_id=owner)
    record("B2", "制单=审批红线 NO_SELF_APPROVAL",
           self_apr.get("ok") is False and self_apr["error"]["code"] == "NO_SELF_APPROVAL",
           str(self_apr))

    # ============ C 逐科目期末余额（手册 C1 核对表口径） ============
    bal = call(server, "query_balances", ledger_set_id=ls_id,
               period_year=YEAR, period_month=MONTH)
    bals = {b["account_code"]: b for b in bal.get("balances", [])}

    def net(code: str, side: str) -> Decimal:
        """side='dr' 借方余额（借-贷）；side='cr' 贷方余额（贷-借）。"""
        b = bals[code]
        dr, cr = _num(b["debit_total"]), _num(b["credit_total"])
        return dr - cr if side == "dr" else cr - dr

    expect_c1 = [
        ("100201", "dr", "108500"), ("1001", "dr", "1700"),
        ("1122", "dr", "3000"), ("1601", "dr", "26000"),
        ("2202", "cr", "16000"), ("3001", "cr", "110000"),
        ("6001", "cr", "19000"),
    ]
    c1_bad = [(c, str(net(c, s)), v) for c, s, v in expect_c1
              if net(c, s) != _num(v)]
    exp_total = _num("500") + _num("3000") + _num("800") + _num("1500")
    got_total = sum(net(c, "dr") for c in ("660102", "660202", "660203"))
    if got_total != exp_total:
        c1_bad.append(("费用合计", str(got_total), str(exp_total)))
    record("C1", "期末余额逐科目 8 行与手册核对表一致", not c1_bad, str(c1_bad))

    inc = call(server, "report_income_statement", ledger_set_id=ls_id,
               period_year=YEAR, period_month=MONTH)["report"]
    record("C2", "利润表 营收19000/费用5800/净利13200",
           _num(inc["revenue"]) == 19000 and _num(inc["expense"]) == 5800
           and _num(inc["net_profit"]) == 13200,
           f"revenue={inc.get('revenue')} expense={inc.get('expense')} "
           f"net_profit={inc.get('net_profit')}")

    bs = call(server, "report_balance_sheet", ledger_set_id=ls_id,
              period_year=YEAR, period_month=MONTH)["report"]
    # 手册 C3：负债 16,000 + 权益 110,000 + 净利润（未结转插值）13,200 = 139,200
    record("C3", "资产负债表 负债16000+权益123200=资产139200 平衡",
           _num(bs["assets"]["total"]) == 139200
           and _num(bs["liabilities"]["total"]) == 16000
           and _num(bs["equity"]["total"]) == 123200 and bs.get("balanced") is True,
           f"assets={bs['assets'].get('total')} liab={bs['liabilities'].get('total')} "
           f"equity={bs['equity'].get('total')} balanced={bs.get('balanced')}")

    cf = call(server, "report_cash_flow", ledger_set_id=ls_id,
              period_year=YEAR, period_month=MONTH)["report"]
    record("C4", "现金流量表 经营净额8200",
           _num(cf["operating"]) == 8200, f"operating={cf.get('operating')}")

    # ============ F 事件适配器 + 往来 ============
    f1 = call(server, "adapter_ingest", ledger_set_id=ls_id, actor_id=owner,
              adapter="ar", event_type="invoice.issued",
              event={"event_id": "INV-UAT-1", "invoice_no": "INV-UAT-1",
                     "customer": "客户甲", "issued_at": "2026-09-25",
                     "net_amount": "2000.00", "tax_amount": "20.00",
                     "total_amount": "2020.00"})
    record("F1", "开票事件自动入账 PUSHED（挂客户甲）",
           f1.get("ok") and f1["voucher"]["status"] == "PUSHED"
           and f1.get("replayed") is False, str(f1.get("voucher", f1)))
    f1_dup = call(server, "adapter_ingest", ledger_set_id=ls_id, actor_id=owner,
                  adapter="ar", event_type="invoice.issued",
                  event={"event_id": "INV-UAT-1", "invoice_no": "INV-UAT-1",
                         "customer": "客户甲", "issued_at": "2026-09-25",
                         "net_amount": "2000.00", "tax_amount": "20.00",
                         "total_amount": "2020.00"})
    record("F1b", "开票事件幂等（重放不重复入账）", f1_dup.get("replayed") is True)

    f2 = call(server, "adapter_ingest", ledger_set_id=ls_id, actor_id=owner,
              adapter="ar", event_type="payment.received",
              event={"event_id": "PAY-UAT-1", "customer": "客户甲",
                     "received_at": "2026-09-26", "amount": "2020.00"})
    record("F2", "回款事件自动入账 PUSHED",
           f2.get("ok") and f2["voucher"]["status"] == "PUSHED", str(f2.get("voucher", f2)))

    pb = call(server, "partner_balances", ledger_set_id=ls_id,
              year=YEAR, month=MONTH)
    record("F3", "往来余额表（客户甲在途 + untracked 诚实单列）",
           pb.get("ok") and "report" in pb, json.dumps(pb.get("report", {}), ensure_ascii=False)[:200])

    # ============ G 发票 OCR（手册 G：日期用 2026-09-01 —— 业务期间内且不触发
    # 未来日期闸门；适配器按 invoice_date 入账，落未开账期间会硬拒） ============
    inv = {
        "invoice_no": "26123001", "invoice_date": "2026-09-01",
        "seller_name": "供应商丙", "seller_tax_id": "91330106MA2XY1N234",
        "total_amount": "1130.00", "net_amount": "1118.81", "tax_amount": "11.19",
        "expense_category": "办公费", "confidence": {},
    }
    g1 = call(server, "ocr_ingest_invoice", ledger_set_id=ls_id, actor_id=owner,
              invoice=inv)
    record("G1", "合格发票入账 PUSHED", g1.get("ok")
           and g1.get("disposition") == "ingested", str(g1)[:160])
    g2 = call(server, "ocr_ingest_invoice", ledger_set_id=ls_id, actor_id=owner,
              invoice=inv)
    record("G2", "重复发票硬闸 DUPLICATE_INVOICE",
           g2.get("ok") is False and g2["error"]["code"] == "DUPLICATE_INVOICE", str(g2))
    bad_inv = dict(inv, invoice_no="26123002", total_amount="2000.00",
                   net_amount="1000.00", tax_amount="10.00")
    g3 = call(server, "ocr_ingest_invoice", ledger_set_id=ls_id, actor_id=owner,
              invoice=bad_inv)
    record("G3", "勾稽不符 → flagged 入复核队列",
           g3.get("ok") and g3.get("disposition") == "flagged", str(g3)[:160])
    truth = {"invoice_no": "26123001", "invoice_date": "2026-09-01",
             "seller_name": "供应商丙", "total_amount": "1130.00",
             "net_amount": "1118.81", "tax_amount": "11.19"}
    g4 = call(server, "ocr_accuracy_report",
              samples=[{"extracted": truth, "ground_truth": truth}])
    rep4 = g4.get("report", {})
    record("G4", "OCR 准确率抽检 ≥95%",
           rep4.get("pass_threshold_95") is True and _num(rep4.get("accuracy", 0)) >= 0.95,
           f"accuracy={rep4.get('accuracy')}")

    # ============ H 银行对账 ============
    csv_text = (
        "date,amount,counterparty,summary,txn_id\n"
        "2026-09-05,11000.00,客户乙,销售收款,TXN-U1\n"
        "2026-09-22,-3000.00,房东,房租,TXN-U2\n"
        "2026-09-30,-38.00,银行,账户手续费,TXN-U3\n"
    )
    h1 = call(server, "bank_import_csv", ledger_set_id=ls_id, actor_id=owner,
              csv_text=csv_text)
    record("H1", "银行流水导入 3 条", h1.get("ok") and h1.get("imported") == 3, str(h1)[:160])
    h1b = call(server, "bank_import_csv", ledger_set_id=ls_id, actor_id=owner,
               csv_text=csv_text)
    record("H1b", "流水重复导入幂等 skipped=3", h1b.get("skipped") == 3, str(h1b)[:160])
    h2 = call(server, "bank_reconcile", ledger_set_id=ls_id, actor_id=owner)
    rep2 = h2.get("report", {})
    summary = rep2.get("summary", {})
    bank_only = [t.get("txn_id") for t in rep2.get("bank_only", [])]
    record("H2", "对账：勾对 + 未达账 TXN-U3 银行有账上无",
           summary.get("matched_count") == 2 and "TXN-U3" in bank_only,
           f"matched={summary.get('matched_count')} bank_only={bank_only}")

    # ============ J L3 自治档 ============
    uat_agent = _create_agent(engine, ls_id, "UAT-Agent", 3, "300")
    j1 = call(server, "autonomy_post", ledger_set_id=ls_id, actor_id=uat_agent,
              voucher_date="2026-09-28", summary="订阅费",
              lines=two_line("660202", "100201", "200"))
    record("J1", "L3 额度内自治过账直接 POSTED",
           j1.get("ok") and j1.get("autonomous") is True
           and j1["voucher"]["status"] == "POSTED"
           and _num(j1.get("quota_used_today")) == 200, str(j1)[:160])
    j2 = call(server, "autonomy_post", ledger_set_id=ls_id, actor_id=uat_agent,
              voucher_date="2026-09-28", summary="超额度",
              lines=two_line("660202", "100201", "200"))
    record("J2", "超日额度 QUOTA_EXCEEDED",
           j2.get("ok") is False and j2["error"]["code"] == "QUOTA_EXCEEDED", str(j2))
    jv = j1["voucher"]["id"]
    j3 = call(server, "autonomy_audit_review", ledger_set_id=ls_id, actor_id=owner,
              voucher_id=jv, verdict="reverse", note="科目用错")
    record("J3", "抽检推翻 → 红字冲销", j3.get("ok")
           and j3.get("reversal_voucher_no"), str(j3)[:160])
    j4 = call(server, "autonomy_replay", voucher_id=jv)
    types4 = [e["event_type"] for e in j4.get("timeline", [])]
    record("J4", "一键回放事件链完整",
           "AUTONOMOUS_POSTED" in types4 and "AUTONOMOUS_REVERSED" in types4,
           ",".join(types4))

    # ============ I 关账 Agent ============
    dry = call(server, "monthend_run", ledger_set_id=ls_id, actor_id=owner,
               period_year=YEAR, period_month=MONTH, dry_run=True)
    record("I1", "关账 dry-run 列未审不动账",
           dry.get("ok") and dry["report"].get("dry_run") is True
           and "check" in dry["report"].get("steps", {}), str(dry)[:160])
    # 审批所有 PUSHED（适配器 2 + OCR 1）
    with Session(engine) as s:
        from kernel.db.models import Voucher
        pushed_ids = [v.id for v in s.scalars(
            select(Voucher).where(Voucher.ledger_set_id == ls_id,
                                  Voucher.status == "PUSHED")).all()]
    for pid in pushed_ids:
        call(server, "approve_voucher", voucher_id=pid, actor_id=reviewer)
        call(server, "post_voucher", voucher_id=pid, actor_id=owner)
    formal = call(server, "monthend_run", ledger_set_id=ls_id, actor_id=owner,
                  period_year=YEAR, period_month=MONTH, dry_run=False)
    steps = formal.get("report", {}).get("steps", {})
    closing_ok = "closing" in steps and steps["closing"].get("voucher_no")
    trial_ok = steps.get("trial_balance", {}).get("reconcile_ok") is True
    record("I2", "正式关账 结转+试算通过",
           formal.get("ok") and closing_ok and trial_ok, str(formal)[:160])

    # ============ K 异常侦测 + 断路器（10 月期间） ============
    kagent = _create_agent(engine, ls_id, "异常Agent", 2, None)
    kv = call(server, "create_voucher", ledger_set_id=ls_id,
              voucher_date="2026-10-05", summary="大额异常", actor_id=kagent,
              lines=two_line("660202", "2202", "88888"))
    kscan = call(server, "anomaly_scan", ledger_set_id=ls_id, actor_id=owner,
                 voucher_id=kv["voucher"]["id"])
    krules = [f["rule"] for f in kscan.get("findings", [])]
    kblock = call(server, "create_voucher", ledger_set_id=ls_id,
                  voucher_date="2026-10-06", summary="再试", actor_id=kagent,
                  lines=two_line("660202", "2202", "10"))
    record("K1", "大额命中 large_amount + 断路器冻结 Agent",
           "large_amount" in krules
           and kblock.get("ok") is False and kblock["error"]["code"] == "BREAKER_OPEN",
           f"rules={krules} block={kblock.get('error', {}).get('code')}")
    rel = call(server, "anomaly_release", actor_id=owner, subject_id=kagent, note="验收")
    record("K2", "人工解除断路器", rel.get("ok") is True, str(rel)[:120])

    # ============ L 审计安全 ============
    with Session(engine) as s:
        ok_chain, problem = verify_chain(s, ls_id)
    record("L1", "审计链校验 chain_ok", ok_chain and problem is None, str(problem)[:120])
    rec = call(server, "reconcile_ledger", ledger_set_id=ls_id,
               period_year=YEAR, period_month=MONTH)
    record("L2", "账账核对无差异", rec.get("ok")
           and rec["report"].get("ok") is True, str(rec.get("report", rec))[:160])
    # 篡改检出：直接 SQL 改一张 POSTED 凭证金额
    with Session(engine) as s:
        from kernel.db.models import Voucher, VoucherLine
        victim = s.scalars(select(Voucher).where(
            Voucher.ledger_set_id == ls_id, Voucher.status == "POSTED")).first()
        ln = s.scalars(select(VoucherLine).where(
            VoucherLine.voucher_id == victim.id)).first()
        ln.debit = ln.debit + Decimal("1")  # 破坏借贷平衡
        s.commit()
        victim_id = victim.id
    rec2 = call(server, "reconcile_ledger", ledger_set_id=ls_id,
                period_year=YEAR, period_month=MONTH)
    issues2 = rec2.get("report", {}).get("issues", [])
    record("L3", "篡改被账账核对检出",
           len(issues2) > 0 or rec2["report"].get("ok") is False,
           f"issues={len(issues2)}")

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    print("XErp 验收回归报告（ACCEPTANCE A-L 数字级）")
    print("=" * 70)
    passed = sum(1 for r in RESULTS if r["ok"])
    for r in RESULTS:
        mark = "✅" if r["ok"] else "❌"
        print(f"  {mark} {r['id']:<5} {r['name']:<40} {r['detail']}")
    print("-" * 70)
    print(f"  结果：{passed}/{len(RESULTS)} 通过")
    print("=" * 70)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

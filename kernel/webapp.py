"""XErp Web 最小界面（P0-13，HTML 服务端渲染兜底；正式 A2UI 在 P1）。

运行: python -m kernel.webapp   （默认 http://127.0.0.1:8001，XERP_DB 可覆盖）
页面: / 工作区 · /ledger/{id} 凭证+余额 · /voucher/{id} 凭证详情 · /init 建账向导
"""

from __future__ import annotations

import html
import os
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.coa import CoaImportError, import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Balance, LedgerSet, Period, Subject, Voucher

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CSS = """<style>
body{font-family:-apple-system,'Segoe UI',Inter,sans-serif;
     max-width:960px;margin:24px auto;padding:0 16px;color:#1a1a1a}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px}
th{background:#f5f5f0}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;
       background:#eef4e6;color:#3b6d11;font-size:12px}
.err{color:#a32d2d;background:#fcebeb;padding:8px 12px;border-radius:6px}
a{color:#185fa5;text-decoration:none}a:hover{text-decoration:underline}
input,textarea{width:100%;padding:6px;margin:4px 0;box-sizing:border-box}
button{padding:6px 18px;background:#185fa5;color:#fff;border:0;border-radius:6px;cursor:pointer}
</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
        f"<title>{html.escape(title)} · XErp</title>{_CSS}</head>"
        f"<body><h1>XErp <span class=badge>v0.1-dev</span></h1>{body}</body></html>"
    )


def _fmt(d) -> str:
    return f"{(d or 0):.2f}"


def build_app(db_url: str | None = None) -> FastAPI:
    url = db_url or os.environ.get("XERP_DB") or f"sqlite:///{_REPO_ROOT / 'ledgeros_dev.db'}"
    from sqlalchemy import create_engine

    engine = create_engine(url)
    Base.metadata.create_all(engine)

    app = FastAPI(title="XErp Web")

    def session() -> Session:
        return Session(engine)

    # ---------- 工作区 ----------

    @app.get("/", response_class=HTMLResponse)
    def index():
        with session() as s:
            ledgers = s.scalars(select(LedgerSet)).all()
            rows = ""
            for ls in ledgers:
                open_p = s.scalars(
                    select(Period).where(
                        Period.ledger_set_id == ls.id, Period.status == "OPEN"
                    )
                ).all()
                ptxt = ", ".join(f"{p.year}-{p.month:02d}" for p in open_p) or "（无 OPEN 期间）"
                rows += (
                    f"<tr><td><a href='/ledger/{ls.id}'>{html.escape(ls.name)}</a></td>"
                    f"<td>{ls.accounting_standard}</td><td>{ptxt}</td></tr>"
                )
            body = (
                "<h2>账套</h2><table><tr><th>名称</th><th>准则</th><th>OPEN 期间</th></tr>"
                + (rows or "<tr><td colspan=3>暂无账套，请先建账 ↓</td></tr>")
                + "</table>"
                + '<p><a href="/init">＋ 建账向导（新建账套并录入期初）</a></p>'
            )
            return _page("工作区", body)

    # ---------- 建账向导 ----------

    @app.get("/init", response_class=HTMLResponse)
    def init_form(error: str = ""):
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = (
            err
            + "<h2>建账向导</h2><form method=post action=/init>"
            + "账套名称<br><input name=name required><br>"
            + "所有者姓名（制单人身份）<br><input name=owner_name required><br>"
            + "<br><button type=submit>创建（自动导入小企业会计准则科目）</button></form>"
            + "<p>创建后请在账套页录入期初余额（试算平衡自动校验）。</p>"
        )
        return _page("建账", body)

    @app.post("/init")
    def init_submit(name: str = Form(""), owner_name: str = Form("")):
        name, owner_name = name.strip(), owner_name.strip()
        if not name or not owner_name:
            return RedirectResponse("/init?error=账套名与所有者姓名必填", status_code=303)
        with session() as s:
            exists = s.scalars(select(LedgerSet).where(LedgerSet.name == name)).first()
            if exists is not None:
                return RedirectResponse(f"/ledger/{exists.id}", status_code=303)
            ls = LedgerSet(name=name, accounting_standard="small_business")
            s.add(ls)
            s.flush()
            try:
                import_chart_of_accounts(s, ls.id, load_template_rows())
            except CoaImportError as e:
                s.rollback()
                return RedirectResponse(f"/init?error={e}", status_code=303)
            today = date.today()
            s.add(Period(ledger_set_id=ls.id, year=today.year, month=today.month, status="OPEN"))
            s.add(Subject(type="user", display_name=owner_name, autonomy_level=3))
            s.commit()
            return RedirectResponse(f"/ledger/{ls.id}", status_code=303)

    # ---------- 账套仪表盘 ----------

    @app.get("/ledger/{ls_id}", response_class=HTMLResponse)
    def ledger_dashboard(ls_id: str, year: int = 0, month: int = 0, error: str = ""):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>")
            periods = s.scalars(
                select(Period).where(Period.ledger_set_id == ls_id).order_by(
                    Period.year.desc(), Period.month.desc()
                )
            ).all()
            period = next(
                (p for p in periods if (not year or not month) and p.status == "OPEN"),
                None,
            ) or (periods[0] if periods else None)
            vouchers = s.scalars(
                select(Voucher)
                .where(Voucher.ledger_set_id == ls_id)
                .order_by(Voucher.voucher_no.desc())
                .limit(50)
            ).all()

            vrows = ""
            for v in vouchers:
                vrows += (
                    f"<tr><td><a href=/voucher/{v.id}>{v.voucher_no}</a></td>"
                    f"<td>{v.voucher_date}</td><td><span class=badge>{v.status}</span></td>"
                    f"<td>{html.escape(v.summary or '')}</td></tr>"
                )

            brows = ""
            if period is not None:
                for b in s.scalars(
                    select(Balance).where(Balance.period_id == period.id)
                ):
                    acc = s.get(Account, b.account_id)
                    brows += (
                        f"<tr><td>{acc.code if acc else '?'}</td>"
                        f"<td>{html.escape(acc.name if acc else '?')}</td>"
                        f"<td>{b.dims_key}</td>"
                        f"<td style=text-align:right>{_fmt(b.debit_total)}</td>"
                        f"<td style=text-align:right>{_fmt(b.credit_total)}</td></tr>"
                    )

            ptabs = "".join(
                f'<a href="/ledger/{ls_id}?year={p.year}&month={p.month}">'
                f"{p.year}-{p.month:02d}({p.status})</a>&nbsp;"
                for p in periods
            ) or "（无期间）"
            err = f'<p class="err">{html.escape(error)}</p>' if error else ""
            body = (
                f"<h2>账套：{html.escape(ls.name)}　"
                f"<a href='/ledger/{ls_id}/reports'>三大报表 →</a></h2>"
                f"<p>期间切换：{ptabs}</p>{err}"
                "<h3>凭证（最近 50 张）</h3>"
                "<table><tr><th>凭证号</th><th>日期</th><th>状态</th><th>摘要</th></tr>"
                + (vrows or "<tr><td colspan=4>暂无凭证</td></tr>")
                + "</table>"
                f"<h3>发生额投影 {period.year}-{period.month:02d}</h3>"
                "<table><tr><th>编码</th><th>科目</th><th>维度</th>"
                "<th>借方合计</th><th>贷方合计</th></tr>"
                + (brows or "<tr><td colspan=5>本期间尚无过账数据</td></tr>")
                + "</table>"
                f"""
<h3>导入期初余额</h3>
<form method=post action="/ledger/{ls_id}/opening">
每行一条：<code>科目编码,借方,贷方</code>（留空填 0 亦可省略为空段）<br>
<textarea name=lines_text rows=6 placeholder="1002,200000,&#10;3001,,200000"></textarea><br>
<button type=submit>导入（试算平衡校验）</button></form>"""
            )
            return _page(f"{ls.name}", body)

    @app.post("/ledger/{ls_id}/opening")
    def opening_import(ls_id: str, lines_text: str = Form("")):
        from kernel.opening import import_opening_balances
        from kernel.posting import PostingError
        back = f"/ledger/{ls_id}"
        subject = s_first_subject()
        actor = {"type": "user", "id": subject}
        parsed = []
        for raw in (lines_text or "").splitlines():
            parts = [x.strip() for x in raw.split(",")]
            if len(parts) < 3 or not parts[0]:
                continue
            parsed.append(
                {"account_code": parts[0], "debit": parts[1], "credit": parts[2]}
            )
        try:
            with session() as s:
                import_opening_balances(s, ledger_set_id=ls_id, actor=actor, lines=parsed)
                s.commit()
        except PostingError as e:
            return RedirectResponse(f"{back}?error={e.message_zh}", status_code=303)
        return RedirectResponse(back, status_code=303)

    def s_first_subject() -> str:
        with session() as s:
            sub = s.scalars(select(Subject)).first()
            return sub.id if sub else ""

    # ---------- 凭证详情 ----------

    @app.get("/ledger/{ls_id}/reports", response_class=HTMLResponse)
    def reports(ls_id: str, year: int = 0, month: int = 0, error: str = ""):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>")
            periods = s.scalars(
                select(Period).where(Period.ledger_set_id == ls_id).order_by(
                    Period.year.desc(), Period.month.desc()
                )
            ).all()
            period = next((p for p in periods if not year and p.status == "OPEN"), None) or (
                periods[0] if periods else None
            )
            if period is None:
                return _page(f"{ls.name}", "<p class=err>尚无期间</p>")
            yr, mo = period.year, period.month
            try:
                from kernel.reporting.statements import (
                    balance_sheet,
                    cash_flow,
                    income_statement,
                )

                bs = balance_sheet(s, ls_id, yr, mo, ls.accounting_standard)
                inc = income_statement(s, ls_id, yr, mo, ls.accounting_standard)
                cf = cash_flow(s, ls_id, yr, mo, ls.accounting_standard)
            except Exception as e:  # noqa: BLE001
                return _page(f"{ls.name}", f'<p class=err>报表生成失败: {html.escape(str(e))}</p>')

            def table(rows, head1, head2):
                out = f"<table><tr><th>{head1}</th><th style=text-align:right>{head2}</th></tr>"
                for name, amt in rows:
                    out += (
                        f"<tr><td>{html.escape(name)}</td>"
                        f"<td style=text-align:right>{amt:,.2f}</td></tr>"
                    )
                return out + "</table>"

            bs_rows = []
            for key, label in (("assets", "资产"), ("liabilities", "负债"),
                               ("equity", "所有者权益")):
                for it in bs[key]["items"]:
                    bs_rows.append((f"{label} · {it['group']}", it["amount"]))
            bs_rows.append(("资产合计", bs["assets"]["total"]))
            bs_rows.append(("负债和所有者权益合计",
                            bs["liabilities"]["total"] + bs["equity"]["total"]))

            inc_rows = [(i["item"], i["amount"]) for i in inc["items"]]
            inc_rows.append(("净利润", inc["net_profit"]))

            cf_rows = [(i["item"], i["amount"]) for i in cf["items"]]
            cf_rows.append(("经营活动净额", cf["operating"]))
            cf_rows.append(("投资活动净额", cf["investing"]))
            cf_rows.append(("筹资活动净额", cf["financing"]))
            cf_rows.append(("现金净增加额", cf["net_increase"]))

            badge = "✅ 平衡" if bs["balanced"] else f"❌ 差 {bs['check']['diff']}"
            from kernel.reconcile import reconcile_ledger

            rec = reconcile_ledger(s, ls_id, yr, mo, ls.accounting_standard)
            rec_badge = (
                "✅ 账账核对一致" if rec["ok"]
                else f"❌ 对账异常 {len(rec['issues'])} 项："
                + "、".join(i["kind"] for i in rec["issues"])
            )
            err = f'<p class="err">{html.escape(error)}</p>' if error else ""
            closed = s.scalars(
                select(Voucher.id).where(
                    Voucher.ledger_set_id == ls_id,
                    Voucher.voucher_no.like(f"结转-{yr}{mo:02d}-%"),
                )
            ).first() is not None
            close_ui = (
                '<span class=badge>✅ 已执行期末结转</span>' if closed else
                f'<form method=post action="/ledger/{ls_id}/close">'
                f'<input type=hidden name=year value={yr}>'
                f'<input type=hidden name=month value={mo}>'
                f'<button type=submit>执行 {yr}-{mo:02d} 期末结转</button></form>'
            )
            body = (
                f"<h2>{html.escape(ls.name)} · {yr}-{mo:02d} 三大报表</h2>"
                f"<p><a href=/ledger/{ls_id}>← 返回账套</a></p>{err}<p>{close_ui}</p>"
                f"<h3>利润表</h3>{table(inc_rows, '项目', '金额')}"
                f"<h3>资产负债表 <span class=badge>{badge}</span></h3>"
                f"{table(bs_rows, '项目', '金额')}"
                f"<h3>现金流量表（直接法）</h3>{table(cf_rows, '项目', '金额')}"
                f"<h3>账账核对</h3><p>{rec_badge}</p>"
                f"<p>勾稽：期初现金 {cf['reconcile']['opening_cash']:,.2f} + 净增加 "
                f"{cf['reconcile']['net_increase']:,.2f} = 期末现金 "
                f"{cf['reconcile']['closing_cash']:,.2f}</p>"
            )
            return _page(f"{ls.name} 报表", body)

    @app.post("/ledger/{ls_id}/close")
    def do_close(ls_id: str, year: int = Form(0), month: int = Form(0)):
        from kernel.closing import close_period
        from kernel.posting import PostingError

        subject = s_first_subject()
        try:
            with session() as s:
                close_period(s, ledger_set_id=ls_id, year=year, month=month,
                             actor={"type": "user", "id": subject})
                s.commit()
        except PostingError as e:
            return RedirectResponse(
                f"/ledger/{ls_id}/reports?year={year}&month={month}"
                f"&error={e.message_zh}",
                status_code=303,
            )
        return RedirectResponse(f"/ledger/{ls_id}/reports?year={year}&month={month}",
                                status_code=303)

    @app.get("/voucher/{vid}", response_class=HTMLResponse)
    def voucher_detail(vid: str):
        with session() as s:
            v = s.get(Voucher, vid)
            if v is None:
                return _page("错误", "<p class=err>凭证不存在</p>")
            lrows = ""
            for ln in v.lines:
                acc = s.get(Account, ln.account_id)
                lrows += (
                    f"<tr><td>{ln.line_no}</td>"
                    f"<td>{acc.code if acc else '?'}</td>"
                    f"<td>{html.escape(acc.name if acc else '?')}</td>"
                    f"<td style=text-align:right>{_fmt(ln.debit)}</td>"
                    f"<td style=text-align:right>{_fmt(ln.credit)}</td></tr>"
                )
            body = (
                f"<h2>凭证 {v.voucher_no} <span class=badge>{v.status}</span></h2>"
                f"<p>日期 {v.voucher_date}　摘要 {html.escape(v.summary or '')}</p>"
                "<table><tr><th>#</th><th>编码</th><th>科目</th><th>借方</th><th>贷方</th></tr>"
                + lrows
                + "</table><p><a href=/ledger/"
                + v.ledger_set_id
                + ">← 返回账套</a></p>"
            )
            return _page(v.voucher_no, body)

    # ---------- JSON API（React 前端 / A2UI 渲染器数据底座，P1-05） ----------

    @app.get("/api/workspace")
    def api_workspace():
        with session() as s:
            ledgers = []
            for ls in s.scalars(select(LedgerSet)).all():
                open_p = s.scalars(
                    select(Period).where(
                        Period.ledger_set_id == ls.id, Period.status == "OPEN"
                    )
                ).all()
                ledgers.append({
                    "id": ls.id,
                    "name": ls.name,
                    "standard": ls.accounting_standard,
                    "open_periods": [{"year": p.year, "month": p.month} for p in open_p],
                })
            return {"ledgers": ledgers}

    @app.get("/api/ledger/{ls_id}")
    def api_ledger(ls_id: str, year: int = 0, month: int = 0):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return JSONResponse({"error": "ledger not found"}, status_code=404)
            periods = s.scalars(
                select(Period).where(Period.ledger_set_id == ls_id).order_by(
                    Period.year.desc(), Period.month.desc()
                )
            ).all()
            period = next((p for p in periods if not year and p.status == "OPEN"), None) or (
                periods[0] if periods else None
            )
            vouchers = [
                {
                    "id": v.id, "voucher_no": v.voucher_no,
                    "date": v.voucher_date.isoformat(), "status": v.status,
                    "summary": v.summary or "",
                }
                for v in s.scalars(
                    select(Voucher).where(Voucher.ledger_set_id == ls_id)
                    .order_by(Voucher.voucher_no.desc()).limit(100)
                )
            ]
            balances = []
            if period is not None:
                for b in s.scalars(select(Balance).where(Balance.period_id == period.id)):
                    acc = s.get(Account, b.account_id)
                    balances.append({
                        "code": acc.code if acc else "?",
                        "name": acc.name if acc else "?",
                        "debit_total": f"{b.debit_total:.2f}",
                        "credit_total": f"{b.credit_total:.2f}",
                    })
            return {
                "id": ls.id, "name": ls.name, "standard": ls.accounting_standard,
                "periods": [{"year": p.year, "month": p.month, "status": p.status}
                            for p in periods],
                "current_period": {"year": period.year, "month": period.month}
                if period else None,
                "vouchers": vouchers, "balances": balances,
            }

    @app.get("/api/ledger/{ls_id}/reports")
    def api_reports(ls_id: str, year: int = 0, month: int = 0):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return JSONResponse({"error": "ledger not found"}, status_code=404)
            periods = s.scalars(
                select(Period).where(Period.ledger_set_id == ls_id).order_by(
                    Period.year.desc(), Period.month.desc()
                )
            ).all()
            period = next((p for p in periods if not year and p.status == "OPEN"), None) or (
                periods[0] if periods else None
            )
            if period is None:
                return JSONResponse({"error": "no period"}, status_code=404)
            from kernel.reconcile import reconcile_ledger
            from kernel.reporting.statements import (
                balance_sheet,
                cash_flow,
                income_statement,
            )

            def _dec(x):
                return f"{x:.2f}"

            bs = balance_sheet(s, ls_id, period.year, period.month, ls.accounting_standard)
            inc = income_statement(s, ls_id, period.year, period.month, ls.accounting_standard)
            cf = cash_flow(s, ls_id, period.year, period.month, ls.accounting_standard)
            rec = reconcile_ledger(s, ls_id, period.year, period.month, ls.accounting_standard)

            def _money(d):
                return {k: _dec(v) if isinstance(v, Decimal) else v for k, v in d.items()}

            return {
                "ledger": {"id": ls.id, "name": ls.name},
                "period": {"year": period.year, "month": period.month},
                "balance_sheet": {
                    "assets": {"total": _dec(bs["assets"]["total"]),
                               "items": [{"group": i["group"], "amount": _dec(i["amount"])}
                                         for i in bs["assets"]["items"]]},
                    "liabilities": {"total": _dec(bs["liabilities"]["total"]),
                                    "items": [{"group": i["group"], "amount": _dec(i["amount"])}
                                              for i in bs["liabilities"]["items"]]},
                    "equity": {"total": _dec(bs["equity"]["total"]),
                               "items": [{"group": i["group"], "amount": _dec(i["amount"])}
                                         for i in bs["equity"]["items"]]},
                    "balanced": bs["balanced"],
                },
                "income_statement": {
                    "items": [{"item": i["item"], "amount": _dec(i["amount"])}
                              for i in inc["items"]],
                    "net_profit": _dec(inc["net_profit"]),
                },
                "cash_flow": {
                    "items": [{"item": i["item"], "amount": _dec(i["amount"])}
                              for i in cf["items"]],
                    "operating": _dec(cf["operating"]),
                    "investing": _dec(cf["investing"]),
                    "financing": _dec(cf["financing"]),
                    "net_increase": _dec(cf["net_increase"]),
                },
                "reconcile": {"ok": rec["ok"],
                              "issues": rec["issues"]},
            }

    @app.get("/api/ledger/{ls_id}/a2ui")
    def api_a2ui(ls_id: str, year: int = 0, month: int = 0):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return JSONResponse({"error": "ledger not found"}, status_code=404)
            periods = s.scalars(
                select(Period).where(Period.ledger_set_id == ls_id).order_by(
                    Period.year.desc(), Period.month.desc()
                )
            ).all()
            period = next((p for p in periods if not year and p.status == "OPEN"), None) or (
                periods[0] if periods else None
            )
            if period is None:
                return JSONResponse({"error": "no period"}, status_code=404)
            from kernel.a2ui import build_ledger_messages

            return build_ledger_messages(
                s, ls_id, period.year, period.month, ls.name, ls.accounting_standard
            )

    @app.get("/api/voucher/{vid}")
    def api_voucher(vid: str):
        with session() as s:
            v = s.get(Voucher, vid)
            if v is None:
                return JSONResponse({"error": "voucher not found"}, status_code=404)
            lines = []
            for ln in v.lines:
                acc = s.get(Account, ln.account_id)
                lines.append({
                    "line_no": ln.line_no,
                    "account_code": acc.code if acc else "?",
                    "account_name": acc.name if acc else "?",
                    "debit": f"{ln.debit:.2f}", "credit": f"{ln.credit:.2f}",
                })
            return {
                "voucher_no": v.voucher_no, "status": v.status,
                "date": v.voucher_date.isoformat(), "summary": v.summary or "",
                "lines": lines,
            }

    # ---------- 企业微信回调（审核与交互端，P4-W1） ----------

    @app.get("/wecom/callback")
    def wecom_verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        """企微回调 URL 验证：解密 echostr 原样返回明文。"""
        from kernel import wecom

        try:
            token = wecom._cfg("WECOM_TOKEN")
            if not wecom.verify_signature(token, msg_signature, timestamp, nonce, echostr):
                return Response("签名校验失败", status_code=400)
            plain = wecom.decrypt_message(echostr)
            return Response(plain, media_type="text/plain")
        except wecom.WecomError as e:
            return Response(str(e), status_code=400)

    @app.post("/wecom/callback")
    async def wecom_callback(
        request: Request, msg_signature: str, timestamp: str, nonce: str
    ):
        """企微事件分发：文本指令 → 被动回复；模板卡片按钮 → 状态机 + 卡片更新。"""
        from kernel import wecom
        from kernel.posting import PostingError

        body = await request.body()
        try:
            token = wecom._cfg("WECOM_TOKEN")
            encrypt = wecom.parse_encrypt_xml(body)
            if not wecom.verify_signature(token, msg_signature, timestamp, nonce, encrypt):
                return Response("签名校验失败", status_code=400)
            plain = wecom.decrypt_message(encrypt)
            msg = ET.fromstring(plain)
            msg_type = msg.findtext("MsgType") or ""
            from_user = msg.findtext("FromUserName") or ""

            if msg_type == "text":
                content = (msg.findtext("Content") or "").strip()
                with session() as s:
                    try:
                        reply = wecom.handle_text_command(s, content, from_user)
                    except PostingError as e:
                        reply = f"❌ {e.message_zh}"
                return Response(
                    wecom.build_text_reply_xml(reply, to_user=from_user),
                    media_type="text/plain",
                )

            if msg_type == "event":
                event = msg.findtext("Event") or ""
                if event != "template_card_event":
                    return Response("", media_type="text/plain")
                event_key = msg.findtext("EventKey") or ""
                with session() as s:
                    try:
                        result = wecom.handle_card_event(s, event_key, from_user)
                    except PostingError as e:
                        result = f"error:{e.message_zh}"
                if result.startswith(("approved:", "rejected:")):
                    state, _, voucher_no = result.partition(":")
                    # 按钮处理成功：主动更新卡片为已完成态；失败则文本告知
                    try:
                        vid = event_key.partition(":")[2]
                        wecom.update_card(
                            from_user, vid, "已批准" if state == "approved" else "已驳回",
                            voucher_no,
                        )
                    except wecom.WecomError as e:
                        print(f"[wecom] 卡片更新失败: {e}")
                        wecom.send_text(
                            from_user,
                            f"✅ {voucher_no} 已{'批准' if state == 'approved' else '驳回'}"
                            "（卡片更新失败）",
                        )
            return Response("", media_type="text/plain")
        except wecom.WecomError as e:
            return Response(str(e), status_code=400)

    # ---------- React 构建产物挂载（P1-05，/ui/） ----------
    dist = _REPO_ROOT / "web" / "dist"
    if dist.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/ui", StaticFiles(directory=str(dist), html=True), name="ui")

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        build_app(os.environ.get("XERP_DB")),
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8001")),
    )


if __name__ == "__main__":
    main()

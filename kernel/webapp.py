"""XErp Web 最小界面（P0-13，HTML 服务端渲染兜底；正式 A2UI 在 P1）。

运行: python -m kernel.webapp   （默认 http://127.0.0.1:8001，XERP_DB 可覆盖）
页面: / 工作区 · /ledger/{id} 凭证+余额 · /voucher/{id} 凭证详情 · /init 建账向导
"""

from __future__ import annotations

import html
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
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
                f"<h2>账套：{html.escape(ls.name)}</h2>"
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

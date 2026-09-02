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
from kernel.db.models import (
    Account,
    Balance,
    LedgerSet,
    Period,
    Subject,
    Voucher,
    VoucherLine,
)

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
.warn{color:#7a4a00;background:#fff7e6;border:1px solid #ffd591;
      padding:10px 14px;border-radius:6px;margin:8px 0;font-size:14px;line-height:1.7}
.warn ul{margin:6px 0 6px 20px;padding:0}
a{color:#185fa5;text-decoration:none}a:hover{text-decoration:underline}
input,textarea{width:100%;padding:6px;margin:4px 0;box-sizing:border-box}
input[type=checkbox]{width:auto;margin-right:6px;vertical-align:middle}
label{font-size:14px;cursor:pointer;user-select:none}
button{padding:6px 18px;background:#185fa5;color:#fff;border:0;border-radius:6px;cursor:pointer}
.userbar{float:right;font-size:13px;color:#555;margin-top:-34px}
.userbar b{color:#1a1a1a}
select{width:100%;padding:6px;margin:4px 0;box-sizing:border-box}
</style>"""


def _page(title: str, body: str, user: str | None = None) -> HTMLResponse:
    """渲染页面。user 非空时在右上角显示当前身份与退出入口——
    让「这笔账记在谁名下」始终可见，是审计可追溯的第一道防线。"""
    userbar = ""
    if user:
        userbar = (
            f'<div class="userbar">当前身份：<b>{html.escape(user)}</b>'
            f'　<a href="/logout">退出</a></div>'
        )
    return HTMLResponse(
        f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
        f"<title>{html.escape(title)} · XErp</title>{_CSS}</head>"
        f"<body><h1>XErp <span class=badge>v0.1-dev</span></h1>{userbar}{body}</body></html>"
    )


def _fmt(d) -> str:
    return f"{(d or 0):.2f}"


def _opening_form(ls_id: str, existing: list) -> str:
    """期初导入表单。已存在期初时切换为警告态：默认导入会被内核拒绝，
    必须显式勾选「覆盖」才走 force 红字冲销重导。"""
    textarea = (
        '<textarea name=lines_text rows=6 '
        'placeholder="1002,200000,&#10;3001,,200000"></textarea>'
    )
    if not existing:
        return (
            f'<form method=post action="/ledger/{ls_id}/opening">'
            "每行一条：<code>科目编码,借方,贷方</code>（留空填 0 亦可省略为空段）<br>"
            f"{textarea}<br>"
            '<button type=submit>导入（试算平衡校验）</button></form>'
        )

    rows = "".join(
        f"<li>{html.escape(v.voucher_no)}（{v.voucher_date}）</li>" for v in existing
    )
    return (
        '<div class="warn">'
        "<b>本账套已导入期初余额</b>，重复导入会使期初翻倍，因此默认拒绝。<ul>"
        f"{rows}</ul>"
        "如需修正，请在下方勾选「覆盖」后重新提交 —— "
        "系统会先<b>红字冲销</b>上述期初（余额归零、审计留痕），再导入新数据。"
        "</div>"
        f'<form method=post action="/ledger/{ls_id}/opening">'
        "重新导入（每行一条 <code>科目编码,借方,贷方</code>）：<br>"
        f"{textarea}<br>"
        '<label><input type=checkbox name=force> 覆盖：红字冲销旧期初后重新导入</label><br>'
        '<button type=submit>覆盖导入</button></form>'
    )


def build_app(db_url: str | None = None) -> FastAPI:
    url = db_url or os.environ.get("XERP_DB") or f"sqlite:///{_REPO_ROOT / 'ledgeros_dev.db'}"
    from sqlalchemy import create_engine

    engine = create_engine(url)
    Base.metadata.create_all(engine)

    app = FastAPI(title="XErp Web")

    def session() -> Session:
        return Session(engine)

    # ---------- 认证（P0-4/P0-5 止血） ----------
    # 修复前 actor 恒为「数据库里第一个主体」，NO_SELF_APPROVAL 在 Web 端形同虚设。
    # 现在：所有页面/API 必须携带已签名会话，actor 取自会话中显式选择的身份。

    from kernel import webauth

    # 无需登录即可访问的路径（企微回调由签名校验保护，不能走会话）
    PUBLIC_PATHS = ("/login", "/logout", "/wecom/callback")

    def _is_fresh_install(s: Session) -> bool:
        """库中尚无任何操作身份 —— 即全新安装、从未建账。"""
        return s.scalars(select(Subject.id).limit(1)).first() is None

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        # 先置默认值：公开路径下的页面模板同样会读 request.state.subject_name
        request.state.subject_id = ""
        request.state.subject_name = ""
        path = request.url.path
        public = any(path.startswith(p) for p in PUBLIC_PATHS)
        if not public and path.startswith("/init"):
            # 全新安装时放行建账向导。建账是创建第一个身份的唯一途径，
            # 若在此处拦截会形成「无身份 → 不能登录 → 不能建账 → 无身份」死锁，
            # 全新安装的客户将彻底进不去系统。口令防护下沉到 /init 表单内。
            with session() as s:
                public = _is_fresh_install(s)
        if not public:
            payload = webauth.parse_token(request.cookies.get(webauth.COOKIE_NAME))
            if payload is None:
                from urllib.parse import quote

                return RedirectResponse(
                    f"/login?next={quote(path, safe='')}", status_code=303
                )
            # 身份可能已被删除，这里不做 DB 校验（避免每个请求多一次查询），
            # 由各写入点在需要时用内核侧校验兜底
            request.state.subject_id = payload.get("sub") or ""
            request.state.subject_name = payload.get("name") or ""
        return await call_next(request)

    def _safe_next(nxt: str) -> str:
        """防开放重定向：只允许站内相对路径。"""
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return nxt
        return "/"

    @app.get("/login", response_class=HTMLResponse)
    def login_form(next: str = "", error: str = ""):
        with session() as s:
            subjects = s.scalars(
                select(Subject).order_by(Subject.display_name)
            ).all()
            fresh = _is_fresh_install(s)
            opts = "".join(
                f'<option value="{sub.id}">{html.escape(sub.display_name)}'
                f"（{'人员' if sub.type == 'user' else 'Agent'} · L{sub.autonomy_level}）</option>"
                for sub in subjects
            )
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        if fresh:
            # 全新安装：给出去路，而不是让用户对着空下拉框干瞪眼
            return _page(
                "首次建账",
                f"{err}<h2>欢迎使用 XErp</h2>"
                '<div class="warn">系统中还没有任何操作身份 —— 这是全新安装。'
                "请先通过<b>建账向导</b>创建账套，届时会自动创建第一个（管理员）身份，"
                "建账完成后直接进入系统，无需再登录一次。</div>"
                '<p><a href="/init">→ 建账向导（创建账套与第一个身份）</a></p>',
            )
        mode_note = (
            '<div class="warn"><b>单机开放模式</b>：未设置 <code>XERP_WEB_PASSWORD</code>，'
            "口令栏留空即可登录。生产部署请在环境变量中设置口令。</div>"
            if webauth.is_open_mode()
            else ""
        )
        body = (
            "<h2>登录 · 选择操作身份</h2>"
            f"{err}{mode_note}"
            '<form method=post action="/login">'
            f'<input type=hidden name=next value="{html.escape(_safe_next(next))}">'
            "<label>身份（决定凭证记在谁名下，影响审批与审计链）</label>"
            f'<select name=subject_id>{opts}</select>'
            "<label>口令</label>"
            '<input type=password name=password placeholder="开放模式下留空">'
            '<br><button type=submit>登录</button></form>'
        )
        return _page("登录", body)

    @app.post("/login")
    def login_submit(
        subject_id: str = Form(""), password: str = Form(""), next: str = Form("")
    ):
        if not webauth.check_password(password):
            return RedirectResponse("/login?error=口令错误", status_code=303)
        with session() as s:
            sub = s.get(Subject, subject_id)
            if sub is None:
                return RedirectResponse("/login?error=请选择有效身份", status_code=303)
            name = sub.display_name
        resp = RedirectResponse(_safe_next(next), status_code=303)
        resp.set_cookie(
            webauth.COOKIE_NAME,
            webauth.issue_token(subject_id, name),
            httponly=True,
            samesite="lax",
            max_age=webauth.SESSION_TTL_SECONDS,
        )
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(webauth.COOKIE_NAME)
        return resp

    # ---------- 工作区 ----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
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
            return _page("工作区", body, request.state.subject_name)

    # ---------- 建账向导 ----------

    @app.get("/init", response_class=HTMLResponse)
    def init_form(request: Request, error: str = ""):
        with session() as s:
            fresh = _is_fresh_install(s)
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        # 全新安装时 /init 免登录（否则无身份→不能登录→不能建账→死锁）。
        # 为免被他人抢先建账，此时要求输入管理员口令（开放模式下无需）。
        boot_note = (
            '<div class="warn"><b>首次使用</b>：本次建账会同时创建第一个（管理员）身份，'
            "完成后自动以该身份登录。请在受信任的网络环境下操作；"
            "建账完成后 Web 端即刻上锁，后续访问均需登录。</div>"
            if fresh
            else ""
        )
        pwd_field = (
            "<label>管理员口令</label>"
            '<input type=password name=password required '
            'placeholder="环境变量 XERP_WEB_PASSWORD"><br>'
            if fresh and not webauth.is_open_mode()
            else ""
        )
        body = (
            err
            + boot_note
            + "<h2>建账向导</h2><form method=post action=/init>"
            + "账套名称<br><input name=name required><br>"
            + "所有者姓名（制单人身份）<br><input name=owner_name required><br>"
            + pwd_field
            + "<br><button type=submit>创建（自动导入小企业会计准则科目）</button></form>"
            + "<p>创建后请在账套页录入期初余额（试算平衡自动校验）。</p>"
        )
        return _page("建账", body, request.state.subject_name)

    @app.post("/init")
    def init_submit(
        request: Request,
        name: str = Form(""),
        owner_name: str = Form(""),
        password: str = Form(""),
    ):
        name, owner_name = name.strip(), owner_name.strip()
        if not name or not owner_name:
            return RedirectResponse("/init?error=账套名与所有者姓名必填", status_code=303)
        # 未登录（全新安装）时校验管理员口令，防止他人抢先建账占下管理员身份
        anonymous = not getattr(request.state, "subject_id", "")
        if anonymous and not webauth.check_password(password):
            return RedirectResponse("/init?error=口令错误", status_code=303)
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
            owner = Subject(type="user", display_name=owner_name, autonomy_level=3)
            s.add(owner)
            s.commit()
            resp = RedirectResponse(f"/ledger/{ls.id}", status_code=303)
            if anonymous:
                # 建账即登录：全新安装时不必建完再去登录页选一次身份
                resp.set_cookie(
                    webauth.COOKIE_NAME,
                    webauth.issue_token(owner.id, owner_name),
                    httponly=True,
                    samesite="lax",
                    max_age=webauth.SESSION_TTL_SECONDS,
                )
            return resp

    # ---------- 账套仪表盘 ----------

    @app.get("/ledger/{ls_id}", response_class=HTMLResponse)
    def ledger_dashboard(request: Request, ls_id: str, year: int = 0,
                        month: int = 0, error: str = ""):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>", request.state.subject_name)
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
            # 只列「生效中」的期初：已冲销的旧期初仍在库（append-only 审计需要），
            # 但展示给会计时必须排除，否则会显示两份期初让人以为翻倍。
            from kernel.opening import _active_opening_vouchers

            opening_vouchers = _active_opening_vouchers(s, ls_id)

            vrows = ""
            for v in vouchers:
                vrows += (
                    f"<tr><td><a href=/voucher/{v.id}>{v.voucher_no}</a></td>"
                    f"<td>{v.voucher_date}</td><td><span class=badge>{v.status}</span></td>"
                    f"<td>{html.escape(v.summary or '')}</td></tr>"
                )

            # 科目余额表：期初 / 本期发生额 / 期末余额 三栏分列。
            # 不能只丢一张「发生额投影」给会计 —— 期初余额不是本期发生额，
            # 混在一起会让本期发生额凭空虚增（force 重导时还会翻倍），
            # 会计拿去对账会立刻对不上，直接不信任系统。
            brows = ""
            if period is not None:
                from kernel.opening import is_opening_voucher
                from kernel.reporting.statements import ending_balance

                accs = {a.id: a for a in s.scalars(select(Account)).all()}
                opening_agg: dict[str, list] = {}
                current_agg: dict[str, list] = {}
                for v in s.scalars(
                    select(Voucher).where(
                        Voucher.ledger_set_id == ls_id,
                        Voucher.period_id == period.id,
                        Voucher.status == "POSTED",
                    )
                ):
                    # 期初口径 = 期初凭证 + 其红字冲销（冲销是对期初的调整，
                    # 不属本期业务）；两者相抵后的净额才是真实期初。
                    tgt = (
                        opening_agg if is_opening_voucher(v.voucher_no) else current_agg
                    )
                    for ln in s.scalars(
                        select(VoucherLine).where(VoucherLine.voucher_id == v.id)
                    ):
                        d, c = tgt.get(ln.account_id, (Decimal("0"), Decimal("0")))
                        tgt[ln.account_id] = (d + ln.debit, c + ln.credit)
                for aid in sorted(set(opening_agg) | set(current_agg)):
                    acc = accs.get(aid)
                    if acc is None:
                        continue
                    od, oc = opening_agg.get(aid, (Decimal("0"), Decimal("0")))
                    cd, cc = current_agg.get(aid, (Decimal("0"), Decimal("0")))
                    ob = ending_balance(acc.code, od, oc)
                    cb = ending_balance(acc.code, od + cd, oc + cc)
                    brows += (
                        f"<tr><td>{acc.code}</td>"
                        f"<td>{html.escape(acc.name)}</td>"
                        f'<td style=text-align:right>{_fmt(ob)}</td>'
                        f'<td style=text-align:right>{_fmt(cd)}</td>'
                        f'<td style=text-align:right>{_fmt(cc)}</td>'
                        f'<td style=text-align:right><b>{_fmt(cb)}</b></td></tr>'
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
                f"<h3>科目余额表 {period.year}-{period.month:02d}</h3>"
                "<table><tr><th rowspan=2>编码</th><th rowspan=2>科目</th>"
                "<th rowspan=2>期初余额</th><th colspan=2>本期发生额</th>"
                "<th rowspan=2>期末余额</th></tr>"
                "<tr><th>借方</th><th>贷方</th></tr>"
                + (brows or "<tr><td colspan=6>本期间尚无过账数据</td></tr>")
                + "</table>"
                f"""
<h3>导入期初余额</h3>
{_opening_form(ls_id, opening_vouchers)}
"""
            )
            return _page(f"{ls.name}", body, request.state.subject_name)

    @app.post("/ledger/{ls_id}/opening")
    def opening_import(
        request: Request, ls_id: str, lines_text: str = Form(""), force: str = Form("")
    ):
        from kernel.opening import import_opening_balances
        from kernel.posting import PostingError
        back = f"/ledger/{ls_id}"
        actor = {"type": "user", "id": request.state.subject_id}
        parsed = []
        for raw in (lines_text or "").splitlines():
            parts = [x.strip() for x in raw.split(",")]
            if len(parts) < 3 or not parts[0]:
                continue
            parsed.append(
                {"account_code": parts[0], "debit": parts[1], "credit": parts[2]}
            )
        if not parsed:
            return RedirectResponse(
                f"{back}?error=未识别到任何有效行，格式：科目编码,借方,贷方",
                status_code=303,
            )
        try:
            with session() as s:
                import_opening_balances(
                    s,
                    ledger_set_id=ls_id,
                    actor=actor,
                    lines=parsed,
                    force=(force == "on"),
                )
                s.commit()
        except PostingError as e:
            return RedirectResponse(f"{back}?error={e.message_zh}", status_code=303)
        return RedirectResponse(back, status_code=303)

    # ---------- 凭证详情 ----------

    @app.get("/ledger/{ls_id}/reports", response_class=HTMLResponse)
    def reports(request: Request, ls_id: str, year: int = 0, month: int = 0,
             error: str = ""):
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>", request.state.subject_name)
            periods = s.scalars(
                select(Period).where(Period.ledger_set_id == ls_id).order_by(
                    Period.year.desc(), Period.month.desc()
                )
            ).all()
            period = next((p for p in periods if not year and p.status == "OPEN"), None) or (
                periods[0] if periods else None
            )
            if period is None:
                return _page(f"{ls.name}", "<p class=err>尚无期间</p>",
                            request.state.subject_name)
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
                return _page(f"{ls.name}",
                            f'<p class=err>报表生成失败: {html.escape(str(e))}</p>',
                            request.state.subject_name)

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
            return _page(f"{ls.name} 报表", body, request.state.subject_name)

    @app.post("/ledger/{ls_id}/close")
    def do_close(
        request: Request, ls_id: str, year: int = Form(0), month: int = Form(0)
    ):
        from kernel.closing import close_period
        from kernel.posting import PostingError

        actor = {"type": "user", "id": request.state.subject_id}
        try:
            with session() as s:
                close_period(s, ledger_set_id=ls_id, year=year, month=month, actor=actor)
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
    def voucher_detail(request: Request, vid: str):
        with session() as s:
            v = s.get(Voucher, vid)
            if v is None:
                return _page("错误", "<p class=err>凭证不存在</p>", request.state.subject_name)
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
            return _page(v.voucher_no, body, request.state.subject_name)

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
                response_code = msg.findtext("ResponseCode") or ""
                with session() as s:
                    try:
                        result = wecom.handle_card_event(s, event_key, from_user)
                    except PostingError as e:
                        result = f"error:{e.message_zh}"
                if result.startswith(("approved:", "rejected:")):
                    state, _, voucher_no = result.partition(":")
                    # 按钮处理成功：用回调 ResponseCode 整体替换卡片为已完成态；
                    # 失败则文本告知（状态机已落库，不影响审批结果）
                    try:
                        vid = event_key.partition(":")[2]
                        wecom.update_card(
                            from_user, response_code, vid,
                            "已批准 ✅" if state == "approved" else "已驳回 ↩️",
                            voucher_no,
                        )
                    except wecom.WecomError as e:
                        print(f"[wecom] 卡片更新失败: {e}")
                        wecom.send_text(
                            from_user,
                            f"✅ {voucher_no} 已{'批准' if state == 'approved' else '驳回'}"
                            "（卡片更新失败，请以本条为准）",
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

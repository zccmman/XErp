"""XErp Web 最小界面（P0-13，HTML 服务端渲染兜底；正式 A2UI 在 P1）。

运行: python -m kernel.webapp   （默认 http://127.0.0.1:8001，XERP_DB 可覆盖）
页面: / 工作区 · /ledger/{id} 凭证+余额 · /voucher/{id} 凭证详情 · /init 建账向导
"""

from __future__ import annotations

import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.coa import CoaImportError, import_chart_of_accounts, load_template_rows
from kernel.classic import period_zh, status_zh, voucher_prefix
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
from kernel.ontology import (  # noqa: E402
    check_lines as _ontology_check,
    load_rules as _ontology_rules,
    load_relations as _ontology_relations,
    template_attrs as _ontology_attrs,
)
from kernel.operator import render_fragment as _render_operator  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]

_CSS = """<style>
/* 怀旧皮肤：复刻经典财务软件的观感——深蓝标题栏、宋体正文、密集网格、
   印章式状态。改的是观感不是结构，语义与内核术语保持一致。 */
body{font-family:SimSun,'宋体','NSimSun',serif;
     max-width:1000px;margin:0 auto;padding:0 0 40px;color:#1a1a1a;font-size:14px;
     background:#eef1f5}
.wrap{background:#fff;border:1px solid #9fb0c4;border-top:0;padding:16px 20px 24px}
h1{font-size:16px;margin:0;padding:10px 20px;color:#fff;background:#1f4e79;
   letter-spacing:2px;font-weight:bold}
h1 .badge{background:#3d7ab8;color:#eaf2fb;margin-left:8px}
h2{font-size:15px;margin:22px 0 8px;padding-left:8px;border-left:4px solid #1f4e79}
h3{font-size:14px;margin:18px 0 6px;color:#1f4e79}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{border:1px solid #a9b8c8;padding:4px 8px;text-align:left}
th{background:#dbe5f1;color:#1f3d5c;font-weight:bold}
tbody tr:nth-child(even){background:#f6f8fa}
.badge{display:inline-block;padding:0 6px;border-radius:2px;font-size:12px;
       border:1px solid #888;color:#555;background:#f2f2f2}
.st-DRAFT{border-color:#8a8a8a;color:#5a5a5a;background:#f0f0f0}
.st-PUSHED{border-color:#c98a00;color:#8a5d00;background:#fff6de}
.st-APPROVED{border-color:#1f4e79;color:#1f4e79;background:#e3edf8}
.st-POSTED{border-color:#2e7d32;color:#1b5e20;background:#e8f5e9;font-weight:bold}
.st-REJECTED{border-color:#a32d2d;color:#a32d2d;background:#fcebeb}
.st-WITHDRAWN{border-color:#8a8a8a;color:#5a5a5a;background:#eceff1}
.err{color:#a32d2d;background:#fcebeb;border:1px solid #e5b4b4;padding:8px 12px}
.warn{color:#7a4a00;background:#fff7e6;border:1px solid #ffd591;
      padding:10px 14px;margin:8px 0;font-size:13px;line-height:1.7}
.warn ul{margin:6px 0 6px 20px;padding:0}
.ok{color:#1b5e20;background:#e8f5e9;border:1px solid #a5d6a7;padding:10px 14px;
    margin:8px 0;font-size:13px;line-height:1.7}
a{color:#154c8a;text-decoration:none}a:hover{text-decoration:underline}
.nav{font-size:13px;margin:0 0 14px;padding:6px 10px;background:#dbe5f1;border:1px solid #a9b8c8}
.ops{background:#f4f7fb;border:1px solid #b9c8d8;padding:12px 16px;margin:14px 0}
.ops form{margin:6px 0}
button.danger{background:#a32d2d}
input,textarea{font-family:inherit;width:100%;padding:5px;margin:4px 0;box-sizing:border-box;
               border:1px solid #a9b8c8}
input[type=checkbox]{width:auto;margin-right:6px;vertical-align:middle}
label{font-size:13px;cursor:pointer;user-select:none}
button{padding:5px 16px;background:#1f4e79;color:#fff;border:1px solid #163d5e;
       border-radius:2px;cursor:pointer;font-family:inherit;font-size:13px}
button:hover{background:#2b62a3}
button.ghost{background:#fff;color:#1f4e79}
.userbar{float:right;font-size:12px;color:#eaf2fb;margin-top:-26px;margin-right:20px}
.userbar b{color:#fff}.userbar a{color:#cfe0f2}
select{font-family:inherit;width:100%;padding:4px;margin:4px 0;box-sizing:border-box;
       border:1px solid #a9b8c8}
/* 工具条：老软件的「制单/审核/记账」一排按钮，肌肉记忆的落点 */
.toolbar{background:#dbe5f1;border:1px solid #a9b8c8;padding:6px 10px;margin:10px 0;
         font-size:13px}
.toolbar a,.toolbar span.sep{color:#1f4e79;margin-right:14px}
.toolbar .sep{color:#9fb0c4}
.num{text-align:right;font-family:'Courier New',monospace}
.vno{font-family:'Courier New',monospace;font-weight:bold}
/* 结账体检清单 */
.check{list-style:none;padding:0;margin:8px 0}
.check li{border:1px solid #d5dde6;padding:8px 12px;margin:6px 0;font-size:13px;line-height:1.7}
.check li.pass{border-left:4px solid #2e7d32;background:#f3faf4}
.check li.fail{border-left:4px solid #a32d2d;background:#fdf4f4}
.check .item{font-weight:bold}
.check .hint{color:#8a5d00}
/* 算子 · 账本精灵（产品方案 §8）；右上角常驻 24×24；hover 展开 80×80 详情卡。
   五条红线在 operator.py docstring 中钉死。 */
.op-container{position:fixed;top:8px;right:16px;z-index:1000;display:inline-block}
.op-container.op-hidden{display:none}
.op-container .op-svg{cursor:help;vertical-align:middle}
.op-container .op-detail{display:none;position:absolute;top:30px;right:0;
   background:#fff;border:1px solid #9fb0c4;padding:10px 12px;border-radius:4px;
   box-shadow:0 2px 6px rgba(0,0,0,.08);white-space:nowrap;font-size:13px;
   color:#1a1a1a}
.op-container:hover .op-detail,.op-container:focus .op-detail{display:block}
.op-detail-text{margin-top:6px;color:#1f4e79;font-weight:bold;text-align:center}
.op-detail-svg{display:flex;justify-content:center}
</style>"""


def _page(title: str, body: str, user: str | None = None,
          show_operator: bool = False) -> HTMLResponse:
    """渲染页面。user 非空时在右上角显示当前身份与退出入口——
    让「这笔账记在谁名下」始终可见，是审计可追溯的第一道防线。

    show_operator=True 时在右上角注入算子 fragment（仅 M1 制单页开启，
    遵守产品方案 §8.2 "不能喧宾夺主"红线；其他页面零改动）。
    """
    userbar = ""
    if user:
        userbar = (
            f'<div class="userbar">当前身份：<b>{html.escape(user)}</b>'
            f'　<a href="/logout">退出</a></div>'
        )
    operator_html = _render_operator() if show_operator else ''
    nav = ('<div class=nav><a href="/">工作区</a> · '
           '<a href="/todo">审批待办</a></div>')
    return HTMLResponse(
        f"<!doctype html><html lang=zh><head><meta charset=utf-8>"
        f"<title>{html.escape(title)} · XErp</title>{_CSS}</head>"
        f"<body><h1>XErp <span class=badge>v0.1-dev</span></h1>{userbar}{operator_html}"
        f'<div class=wrap>{nav}{body}</div></body></html>'
    )


def _fmt(d) -> str:
    return f"{(d or 0):.2f}"


def st_badge(status: str) -> str:
    """状态徽章：中文术语 + 配色。

    老会计认的是「未审核 / 已记账」这几个字，不是 DRAFT / POSTED。
    颜色只是辅助，语义由文字承担——色盲用户与打印场景都不能丢信息。
    """
    from kernel.classic import status_zh  # noqa: F401  顶部已导入，此处显式标注来源

    return f'<span class="badge st-{html.escape(status)}">{html.escape(status_zh(status))}</span>'


def _toolbar(*items: str) -> str:
    """工具条。老软件的肌肉记忆落点：一排「制单 / 审核 / 记账 / 结账」。"""
    return '<div class=toolbar>' + '<span class=sep>|</span>'.join(items) + '</div>'


def _guide_card(guide: dict | None, ls_id: str) -> str:
    """本月引导卡（超级AI总账 · 阶段0）：把 month_end_guide 的结论讲给人听。

    会计一进账套，最先看到的不是凭证流水，而是「这个月还差什么、下一步干嘛」。
    本卡片只复用内核 phase/counts/next_action/close 的结论做展示与跳转，
    不做任何二次推断——文案与 MCP 工具 month_end_guide 同源，两端口径永远一致。
    """
    if guide is None:
        # 账套连期间都没有：与内核 no_phase 口径一致，指向建账向导。
        return ('<div class=warn><b>本账套尚未建账</b>——请先初始化期间，'
                "再开始记账。</div>")
    head = (
        f'<b>{html.escape(guide["phase_zh"])}</b>　'
        f'{html.escape(guide["next_action"])}'
    )
    if guide["phase"] == "closed":
        return f'<div class=ok>{head}</div>'
    if guide["phase"] == "closing_ready":
        return (
            f'<div class=ok>{head}　'
            f'<a href="/ledger/{ls_id}/close">去月末结账 →</a></div>'
        )
    parts = [f'<div class=warn>{head}']
    c = guide.get("counts") or {}
    if c:
        parts.append(
            '<div style=margin-top:6px>'
            f'凭证盘点：未审核草稿 {c.get("draft", 0)} 张 · '
            f'待审核 {c.get("pushed", 0)} 张 · '
            f'已审待记账 {c.get("approved", 0)} 张 · '
            f'已记账 {c.get("posted", 0)} 张</div>'
        )
    if guide["phase"] == "daily_pending":
        parts.append(
            '<div style=margin-top:6px>'
            f'<a href="/todo">去审批待办 →</a>　'
            f'<a href="/ledger/{ls_id}/voucher/new">继续制单 →</a></div>'
        )
    close = guide.get("close")
    if close:
        lis = ""
        for chk in close["checks"]:
            cls = "pass" if chk["passed"] else "fail"
            mark = "√" if chk["passed"] else "×"
            hint = chk.get("hint") or ""
            hint_html = (
                f'<div class=hint>→ {html.escape(hint)}</div>' if hint else ""
            )
            lis += (
                f'<li class={cls}><span class=item>{mark} '
                f'{html.escape(chk["item"])}</span>　'
                f'{html.escape(chk["detail"])}{hint_html}</li>'
            )
        parts.append(
            f'<ul style="list-style:none;padding:0;margin:8px 0 0">{lis}</ul>'
            '<div style=margin-top:6px>'
            f'<a href="/ledger/{ls_id}/close">查看结账体检 →</a></div>'
        )
    parts.append("</div>")
    return "".join(parts)


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


# ---------- 制单表单（G1 制单闭环） ----------
#
# 会计一天要在这一屏上花掉最多时间，三件事决定它好不好用：
#   1. 科目能快速定位——144 个科目纯下拉没法用，加一个过滤框按「编码或名称」子串筛；
#   2. 借贷是否平衡当场可见——提交后被内核拒回来重填是最差体验；
#   3. 提交失败必须保留已填内容——一行分录手打完再丢一次，没人愿意用第二次。
# 因此本页不走「redirect + ?error=」的既有模式，而是失败时原地重渲染并回填。

_FORM_JS = """<script>
function filterAccounts(q){
  q = (q||'').trim().toLowerCase();
  document.querySelectorAll('select.acct').forEach(function(sel){
    for (var i=0;i<sel.options.length;i++){
      var opt = sel.options[i];
      // 永不隐藏当前选中项：否则用户筛完看到空框，以为自己没选过
      if (opt.selected) { opt.hidden = false; continue; }
      opt.hidden = !!q && opt.textContent.toLowerCase().indexOf(q) < 0;
    }
  });
}
function recalc(){
  var d=0, c=0;
  document.querySelectorAll('input.amt-debit').forEach(function(i){
    d += parseFloat(i.value)||0; });
  document.querySelectorAll('input.amt-credit').forEach(function(i){
    c += parseFloat(i.value)||0; });
  var diff = d - c, ok = Math.abs(diff) < 0.005;
  var sd=document.getElementById('sumDebit'), sc=document.getElementById('sumCredit'),
      sf=document.getElementById('sumDiff'), hint=document.getElementById('balanceHint');
  if(sd) sd.textContent = d.toFixed(2);
  if(sc) sc.textContent = c.toFixed(2);
  if(sf){ sf.textContent = diff.toFixed(2); sf.style.color = ok?'#3b6d11':'#a32d2d'; }
  if(hint){
    hint.textContent = ok ? '借贷已平衡，可以提交'
                          : '借贷不等，差额 ' + diff.toFixed(2) + '（提交会被拒绝）';
    hint.style.color = ok?'#3b6d11':'#a32d2d';
  }
}
function addLine(){
  var tb = document.getElementById('lines');
  var rows = tb.querySelectorAll('tr');
  var row = rows[rows.length-1].cloneNode(true);
  row.querySelectorAll('input').forEach(function(i){ i.value=''; });
  var sel = row.querySelector('select.acct');
  if (sel) sel.selectedIndex = 0;
  tb.appendChild(row);
  recalc();
}
function delLine(btn){
  var tb = document.getElementById('lines');
  if (tb.querySelectorAll('tr').length <= 1) { return; }  // 至少留一行
  btn.closest('tr').remove();
  recalc();
}
document.addEventListener('DOMContentLoaded', function(){
  var tb = document.getElementById('lines');
  if (tb) {
    // 事件委托：新增/克隆的行自动获得监听，无需逐个绑定
    tb.addEventListener('input', recalc);
    tb.addEventListener('click', function(e){
      if (e.target && e.target.classList.contains('del')) delLine(e.target);
    });
  }
  recalc();
});
</script>"""


def _default_voucher_date(period) -> date:
    """默认制单日期：今天落在开放期间内就用今天，否则钳到期间边界。

    钳到**期末**而非期初——会计在 9 月初补录 8 月凭证是常态，
    给 8-01 反而要再改一次日期。
    """
    from calendar import monthrange

    today = date.today()
    if period is None:
        return today
    start = date(period.year, period.month, 1)
    end = date(period.year, period.month, monthrange(period.year, period.month)[1])
    if today < start:
        return start
    if today > end:
        return end
    return today


def _account_options(accounts: list, selected: str = "") -> str:
    opts = ['<option value="">（选择科目）</option>']
    for a in accounts:
        sel = " selected" if a.code == selected else ""
        # 文本带编码：浏览器原生键盘搜索可直接敲「1002」跳到银行存款
        opts.append(
            f'<option value="{html.escape(a.code)}"{sel}>'
            f"{html.escape(a.code)} {html.escape(a.name)}</option>"
        )
    return "".join(opts)


def _parse_aux_dims(raw: str) -> dict | None:
    """「customer=甲公司;department=销售部」→ dict；格式非法返回 None。

    分隔符兼容中英文分号/逗号；k=v 风格与 coa 模板 attrs 列一致，
    会计一次学习两处通用。空串返回空 dict（无维度，合法）。
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    dims: dict = {}
    for part in re.split(r"[;；,，]", raw):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            return None
        k, _, val = part.partition("=")
        k, val = k.strip(), val.strip()
        if not k or not val:
            return None
        dims[k] = val
    return dims


def _voucher_form(ls_id: str, accounts: list, period, values: dict | None = None,
                  summaries: list | None = None) -> str:
    """渲染制单表单。values 非空表示提交失败后的回填。

    怀旧设计：凭证类别是老会计制单的第一下手感——先选收/付/转，再录分录。
    这里默认「自动」，由前端按资金流向实时预判并回显，与内核
    classify_voucher_type 同一套规则，避免界面与内核判定打架。
    """
    from kernel.classic import CASH_BANK_CODES, period_zh

    v = values or {}
    vdate = v.get("voucher_date") or _default_voucher_date(period)
    summary = v.get("summary") or ""
    vtype = str(v.get("voucher_type") or "")
    rows = list(v.get("rows") or [])
    while len(rows) < 4:  # 至少 4 行：一借一贷是常态，留两行给复杂分录
        rows.append({"account_code": "", "debit": "", "credit": ""})

    if period is None:
        head = "<p class=err>本账套尚无会计期间，无法制单。请先初始化期间。</p>"
    else:
        from calendar import monthrange

        last = monthrange(period.year, period.month)[1]
        head = (
            f"<p>记账期间：<b>{period.year}-{period.month:02d}</b>"
            f"（{period_zh(period.status)}）　日期须落在 "
            f"{period.year}-{period.month:02d}-01 ～ {period.year}-{period.month:02d}-{last:02d}</p>"
        )

    trows = ""
    for r in rows:
        trows += (
            "<tr><td>"
            f'<select class=acct name=account_code>{_account_options(accounts, r.get("account_code") or "")}'
            "</select></td>"
            f'<td><input class="amt-debit" name=debit inputmode=decimal '
            f'placeholder="0.00" value="{html.escape(str(r.get("debit") or ""))}"></td>'
            f'<td><input class="amt-credit" name=credit inputmode=decimal '
            f'placeholder="0.00" value="{html.escape(str(r.get("credit") or ""))}"></td>'
            # 辅助维度（阶段1）：往来科目挂客户/供应商是本体规则要求，
            # 在表单里就地提供输入，而不是让用户每次勾「已知晓」绕过提示
            f'<td><input name=aux_dims class=aux style="width:150px" '
            f'placeholder="如 customer=甲公司" '
            f'value="{html.escape(str(r.get("aux_dims") or ""))}"></td>'
            '<td><button type=button class="del" '
            'style="padding:2px 8px;background:#fff;color:#a32d2d;border:1px solid #ddd">'
            "删除</button></td></tr>"
        )

    type_opts = "".join(
        f'<option value="{val}"{" selected" if vtype == val else ""}>{label}</option>'
        for val, label in (
            ("", "记账凭证（统一编号 记-）"),
            ("收", "收款凭证（收-）"),
            ("付", "付款凭证（付-）"),
            ("转", "转账凭证（转-）"),
        )
    )
    # 常用摘要下拉：老会计的摘要高度重复，让他重打一遍是最招骂的设计
    dl = ""
    if summaries:
        opts = "".join(
            f'<option value="{html.escape(str(s.get("summary") or ""))}">'
            for s in summaries[:50]
            if s.get("summary")
        )
        dl = f"<datalist id=sumList>{opts}</datalist>"

    cash_js = ", ".join(f'"{c}"' for c in CASH_BANK_CODES)
    # 科目 → 声明的辅助维度（aux_dim_defs），供前端就地提示「这个科目要挂什么」
    dims_js = ", ".join(
        '"{code}": [{defs}]'.format(
            code=a.code,
            defs=", ".join(f"'{d}'" for d in (a.aux_dim_defs or [])),
        )
        for a in accounts
        if a.aux_dim_defs
    )
    type_js = f"""
<script>
var CASH_PREFIX = [{cash_js}];
var ACCT_DIMS = {{{dims_js}}};
function isCash(code){{
  code = (code || '').trim();
  for (var i = 0; i < CASH_PREFIX.length; i++) {{
    if (code.indexOf(CASH_PREFIX[i]) === 0) return true;
  }}
  return false;
}}
function hintDims(row){{
  var sel = row.querySelector('select.acct');
  var aux = row.querySelector('input.aux');
  if (!sel || !aux) return;
  var defs = ACCT_DIMS[sel.value];
  if (defs && defs.length) {{
    aux.placeholder = '须挂：' + defs.join('/') + '（格式 ' + defs[0] + '=值）';
  }} else {{
    aux.placeholder = '无辅助维度';
  }}
}}
function guessType(){{
  var rows = document.querySelectorAll('#lines tr'), dr = false, cr = false;
  for (var i = 0; i < rows.length; i++) {{
    hintDims(rows[i]);
    var sel = rows[i].querySelector('select.acct');
    if (!sel || !isCash(sel.value)) continue;
    var dEl = rows[i].querySelector('.amt-debit');
    var cEl = rows[i].querySelector('.amt-credit');
    var d = parseFloat((dEl && dEl.value) || 0) || 0;
    var c = parseFloat((cEl && cEl.value) || 0) || 0;
    if (c > 0) cr = true;
    if (d > 0) dr = true;
  }}
  var t = cr ? '付' : (dr ? '收' : '转');
  var el = document.getElementById('typeHint');
  if (el) el.textContent = '（资金流向判定：' + t + '）';
}}
document.addEventListener('DOMContentLoaded', function(){{
  var tb = document.getElementById('lines');
  if (tb) {{
    tb.addEventListener('input', guessType);
    tb.addEventListener('change', guessType);
  }}
  guessType();
}});
</script>"""

    return f"""
{_FORM_JS}
<h2>填制凭证</h2>
{head}
<form method=post action="/ledger/{ls_id}/voucher/new">
<p>凭证类别：<select name=voucher_type onchange="this.form.querySelector(
   'select[name=voucher_type]').blur()" style="max-width:220px;display:inline-block"
   >{type_opts}</select>
   <span id=typeHint style="margin-left:10px;color:#1f4e79"></span></p>
<p>科目过滤：<input id=acctFilter oninput="filterAccounts(this.value)"
   placeholder="输入编码或名称，如 1002 或 银行"
   style="max-width:320px;display:inline-block"></p>
<table>
<thead><tr><th style="width:44%">科目</th><th>借方</th><th>贷方</th>
<th style="width:18%">辅助维度</th><th style="width:70px"></th></tr></thead>
<tbody id=lines>{trows}</tbody>
<tfoot><tr>
  <th style="text-align:right">合计</th>
  <th id=sumDebit style="text-align:right">0.00</th>
  <th id=sumCredit style="text-align:right">0.00</th>
  <th></th><th></th>
</tr><tr>
  <th style="text-align:right">差额</th>
  <th id=sumDiff colspan=2 style="text-align:right">0.00</th>
  <th></th><th></th>
</tr></tfoot>
</table>
<p><button type=button onclick="addLine()" style="background:#fff;color:#185fa5;
   border:1px solid #185fa5">+ 增加一行</button>
   <span id=balanceHint style="margin-left:12px;font-size:14px"></span></p>
<p>日期：<input type=date name=voucher_date value="{vdate}"
   style="max-width:200px;display:inline-block"></p>
<p>摘要：<input name=summary list=sumList autocomplete=off
   value="{html.escape(str(summary))}" placeholder="如：报销差旅费"></p>
<p>
<button type=submit name=action value=draft>保存（草稿）</button>
<button type=submit name=action value=submit style="margin-left:8px">保存并送审</button>
<a href="/ledger/{ls_id}" style="margin-left:12px">取消</a>
</p>
</form>
{dl}
{type_js}
"""


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
                    f"<tr><td><a class=vno href=/voucher/{v.id}>{v.voucher_no}</a></td>"
                    f"<td>{v.voucher_date}</td><td>{st_badge(v.status)}</td>"
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
                f"{p.year}-{p.month:02d}({period_zh(p.status)})</a>&nbsp;"
                for p in periods
            ) or "（无期间）"
            # 本月引导卡（阶段0）：进账套先回答「这个月还差什么、下一步干嘛」，
            # 再看凭证流水。选中期间与内核 month_end_guide 同一真源。
            from kernel.period_guide import month_end_guide

            guide = (
                month_end_guide(
                    s, ledger_set_id=ls_id, year=period.year, month=period.month
                )
                if period is not None
                else None
            )
            err = f'<p class="err">{html.escape(error)}</p>' if error else ""
            plabel = (
                f"{period.year}-{period.month:02d}" if period is not None else "无期间"
            )
            body = (
                f"<h2>账套：{html.escape(ls.name)}</h2>"
                + _toolbar(
                    f"<a href='/ledger/{ls_id}/voucher/new'>填制凭证</a>",
                    f"<a href='/ledger/{ls_id}/reports'>账簿报表</a>",
                    f"<a href='/ledger/{ls_id}/forecast'>三表预测</a>",
                    f"<a href='/ledger/{ls_id}/close'>月末结账</a>",
                )
                + f"<p>期间切换：{ptabs}</p>{err}"
                + ("<h3>本月引导</h3>" + _guide_card(guide, ls_id))
                + '<h3>凭证（最近 50 张）　'
                f"<a href='/ledger/{ls_id}/voucher/new'>+ 新建凭证</a></h3>"
                "<table><tr><th>凭证号</th><th>日期</th><th>状态</th><th>摘要</th></tr>"
                + (vrows or "<tr><td colspan=4>暂无凭证</td></tr>")
                + "</table>"
                f"<h3>科目余额表 {plabel}</h3>"
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

    # ---------- 制单（G1 制单闭环） ----------

    def _render_voucher_form(request, ls_id: str, error: str = "",
                             values: dict | None = None,
                             findings: list | None = None) -> HTMLResponse:
        """制单页渲染。建单走内核原语 create_draft_voucher——与 MCP 同一实现，
        避免「对话里进得来、界面上进不去」的两套校验。"""
        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>",
                             request.state.subject_name, show_operator=True)
            period = s.scalars(
                select(Period)
                .where(Period.ledger_set_id == ls_id, Period.status == "OPEN")
                .order_by(Period.year.desc(), Period.month.desc())
            ).first()
            all_acc = list(
                s.scalars(
                    select(Account).where(
                        Account.ledger_set_id == ls_id
                    ).order_by(Account.code)
                ).all()
            )
            # 只给末级科目：非末级科目过账会被内核拒绝（父科目余额由子科目汇总），
            # 放进下拉等于埋一个「选了必然报错」的坑。
            leaf = [a for a in all_acc if a.is_leaf] or all_acc
            # 常用摘要：从历史凭证统计，不新增表也不缓存（历史凭证就是摘要库）
            from kernel.classic import suggest_summaries

            summaries = suggest_summaries(s, ledger_set_id=ls_id, limit=20)
            err = f'<p class="err">{html.escape(error)}</p>' if error else ""
            # 本体提示区（阶段1）：确定性规则命中时展示，勾选确认才放行（HITL）
            onto_html = ""
            if findings:
                items = "".join(
                    f'<li class="{"fail" if f["severity"] == "error" else "warn"}">'
                    f'{html.escape(f["message_zh"])}（{f["rule_id"]}）</li>'
                    for f in findings
                )
                onto_html = (
                    '<div class="warn"><p><b>本体提示——以下分录可能有讲究：</b></p>'
                    f"<ul>{items}</ul>"
                    '<p><label><input type=checkbox name=ignore_ontology value=1>'
                    "已知晓上述提示，继续创建凭证</label></p></div>"
                )
            body = (
                f"<p><a href='/ledger/{ls_id}'>← 返回账套</a></p>{err}{onto_html}"
                + _voucher_form(ls_id, leaf, period, values, summaries)
            )
            return _page(f"{ls.name} · 新建凭证", body,
                         request.state.subject_name, show_operator=True)

    @app.get("/ledger/{ls_id}/voucher/new", response_class=HTMLResponse)
    def voucher_new(request: Request, ls_id: str, error: str = ""):
        return _render_voucher_form(request, ls_id, error)

    @app.post("/ledger/{ls_id}/voucher/new")
    def voucher_create(
        request: Request,
        ls_id: str,
        voucher_date: str = Form(""),
        summary: str = Form(""),
        account_code: list[str] = Form([]),
        debit: list[str] = Form([]),
        credit: list[str] = Form([]),
        aux_dims: list[str] = Form([]),
        action: str = Form("draft"),
        voucher_type: str = Form(""),
        ignore_ontology: str = Form(""),
    ):
        from itertools import zip_longest

        from kernel.classic import voucher_prefix
        from kernel.posting import PostingError
        from kernel.state import transition
        from kernel.voucher_wizard import create_draft_voucher, record_voucher_created

        actor = {"type": "user", "id": request.state.subject_id}
        rows = []
        dims_error = ""
        for i, (code, dr, cr) in enumerate(
            zip_longest(account_code, debit, credit, fillvalue="")
        ):
            code = (code or "").strip()
            if not code:
                continue  # 未选科目的空行直接丢弃，不算分录
            raw_dims = (aux_dims[i] if i < len(aux_dims) else "").strip()
            dims = _parse_aux_dims(raw_dims)
            if dims is None:
                dims_error = (
                    f"第 {len(rows) + 1} 行辅助维度格式不对：请写「customer=甲公司」"
                    "这样的「维度=值」，多个用分号隔开"
                )
            rows.append(
                {
                    "account_code": code,
                    "debit": dr or "",
                    "credit": cr or "",
                    "aux_dims": raw_dims,  # 原样回填用
                    "dims": dims or {},    # 解析结果给内核
                }
            )
        # 凭证类别 → 编号前缀。未选类别时沿用统一编号「记-」，与 MCP 同一
        # 语义、与历史账套一致——怀旧是可选开关，不是默认行为变更。
        chosen = (voucher_type or "").strip()
        use_prefix = chosen in ("收", "付", "转")
        prefix = voucher_prefix(chosen) if use_prefix else "记-"

        def _form_values(rs):
            return {
                "voucher_date": voucher_date,
                "summary": summary,
                "voucher_type": chosen,
                "rows": rs or [{"account_code": "", "debit": "",
                                "credit": "", "aux_dims": ""}] * 4,
            }

        values = _form_values(rows)
        if dims_error:
            return _render_voucher_form(request, ls_id, error=dims_error,
                                        values=values)
        # 本体预检（阶段1）：命中规则先展示提示、确认后才创建（HITL）。
        # 与内核铁律一致——本体只建议不拦截，拦截权在用户的这一次勾选。
        if rows and not ignore_ontology:
            from kernel.ontology import OntologyError, check_lines

            with session() as s:
                ls_row = s.get(LedgerSet, ls_id)
                std = ls_row.accounting_standard if ls_row else "small_business"
                try:
                    findings = check_lines(
                        std,
                        [
                            {"code": r["account_code"],
                             "aux_dims": r.get("dims") or None,
                             "debit": r["debit"]}
                            for r in rows
                        ],
                    )
                except OntologyError:
                    findings = []
            if findings:
                return _render_voucher_form(request, ls_id, values=values,
                                            findings=findings)
        try:
            with session() as s:
                v, replayed = create_draft_voucher(
                    s,
                    ledger_set_id=ls_id,
                    actor=actor,
                    voucher_date=voucher_date,
                    summary=summary,
                    # 内核行协议：aux_dims 必须是 dict；rows 里的
                    # "aux_dims" 原始字符串仅供表单回填，这里换 "dims"
                    lines=[
                        {
                            "account_code": r["account_code"],
                            "debit": r["debit"],
                            "credit": r["credit"],
                            "aux_dims": r["dims"] or None,
                        }
                        for r in rows
                    ],
                    prefix=prefix,
                    per_prefix=use_prefix,
                )
                if not replayed:
                    record_voucher_created(s, v, actor)
                if action == "submit":
                    transition(s, voucher_id=v.id, actor=actor, target="PUSHED")
                s.commit()
                vid = v.id
        except PostingError as e:
            # 原地重渲染并回填：重定向回空表会让会计把整张凭证重打一遍
            return _render_voucher_form(request, ls_id, error=e.message_zh,
                                        values=values)
        return RedirectResponse(f"/voucher/{vid}", status_code=303)

    # ---------- 凭证详情 ----------

    @app.get("/ledger/{ls_id}/reports", response_class=HTMLResponse)
    def reports(request: Request, ls_id: str, year: int = 0, month: int = 0,
             error: str = "", apply_reclass: str = ""):
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

                # 往来重分类列报（阶段1）：默认关闭，勾选后按往来单位余额
                # 方向把预收/预付性质余额搬到对方科目（本体 reclass_pairs）
                use_reclass = apply_reclass in ("1", "on", "true")
                bs = balance_sheet(s, ls_id, yr, mo, ls.accounting_standard,
                                   apply_reclass=use_reclass)
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
            # 重分类开关与明细：勾选即重算，明细逐项列出（本体第一次进报表）
            reclass_ui = (
                f'<form method=get action="/ledger/{ls_id}/reports" class=inline>'
                f'<input type=hidden name=year value={yr}>'
                f'<input type=hidden name=month value={mo}>'
                f'<label><input type=checkbox name=apply_reclass value=1'
                f'{" checked" if use_reclass else ""}'
                f' onchange="this.form.submit()"> 往来重分类列报</label>'
                f"</form>"
            )
            reclass_block = ""
            rc = bs.get("reclass")
            if use_reclass and rc and rc["items"]:
                lis = "".join(
                    f"<li>{html.escape(i['account_code'])} · "
                    f"{html.escape(i['partner'])} {i['balance']:,.2f} → "
                    f"{html.escape(i['to_account_code'])}</li>"
                    for i in rc["items"]
                )
                untracked_note = (
                    f"（另有 {rc['untracked']:,.2f} 未挂往来维度，"
                    f"保守留在原项目）" if rc["untracked"] else ""
                )
                reclass_block = (
                    "<p class=ok>资产→负债 "
                    f"{rc['to_liability']:,.2f}、负债→资产 "
                    f"{rc['to_asset']:,.2f}{untracked_note}</p><ul>{lis}</ul>"
                )
            body = (
                f"<h2>{html.escape(ls.name)} · {yr}-{mo:02d} 三大报表</h2>"
                f"<p><a href=/ledger/{ls_id}>← 返回账套</a> · "
                f"<a href='/ledger/{ls_id}/forecast?year={yr}&month={mo}'>"
                f"三表预测</a></p>{err}<p>{close_ui}</p>"
                f"<h3>利润表</h3>{table(inc_rows, '项目', '金额')}"
                f"<h3>资产负债表 <span class=badge>{badge}</span> {reclass_ui}</h3>"
                f"{reclass_block}{table(bs_rows, '项目', '金额')}"
                f"<h3>现金流量表（直接法）</h3>{table(cf_rows, '项目', '金额')}"
                f"<h3>账账核对</h3><p>{rec_badge}</p>"
                f"<p>勾稽：期初现金 {cf['reconcile']['opening_cash']:,.2f} + 净增加 "
                f"{cf['reconcile']['net_increase']:,.2f} = 期末现金 "
                f"{cf['reconcile']['closing_cash']:,.2f}</p>"
            )
            return _page(f"{ls.name} 报表", body, request.state.subject_name)

    # ---------- 三表预测（P1-01 Web 入口） ----------

    _FORECAST_SCN_ZH = {"base": "基准", "best": "乐观", "worst": "悲观"}

    @app.get("/ledger/{ls_id}/forecast", response_class=HTMLResponse)
    def forecast_page(request: Request, ls_id: str, year: int = 0,
                      month: int = 0, horizon: int = 6, scenario: str = "base"):
        """三表前向预测页——从实际三表外推未来 N 期。

        服务端直调内核 forecast 引擎（不经 MCP）。基准期默认取最近 OPEN 期；
        情景 base/best/worst 渲染三表明细（期间为列），all 渲染三情景对比表。
        """
        from kernel.forecast import forecast_from_actuals

        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>",
                             request.state.subject_name)
            periods = list(
                s.scalars(
                    select(Period)
                    .where(Period.ledger_set_id == ls_id)
                    .order_by(Period.year.desc(), Period.month.desc())
                ).all()
            )
            if year and month:
                base = next(
                    (p for p in periods if p.year == year and p.month == month), None
                )
            else:
                base = next((p for p in periods if p.status == "OPEN"), None)
            base = base or (periods[0] if periods else None)
            if base is None:
                return _page("三表预测", "<p class=err>本账套尚无会计期间</p>",
                             request.state.subject_name)

            if scenario not in ("best", "base", "worst", "all"):
                scenario = "base"
            horizon = max(1, min(horizon, 36))

            def _nav() -> str:
                return (
                    f"<p><a href='/ledger/{ls_id}'>← 返回账套</a> · "
                    f"<a href='/ledger/{ls_id}/reports'>账簿报表</a></p>"
                )

            try:
                result = forecast_from_actuals(
                    s, ls_id, base.year, base.month,
                    horizon=horizon, scenario=scenario,
                    standard=ls.accounting_standard,
                )
            except Exception as e:  # noqa: BLE001
                return _page(
                    f"{ls.name} 三表预测",
                    f"{_nav()}<p class=err>预测生成失败: {html.escape(str(e))}</p>",
                    request.state.subject_name,
                )

            ptabs = "".join(
                f'<a href="/ledger/{ls_id}/forecast?year={p.year}&month={p.month}'
                f'&horizon={horizon}&scenario={scenario}">'
                f"{p.year}-{p.month:02d}({period_zh(p.status)})</a>&nbsp;"
                for p in periods
            )
            stabs = "&nbsp;".join(
                f'<a href="/ledger/{ls_id}/forecast?year={base.year}&month={base.month}'
                f'&horizon={horizon}&scenario={sc}">{zh}</a>'
                for sc, zh in (("base", "基准"), ("best", "乐观"),
                               ("worst", "悲观"), ("all", "三情景对比"))
                if sc != scenario
            )
            form = (
                f'<form method=get action="/ledger/{ls_id}/forecast" class=ops>'
                f'<input type=hidden name=year value="{base.year}">'
                f'<input type=hidden name=month value="{base.month}">'
                f'<input type=hidden name=scenario value="{scenario}">'
                f'预测期数 <input type=number name=horizon min=1 max=36 '
                f'value="{horizon}" style="width:5em"> 个月 '
                "<button type=submit>重新预测</button></form>"
            )

            def _m(v) -> str:
                return f"{v:,.2f}"

            def _grid(labels: list[str], rows: list[tuple[str, list]]) -> str:
                """期间为列的矩阵表：labels 是各期标签，rows 是 (行名, 各期值)。"""
                head = "".join(f"<th style=text-align:right>{html.escape(l)}</th>"
                               for l in labels)
                body_rows = "".join(
                    f"<tr><td>{html.escape(name)}</td>"
                    + "".join(
                        f"<td style=text-align:right>"
                        f"{'　' if v is None else _m(v)}</td>"
                        for v in vals
                    )
                    + "</tr>"
                    for name, vals in rows
                )
                return (f"<table><tr><th>项目</th>{head}</tr>{body_rows}</table>")

            if scenario == "all":
                # 对比表：行=情景×指标，列=期间（期间标签取基准情景）
                per = result["scenarios"]["base"]["periods"]
                labels = [f"{p['year']}-{p['month']:02d}" for p in per]
                cmp_rows = []
                for sc in ("best", "base", "worst"):
                    per = result["scenarios"][sc]["periods"]
                    revs = [p["income_statement"]["revenue"] for p in per]
                    nis = [p["income_statement"]["net_profit"] for p in per]
                    cashes = [p["cash_flow"]["closing_cash"] for p in per]
                    cmp_rows.append((f"{_FORECAST_SCN_ZH[sc]} · 营业收入", revs))
                    cmp_rows.append((f"{_FORECAST_SCN_ZH[sc]} · 净利润", nis))
                    cmp_rows.append((f"{_FORECAST_SCN_ZH[sc]} · 期末现金", cashes))
                all_ok = all(
                    p["balance_sheet"]["balanced"]
                    for sc in ("best", "base", "worst")
                    for p in result["scenarios"][sc]["periods"]
                )
                badge = "✅ 三情景全期平衡" if all_ok else "❌ 存在不平衡期间"
                body = (
                    f"{_nav()}<h2>{html.escape(ls.name)} · 三表预测"
                    f"（三情景对比，自 {base.year}-{base.month:02d} 起 "
                    f"{horizon} 期）</h2>"
                    f"<p>基准期切换：{ptabs}</p>"
                    f"<p>情景切换：{stabs}</p>{form}"
                    f"<h3>对比 <span class=badge>{badge}</span></h3>"
                    f"{_grid(labels, cmp_rows)}"
                    "<p class=hint>假设由实际三表自动推导；调整假设请走 MCP "
                    "forecast_statements 工具（assumptions_json）。</p>"
                )
            else:
                zh = _FORECAST_SCN_ZH[scenario]
                per = result["periods"]
                labels = [f"{p['year']}-{p['month']:02d}" for p in per]
                inc_labels = [i["item"] for i in per[0]["income_statement"]["items"]]
                inc_rows = [
                    (lb, [p["income_statement"]["items"][i]["amount"]
                          for p in per])
                    for i, lb in enumerate(inc_labels)
                ]
                b0 = per[0]["balance_sheet"]
                paid_in = b0["total_equity"] - b0["retained_earnings"]
                bs_keys = (
                    ("货币资金", "cash"), ("应收账款", "ar"), ("存货", "inventory"),
                    ("固定资产净额", "fa_net"), ("资产合计", "total_assets"),
                    ("应付账款", "ap"), ("负债合计", "total_liabilities"),
                    ("实收资本", None), ("留存收益", "retained_earnings"),
                    ("权益合计", "total_equity"),
                )
                bs_rows = []
                for zh2, k in bs_keys:
                    if k is None:
                        bs_rows.append((zh2, [paid_in] * len(per)))
                    else:
                        bs_rows.append((zh2, [p["balance_sheet"][k] for p in per]))
                cf_rows = [
                    ("经营活动净额", [p["cash_flow"]["operating"] for p in per]),
                    ("投资活动净额", [p["cash_flow"]["investing"] for p in per]),
                    ("筹资活动净额", [p["cash_flow"]["financing"] for p in per]),
                    ("现金净增加额", [p["cash_flow"]["net_increase"] for p in per]),
                    ("期末现金", [p["cash_flow"]["closing_cash"] for p in per]),
                ]
                all_ok = all(p["balance_sheet"]["balanced"] for p in per)
                badge = "✅ 全期平衡" if all_ok else "❌ 存在不平衡期间"
                a = result["assumptions"]
                asm_line = (
                    f"假设：收入增速 {a['rev_growth']} · 毛利率 {a['gross_margin']} · "
                    f"费用率 {a['opex_ratio']} · 税率 {a['tax_rate']} · "
                    f"应收 {a['ar_days']} 天 · 应付 {a['ap_days']} 天 · "
                    f"存货 {a['inv_days']} 天 · 资本开支率 {a['capex_pct']} · "
                    f"年折旧率 {a['dep_rate']}"
                )
                body = (
                    f"{_nav()}<h2>{html.escape(ls.name)} · {zh}情景三表预测"
                    f"（自 {base.year}-{base.month:02d} 起 {horizon} 期）</h2>"
                    f"<p>基准期切换：{ptabs}</p>"
                    f"<p>情景切换：{stabs}</p>{form}"
                    f"<p class=hint>{asm_line}</p>"
                    f"<h3>利润表</h3>{_grid(labels, inc_rows)}"
                    f"<h3>资产负债表 <span class=badge>{badge}</span></h3>"
                    f"{_grid(labels, bs_rows)}"
                    f"<h3>现金流量表</h3>{_grid(labels, cf_rows)}"
                    "<p class=hint>勾稽：净利润→留存收益→权益；折旧加回经营现金流；"
                    "营运资本变动连接权责与收付。假设调整请走 MCP "
                    "forecast_statements 工具（assumptions_json）。</p>"
                )
            return _page(f"{ls.name} 三表预测", body, request.state.subject_name)

    @app.get("/ledger/{ls_id}/close", response_class=HTMLResponse)
    def close_page(request: Request, ls_id: str, year: int = 0, month: int = 0):
        """月末结账体检页——怀旧设计里最有仪式感的一环。

        老软件的价值不在于点「结账」这个动作，而在于**点之前先告诉你还差什么**。
        四道闸门一次查完列成清单，而不是逐个抛异常让人来回试错。
        """
        from kernel.classic import precheck_close

        with session() as s:
            ls = s.get(LedgerSet, ls_id)
            if ls is None:
                return _page("错误", "<p class=err>账套不存在</p>",
                             request.state.subject_name)
            periods = list(
                s.scalars(
                    select(Period)
                    .where(Period.ledger_set_id == ls_id)
                    .order_by(Period.year.desc(), Period.month.desc())
                ).all()
            )
            if year and month:
                period = next(
                    (p for p in periods if p.year == year and p.month == month), None
                )
            else:
                period = next((p for p in periods if p.status == "OPEN"), None)
            period = period or (periods[0] if periods else None)
            if period is None:
                return _page("月末结账", "<p class=err>本账套尚无会计期间</p>",
                             request.state.subject_name)

            result = precheck_close(
                s, ledger_set_id=ls_id, year=period.year, month=period.month
            )
            lis = ""
            for c in result["checks"]:
                cls = "pass" if c["passed"] else "fail"
                mark = "√" if c["passed"] else "×"
                hint = c.get("hint") or ""
                hint_html = (
                    f'<div class=hint>→ {html.escape(hint)}</div>' if hint else ""
                )
                lis += (
                    f'<li class={cls}><span class=item>{mark} '
                    f'{html.escape(c["item"])}</span>　'
                    f'{html.escape(c["detail"])}{hint_html}</li>'
                )
            ptabs = "".join(
                f'<a href="/ledger/{ls_id}/close?year={p.year}&month={p.month}">'
                f"{p.year}-{p.month:02d}({period_zh(p.status)})</a>&nbsp;"
                for p in periods
            )
            if period.status == "CLOSED":
                banner = (f'<div class=ok>本期（{period.year}-{period.month:02d}）'
                          "已结账。</div>")
                action = ""
            elif result["can_close"]:
                banner = '<div class=ok>结账条件已全部满足，可以结账。</div>'
                action = (
                    f'<div class=ops><form method=post action="/ledger/{ls_id}/close">'
                    f'<input type=hidden name=year value="{period.year}">'
                    f'<input type=hidden name=month value="{period.month}">'
                    "<button>执行月末结账</button></form></div>"
                )
            else:
                banner = f'<div class=warn>{html.escape(result["summary"])}</div>'
                action = ""
            body = (
                f"<p><a href='/ledger/{ls_id}'>← 返回账套</a></p>"
                f"<h2>月末结账　{period.year}-{period.month:02d}"
                f"（{period_zh(period.status)}）</h2>"
                f"<p>期间切换：{ptabs}</p>"
                f"{banner}<ul class=check>{lis}</ul>{action}"
            )
            return _page("月末结账", body, request.state.subject_name)

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
    def voucher_detail(request: Request, vid: str, error: str = ""):
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
            # 操作区：按「当前身份 × 凭证状态」决定能做什么。与 MCP guarded
            # 同一内核语义（NO_SELF_APPROVAL / AGENT_APPROVAL_FORBIDDEN），
            # 这里只是把门禁翻译成按钮显隐，最终裁决仍在 transition。
            actor_id = request.state.subject_id
            is_maker = str(v.created_by) == str(actor_id)
            me = s.get(Subject, actor_id)
            i_am_agent = (me.type if me else "user") == "agent"
            ops = ""
            if v.status == "DRAFT" and is_maker and not i_am_agent:
                ops = (
                    f'<div class=ops><form method=post action="/voucher/{vid}/push">'
                    "<button>推送审批（提交给审批人）</button></form></div>"
                )
            elif v.status == "DRAFT" and i_am_agent:
                ops = (
                    '<div class=ops><p class=warn>草稿待制单人推送审批；'
                    "当前身份是 Agent，推送须由制单人人执行。</p></div>"
                )
            elif v.status == "PUSHED":
                if is_maker:
                    ops = (
                        f'<div class=ops><form method=post action="/voucher/{vid}/withdraw">'
                        "<button>撤回（收回修改）</button></form></div>"
                    )
                elif i_am_agent:
                    ops = (
                        '<div class=ops><p class=warn>当前身份是 Agent，不能审批或驳回；'
                        "请用人员身份登录处理。</p></div>"
                    )
                else:
                    ops = (
                        f'<div class=ops><form method=post action="/voucher/{vid}/approve">'
                        "<button>同意（批准）</button></form>"
                        f'<form method=post action="/voucher/{vid}/reject">'
                        "<label>驳回原因（必填，将退回制单人修改）</label>"
                        '<textarea name=reason rows=2 '
                        'placeholder="例如：金额与发票不符，请核对后重新提交"></textarea>'
                        '<button class=danger>驳回</button></form></div>'
                    )
            elif v.status == "APPROVED" and not i_am_agent:
                ops = (
                    f'<div class=ops><form method=post action="/voucher/{vid}/post">'
                    "<button>过账（记入总账）</button></form></div>"
                )
            elif v.status == "APPROVED":
                ops = '<div class=ops><p class=warn>Agent 不能执行过账，请由人员操作。</p></div>'
            err = f'<p class="err">{html.escape(error)}</p>' if error else ""
            body = (
                f"<h2>凭证 <span class=vno>{v.voucher_no}</span> {st_badge(v.status)}</h2>"
                f"<p>日期 {v.voucher_date}　摘要 {html.escape(v.summary or '')}</p>"
                + err
                + "<table><tr><th>#</th><th>编码</th><th>科目</th><th>借方</th><th>贷方</th></tr>"
                + lrows
                + "</table>"
                + ops
                + "<p><a href=/ledger/"
                + v.ledger_set_id
                + ">← 返回账套</a>　<a href=/todo>→ 审批待办</a></p>"
            )
            return _page(v.voucher_no, body, request.state.subject_name)

    # ---------- 审批闭环（P1-G1：待办列表 + 同意/驳回/撤回/过账） ----------

    def _apply_transition(request: Request, vid: str, target: str,
                          reason: str = "",
                          require_maker: bool | None = None) -> tuple[str | None, Voucher | None]:
        """Web 端状态跃迁统一入口。与 MCP guarded 同一内核语义：
        require_maker 预校验 → casbin enforce 鉴权 → transition / post_voucher。
        返回 (error_message | None, voucher | None)。"""
        from kernel.authz import AuthzError, enforce
        from kernel.posting import PostingError, post_voucher
        from kernel.state import ALLOWED, transition

        actor_id = request.state.subject_id
        try:
            with session() as s:
                v = s.get(Voucher, vid)
                if v is None:
                    return "凭证不存在", None
                # 状态机合法性预检：非法组合（如已推送再推送）直接给中文
                # 提示，避免落进错误的权限动作被 casbin 抢先拦截。
                # POSTED 目标例外：APPROVED→POSTED 走 post_voucher 专属
                # 路径（不在 transition 的 ALLOWED 字典里），由它自校验。
                if target != "POSTED" and (v.status, target) not in ALLOWED:
                    return f"不允许从 {v.status} 跃迁到 {target}", None
                me = s.get(Subject, actor_id)
                actor = {"type": (me.type if me else "user"), "id": actor_id}
                is_maker = str(v.created_by) == str(actor_id)
                # 与 MCP guarded 一致：先查身份关系再鉴权，错误信息更有操作性
                if require_maker is True and not is_maker:
                    return "只有制单人本人可以撤回该凭证", None
                if require_maker is False and is_maker:
                    return "制单人不能审批自己的凭证；如需收回请改用撤回", None
                # 权限动作映射（与 MCP _action_for 一致）：PUSHED→DRAFT
                # 按执行人区分 —— 撤回是制单人动作，驳回是审批人动作。
                if (v.status, target) == ("DRAFT", "PUSHED"):
                    action = "voucher:push"
                elif (v.status, target) == ("PUSHED", "APPROVED"):
                    action = "voucher:approve"
                elif (v.status, target) == ("PUSHED", "DRAFT"):
                    action = "voucher:push" if is_maker else "voucher:approve"
                elif target == "POSTED":
                    action = "voucher:post"
                else:
                    action = "voucher:cancel"
                enforce(s, actor_id=actor_id,
                        ledger_set_id=v.ledger_set_id, action=action)
                if target == "POSTED":
                    # 过账必须走 post_voucher：它还要累计 balances 投影，
                    # 走裸 transition 会跳过余额更新（账账核对必然炸）。
                    post_voucher(s, voucher_id=vid, actor=actor)
                    v2 = s.get(Voucher, vid)
                else:
                    v2 = transition(s, voucher_id=vid, actor=actor,
                                    target=target, reason=reason)
                s.commit()
                return None, v2
        except (PostingError, AuthzError) as e:
            msg = str(e) if isinstance(e, AuthzError) else e.message_zh
            return msg, None

    @app.get("/todo", response_class=HTMLResponse)
    def todo_list(request: Request, error: str = ""):
        """审批待办：审批人看到待我审批的队列，制单人看到自己推送的待审单。"""
        actor_id = request.state.subject_id
        with session() as s:
            me = s.get(Subject, actor_id)
            i_am_agent = (me.type if me else "user") == "agent"
            pending = s.scalars(
                select(Voucher).where(Voucher.status == "PUSHED").order_by(
                    Voucher.voucher_date, Voucher.voucher_no
                )
            ).all()
            makers = {
                sub.id: sub
                for sub in s.scalars(
                    select(Subject).where(
                        Subject.id.in_({v.created_by for v in pending} or {""})
                    )
                ).all()
            }
            to_approve = ""
            mine = ""
            for v in pending:
                maker = makers.get(v.created_by)
                maker_name = maker.display_name if maker else (v.created_by or "?")
                ls = s.get(LedgerSet, v.ledger_set_id)
                ls_name = ls.name if ls else "?"
                row = (
                    f"<tr><td>{html.escape(ls_name)}</td>"
                    f"<td><a href=/voucher/{v.id}>{v.voucher_no}</a></td>"
                    f"<td>{v.voucher_date}</td>"
                    f"<td>{html.escape(maker_name)}</td>"
                    f"<td>{html.escape(v.summary or '')}</td>"
                )
                if str(v.created_by) == str(actor_id):
                    mine += (
                        row
                        + f"<td><form method=post action=/voucher/{v.id}/withdraw "
                        + 'style=margin:0><button>撤回</button></form></td></tr>'
                    )
                else:
                    to_approve += (
                        row
                        + f"<td><a href=/voucher/{v.id}>去处理 →</a></td></tr>"
                    )
            if i_am_agent:
                tip = (
                    '<p class=warn>当前身份是 Agent：审批与驳回必须由人执行，'
                    "以下队列仅供查看。</p>"
                )
            else:
                tip = ""
            err = f'<p class="err">{html.escape(error)}</p>' if error else ""
            body = (
                "<h2>审批待办</h2>" + err + tip
                + "<h3>待我审批（非本人制单）</h3>"
                + '<table><tr><th>账套</th><th>凭证号</th><th>日期</th>'
                + "<th>制单人</th><th>摘要</th><th>操作</th></tr>"
                + (to_approve or "<tr><td colspan=6>队列已清空 🎉</td></tr>")
                + "</table>"
                + "<h3>我推送的（可撤回）</h3>"
                + '<table><tr><th>账套</th><th>凭证号</th><th>日期</th>'
                + "<th>制单人</th><th>摘要</th><th>操作</th></tr>"
                + (mine or "<tr><td colspan=6>暂无待审单</td></tr>")
                + "</table>"
            )
            return _page("审批待办", body, request.state.subject_name)

    @app.post("/voucher/{vid}/push")
    def voucher_push_web(request: Request, vid: str):
        err, _v = _apply_transition(request, vid, "PUSHED", require_maker=True)
        if err:
            return RedirectResponse(f"/voucher/{vid}?error={quote(err)}", 303)
        return RedirectResponse(f"/voucher/{vid}", 303)

    @app.post("/voucher/{vid}/approve")
    def voucher_approve_web(request: Request, vid: str):
        err, _v = _apply_transition(request, vid, "APPROVED", require_maker=False)
        if err:
            return RedirectResponse(f"/voucher/{vid}?error={quote(err)}", 303)
        return RedirectResponse(f"/voucher/{vid}", 303)

    @app.post("/voucher/{vid}/reject")
    def voucher_reject_web(request: Request, vid: str, reason: str = Form("")):
        if not (reason or "").strip():
            err = "驳回必须填写原因，否则制单人不知道要改什么"
            return RedirectResponse(f"/voucher/{vid}?error={quote(err)}", 303)
        err, _v = _apply_transition(request, vid, "DRAFT", reason=reason.strip(),
                                    require_maker=False)
        if err:
            return RedirectResponse(f"/voucher/{vid}?error={quote(err)}", 303)
        return RedirectResponse(f"/voucher/{vid}", 303)

    @app.post("/voucher/{vid}/withdraw")
    def voucher_withdraw_web(request: Request, vid: str):
        err, _v = _apply_transition(request, vid, "DRAFT", require_maker=True)
        if err:
            return RedirectResponse(f"/voucher/{vid}?error={quote(err)}", 303)
        return RedirectResponse(f"/voucher/{vid}", 303)

    @app.post("/voucher/{vid}/post")
    def voucher_post_web(request: Request, vid: str):
        err, _v = _apply_transition(request, vid, "POSTED")
        if err:
            return RedirectResponse(f"/voucher/{vid}?error={quote(err)}", 303)
        return RedirectResponse(f"/voucher/{vid}", 303)

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

"""XErp MCP Server — 七工具（ADR-003 契约）。

启动: python mcp-server/server.py        （stdio transport）
库引用: build_server(db_url) -> FastMCP  （测试 / 嵌入 WorkBuddy 用）

要点：
- 每次工具调用独立 Session（成功 commit / 异常 rollback）
- 金额入参出参一律 decimal-string；错误统一 {ok:false, error:{code,message_zh,details}}
- 写操作强制 actor 身份（ADR-005 审计前置）；禁止自审（ADR-004）
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation

from fastmcp import FastMCP
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# 本文件位于 <repo>/mcp-server/xerp_mcp/server.py：向上三层才是仓库根
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MCP_DIR = os.path.dirname(_THIS_DIR)
_REPO_ROOT = os.path.dirname(_MCP_DIR)
for _p in (_REPO_ROOT, _MCP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kernel.adapters.spec import (  # noqa: E402
    EventFieldError,
    RuleError,
)
from kernel.db.models import (  # noqa: E402
    Account,
    Balance,
    Period,
    Subject,
    Voucher,
    VoucherLine,
)
from kernel.posting import (  # noqa: E402
    PostingError,
    PostingLine,
)
from kernel.posting import (  # noqa: E402
    post_voucher as _post_voucher,
)
from kernel.posting import (  # noqa: E402
    validate_voucher as _validate_voucher,
)
from kernel.reconcile import ReconcileError  # noqa: E402
from kernel.reporting.statements import ReportError  # noqa: E402
from kernel.state import transition  # noqa: E402


def _ok(**data):
    return {"ok": True, **data}


def _err(code: str, message_zh: str, details: dict | None = None):
    return {
        "ok": False,
        "error": {"code": code, "message_zh": message_zh, "details": details or {}},
    }


def _amount(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation:
        raise PostingError(
            "AMOUNT_INVALID", f"{field} 不是合法金额: {value!r}"
        ) from None
    if -d != abs(d) and d < 0:  # 显式负数由内核规则统一拒绝，这里仅容错
        pass
    return d.quantize(Decimal("0.01"))


def _fmt(d: Decimal | None) -> str:
    return f"{(d or Decimal('0')):.2f}"


class _Repo:
    """按 URL 的 Session 工厂 + 惰性建表（首次连接自动 create_all）。"""

    def __init__(self, url: str):
        from kernel.db.base import Base

        self.engine = create_engine(url)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self):
        s = Session(self.engine)
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def build_server(db_url: str | None = None) -> FastMCP:
    url = db_url or os.environ.get(
        "XERP_DB", f"sqlite:///{os.path.join(_REPO_ROOT, 'ledgeros_dev.db')}"
    )
    repo = _Repo(url)

    _ACTION_BY_TARGET = {
        "PUSHED": "voucher:push",
        "APPROVED": "voucher:approve",
        "POSTED": "voucher:post",
        "DRAFT": "voucher:cancel",
    }

    def guarded(voucher_id: str, actor_id: str, target: str) -> dict:
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce

                v0 = s.get(Voucher, voucher_id)
                if v0 is not None:
                    enforce(s, actor_id=actor_id, ledger_set_id=v0.ledger_set_id,
                            action=_ACTION_BY_TARGET[target])
                v = transition(
                    s,
                    voucher_id=voucher_id,
                    actor={"type": "user", "id": actor_id},
                    target=target,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except (PostingError, AuthzError) as e:
            code = "FORBIDDEN" if isinstance(e, AuthzError) else e.code
            msg = str(e) if isinstance(e, AuthzError) else e.message_zh
            return _err(code, msg, getattr(e, "details", None))

    mcp = FastMCP(
        "XErp",
        instructions=(
            "XErp 智能体 ERP 内核。记账顺序：create_voucher → push_voucher → "
            "approve_voucher（须非制单人审批）→ post_voucher。金额一律字符串十进制。"
        ),
    )

    # ---------- 只读 ----------

    @mcp.tool()
    def list_accounts(ledger_set_id: str, keyword: str = "") -> dict:
        """列出账套科目（编码/名称/方向/类别/是否叶子/辅助维度定义）。keyword 过滤编码或名称。"""
        with repo.session() as s:
            q = select(Account).where(Account.ledger_set_id == ledger_set_id)
            rows = s.scalars(q.order_by(Account.code)).all()
            if keyword:
                rows = [a for a in rows if keyword in a.code or keyword in a.name]
            return _ok(
                accounts=[
                    {
                        "code": a.code,
                        "name": a.name,
                        "direction": a.direction,
                        "category": a.category,
                        "is_leaf": a.is_leaf,
                        "aux_dim_defs": a.aux_dim_defs or [],
                    }
                    for a in rows
                ]
            )

    @mcp.tool()
    def get_workspace() -> dict:
        """发现工作区：账套列表（含 id）、操作者身份（制单人/审批人及其主体 id）、各账套 OPEN 期间。

        会话开始时先调用本工具，取得 ledger_set_id 与 actor_id 后再进行记账。
        """
        from kernel.db.models import LedgerSet, Subject

        with repo.session() as s:
            ledgers = [
                {
                    "ledger_set_id": ls.id,
                    "name": ls.name,
                    "accounting_standard": ls.accounting_standard,
                    "status": ls.status,
                    "open_periods": [
                        {"year": p.year, "month": p.month}
                        for p in s.scalars(
                            select(Period).where(
                                Period.ledger_set_id == ls.id,
                                Period.status == "OPEN",
                            )
                        ).all()
                    ],
                }
                for ls in s.scalars(select(LedgerSet)).all()
            ]
            subjects = [
                {
                    "subject_id": sub.id,
                    "type": sub.type,
                    "display_name": sub.display_name,
                    "autonomy_level": sub.autonomy_level,
                }
                for sub in s.scalars(select(Subject)).all()
            ]
            return _ok(ledgers=ledgers, subjects=subjects)

    @mcp.tool()
    def get_voucher(voucher_id: str) -> dict:
        """按 id 取凭证全量（状态机当前态 + 分录明细）。"""
        with repo.session() as s:
            v = s.get(Voucher, voucher_id)
            if v is None:
                return _err("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
            line_ids = [ln.account_id for ln in v.lines]
            codes = {
                a.id: a
                for a in s.scalars(select(Account).where(Account.id.in_(line_ids)))
            }
            lines = [
                {
                    "line_no": ln.line_no,
                    "account_code": codes[ln.account_id].code,
                    "account_name": codes[ln.account_id].name,
                    "debit": _fmt(ln.debit),
                    "credit": _fmt(ln.credit),
                    "aux_dims": ln.aux_dims or {},
                }
                for ln in v.lines
            ]
            return _ok(
                voucher={
                    "id": v.id,
                    "voucher_no": v.voucher_no,
                    "voucher_date": v.voucher_date.isoformat(),
                    "status": v.status,
                    "summary": v.summary or "",
                    "lines": lines,
                }
            )

    @mcp.tool()
    def query_balances(
        ledger_set_id: str, period_year: int, period_month: int, account_prefix: str = ""
    ) -> dict:
        """查期间发生额投影（过账后可见）。account_prefix 过滤科目编码前缀。"""
        with repo.session() as s:
            per = s.scalars(
                select(Period).where(
                    Period.ledger_set_id == ledger_set_id,
                    Period.year == period_year,
                    Period.month == period_month,
                )
            ).first()
            if per is None:
                return _err(
                    "PERIOD_NOT_FOUND", f"{period_year}-{period_month:02d} 期间不存在"
                )
            balances = []
            for b in s.scalars(select(Balance).where(Balance.period_id == per.id)):
                acc = s.get(Account, b.account_id)
                code = acc.code if acc else "?"
                if account_prefix and not code.startswith(account_prefix):
                    continue
                balances.append(
                    {
                        "account_code": code,
                        "account_name": acc.name if acc else "?",
                        "dims_key": b.dims_key,
                        "debit_total": _fmt(b.debit_total),
                        "credit_total": _fmt(b.credit_total),
                    }
                )
            balances.sort(key=lambda r: r["account_code"])
            return _ok(period={"year": period_year, "month": period_month}, balances=balances)

    # ---------- 写入链路 ----------

    @mcp.tool()
    def create_voucher(
        ledger_set_id: str,
        voucher_date: str,
        actor_id: str,
        summary: str = "",
        idempotency_key: str | None = None,
        lines: list[dict] | None = None,
    ) -> dict:
        """创建草稿凭证并即时硬校验。

        lines 形如 [{"account_code":"6602","debit":"800","credit":""}]，金额字符串。
        不平衡/金额非法/科目不存在将直接拒绝（VOUCHER_UNBALANCED 等）。
        """
        try:
            with repo.session() as sess:
                from decimal import Decimal as _D

                from kernel.authz import AuthzError, check_agent_quota, enforce

                enforce(sess, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="voucher:create")
                total = sum(_D(ln.get("debit") or "0") for ln in (lines or []))
                check_agent_quota(
                    sess, actor_id=actor_id, voucher_amount=total,
                )
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        actor = {"type": "user", "id": actor_id}
        try:
            with repo.session() as s:
                try:
                    d = date.fromisoformat(voucher_date)
                except ValueError:
                    return _err("DATE_INVALID", f"日期格式应为 YYYY-MM-DD: {voucher_date!r}")

                period = s.scalars(
                    select(Period).where(
                        Period.ledger_set_id == ledger_set_id,
                        Period.year == d.year,
                        Period.month == d.month,
                    )
                ).first()
                accounts = {
                    a.code: a
                    for a in s.scalars(
                        select(Account).where(Account.ledger_set_id == ledger_set_id)
                    ).all()
                }

                posting_lines: list[PostingLine] = []
                orm_lines: list[VoucherLine] = []
                for i, ln in enumerate(lines or [], start=1):
                    code = (ln.get("account_code") or "").strip()
                    acc = accounts.get(code)
                    if acc is None:
                        return _err("ACCOUNT_NOT_FOUND", f"第 {i} 行科目不存在: {code!r}")
                    dr = _amount(ln.get("debit"), f"第{i}行借方")
                    cr = _amount(ln.get("credit"), f"第{i}行贷方")
                    dims = ln.get("aux_dims") or {}
                    posting_lines.append(PostingLine(acc.id, dr, cr, dims))
                    orm_lines.append(
                        VoucherLine(
                            line_no=i,
                            account_id=acc.id,
                            debit=dr,
                            credit=cr,
                            aux_dims=dims or None,
                        )
                    )

                _validate_voucher(
                    lines=posting_lines,
                    accounts_by_id={a.id: a for a in accounts.values()},
                    period_status=period.status if period else "MISSING",
                    period_year=d.year,
                    period_month=d.month,
                    voucher_date=d,
                )
                if period is None:
                    return _err(
                        "PERIOD_NOT_FOUND", f"{d.year}-{d.month:02d} 期间不存在，请先初始化"
                    )

                seq = (
                    len(
                        s.scalars(
                            select(Voucher.id).where(
                                Voucher.ledger_set_id == ledger_set_id
                            )
                        ).all()
                    )
                    + 1
                )
                v = Voucher(
                    ledger_set_id=ledger_set_id,
                    period_id=period.id,
                    voucher_no=f"记-{seq:04d}",
                    voucher_date=d,
                    status="DRAFT",
                    summary=summary,
                    created_by=actor_id,
                    idempotency_key=idempotency_key,
                    lines=orm_lines,
                )
                s.add(v)
                try:
                    s.flush()
                except IntegrityError:
                    s.rollback()
                    prior = s.scalars(
                        select(Voucher).where(Voucher.idempotency_key == idempotency_key)
                    ).first()
                    return _ok(voucher=_brief(prior), replayed=True)

                from kernel.ledger import append_event

                append_event(
                    s,
                    ledger_set_id=v.ledger_set_id,
                    event_type="voucher.created",
                    aggregate_id=v.id,
                    payload=_snapshot(s, v),
                    actor=actor,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def push_voucher(voucher_id: str, actor_id: str) -> dict:
        """提交待审：DRAFT → PUSHED。"""
        return guarded(voucher_id, actor_id, "PUSHED")

    @mcp.tool()
    def approve_voucher(voucher_id: str, actor_id: str) -> dict:
        """审批通过：PUSHED → APPROVED。制单人与审批人不能相同（NO_SELF_APPROVAL）。"""
        return guarded(voucher_id, actor_id, "APPROVED")

    @mcp.tool()
    def post_voucher(voucher_id: str, actor_id: str) -> dict:
        """记账：APPROVED → POSTED。写 voucher.posted 事件并累计余额投影。"""
        try:
            with repo.session() as s:
                _post_voucher(s, voucher_id=voucher_id, actor={"type": "user", "id": actor_id})
                s.flush()
                v = s.get(Voucher, voucher_id)
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def cancel_post_voucher(voucher_id: str, actor_id: str) -> dict:
        """撤销记账：POSTED → DRAFT（补偿事务）。

        仅未结账期间可撤；原 POSTED 事件不修改，追加 voucher.cancelled 事件；
        余额投影同步回冲。Agent 主体须 L3 自治等级，人不受限。
        """
        try:
            with repo.session() as s:
                from kernel.state import cancel_post_voucher

                v = cancel_post_voucher(
                    s,
                    voucher_id=voucher_id,
                    actor={"type": "user", "id": actor_id},
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def feishu_send_approval(
        voucher_id: str,
        receive_id: str,
        receive_id_type: str = "open_id",
    ) -> dict:
        """把待审凭证推送为飞书审批卡片（PUSHED 状态凭证）。

        receive_id_type: open_id | chat_id 等；接收人由 scripts/feishu_ws.py 绑定流程获得。
        卡片上的批准/驳回按钮经长连接回调写回状态机。
        """
        try:
            with repo.session() as s:
                from xerp_mcp.feishu import FeishuError, build_approval_card, send_card

                v = s.get(Voucher, voucher_id)
                if v is None:
                    return _err("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
                if v.status != "PUSHED":
                    return _err(
                        "INVALID_TRANSITION",
                        f"仅待审（PUSHED）凭证可推送审批卡片，当前 {v.status}",
                    )
                line_ids = [ln.account_id for ln in v.lines]
                cmap = {a.id: a for a in s.scalars(select(Account).where(Account.id.in_(line_ids)))}
                card = build_approval_card(
                    voucher_no=v.voucher_no,
                    status=v.status,
                    summary=v.summary or "",
                    lines=[
                        {
                            "account_code": cmap[ln.account_id].code,
                            "account_name": cmap[ln.account_id].name,
                            "debit": f"{ln.debit:.2f}",
                            "credit": f"{ln.credit:.2f}",
                        }
                        for ln in v.lines
                    ],
                    voucher_id=v.id,
                )
                send_card(receive_id_type=receive_id_type, receive_id=receive_id, card=card)
                return _ok(voucher=_brief(v), sent_to=receive_id)
        except FeishuError as e:
            return _err("FEISHU_ERROR", str(e))

    # ---------- 三大报表（P1-01） ----------

    @mcp.tool()
    def report_balance_sheet(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "small_business",
    ) -> dict:
        """资产负债表：按准则模板聚合资产/负债/所有者权益，返回是否平衡与差额校验。

        本期净利润在结转（P1-02）前挂在权益项下，以保证表内平衡。
        """
        try:
            with repo.session() as s:
                from kernel.reporting.statements import balance_sheet

                return _ok(report=balance_sheet(
                    s, ledger_set_id, period_year, period_month, accounting_standard
                ))
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))

    @mcp.tool()
    def report_income_statement(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "small_business",
    ) -> dict:
        """利润表：营业收入/成本/费用分项 + 净利润（本期发生额口径）。"""
        try:
            with repo.session() as s:
                from kernel.reporting.statements import income_statement

                return _ok(report=income_statement(
                    s, ledger_set_id, period_year, period_month, accounting_standard
                ))
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))

    @mcp.tool()
    def report_cash_flow(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "small_business",
    ) -> dict:
        """现金流量表（直接法）：经营/投资/筹资三类净额 + 期初-净增加-期末勾稽。"""
        try:
            with repo.session() as s:
                from kernel.reporting.statements import cash_flow

                return _ok(report=cash_flow(
                    s, ledger_set_id, period_year, period_month, accounting_standard
                ))
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))

    @mcp.tool()
    def close_period(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        actor_id: str,
        accounting_standard: str = "small_business",
    ) -> dict:
        """期末结转：损益类科目余额结转至本年利润（3103），生成「结转-YYYYMM-NNN」凭证。

        幂等保护：同期间重复调用返回 ALREADY_CLOSED。结转后该期间损益科目清零，
        利润表仍可按凭证分录回放。
        """
        try:
            with repo.session() as s:
                from kernel.closing import close_period as _close

                v = _close(
                    s,
                    ledger_set_id=ledger_set_id,
                    year=period_year,
                    month=period_month,
                    actor={"type": "user", "id": actor_id},
                    standard=accounting_standard,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- 月度运行（P1-06） ----------

    @mcp.tool()
    def open_next_period(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        actor_id: str,
        accounting_standard: str = "small_business",
    ) -> dict:
        """期初结转：把 (period_year, period_month) 的资产负债类期末余额滚入下一期间。

        前置：该期间已执行期末结转（close_period）。生成「期初-YYYYMM-NNN」凭证，
        新期间自动创建为 OPEN。幂等：重复调用返回 ALREADY_OPENED。
        """
        try:
            with repo.session() as s:
                from kernel.carryforward import open_next_period as _open

                v = _open(
                    s,
                    ledger_set_id=ledger_set_id,
                    year=period_year,
                    month=period_month,
                    actor={"type": "user", "id": actor_id},
                    standard=accounting_standard,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- 事件适配器（P2-01） ----------

    @mcp.tool()
    def adapter_list() -> dict:
        """列出已注册的事件适配器规则（第三方业务事件 → 凭证模板）。

        内置规则位于 kernel/data/adapters/*.json；第三方可用 adapter_register 追加。
        """
        from kernel.adapters import list_rules

        return _ok(rules=list_rules())

    @mcp.tool()
    def adapter_preview(adapter: str, event_type: str, event: dict) -> dict:
        """不落库地预览「该事件按规则会生成怎样的凭证」。

        用于规则调试与上线前核对；返回借贷合计与是否平衡。
        """
        try:
            from kernel.adapters import RuleNotFoundError, get_rule, preview

            rule = get_rule(adapter, event_type)
            if rule is None:
                raise RuleNotFoundError(adapter, event_type)
            return _ok(preview=preview(rule, event))
        except (RuleError, EventFieldError, RuleNotFoundError) as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def adapter_ingest(
        ledger_set_id: str,
        actor_id: str,
        adapter: str,
        event_type: str,
        event: dict,
        event_id: str | None = None,
    ) -> dict:
        """消费一个第三方业务事件，按规则自动生成凭证（幂等）。

        凭证状态由规则 target_status 决定（默认 PUSHED 待人审，绝不自动过账）。
        外部事件 id 作为幂等键：同一事件重复投喂返回 replayed=True，不会重复入账。
        每条消费都会追加 adapter.event.consumed 事件，保留来源事件 id 以便追溯。
        """
        try:
            with repo.session() as s:
                from kernel.adapters import AdapterError, ingest_event
                from kernel.authz import AuthzError, enforce

                enforce(
                    s,
                    actor_id=actor_id,
                    ledger_set_id=ledger_set_id,
                    action="voucher:create",
                )
                res = ingest_event(
                    s,
                    ledger_set_id=ledger_set_id,
                    adapter=adapter,
                    event_type=event_type,
                    event=event,
                    actor={"type": "user", "id": actor_id},
                    event_id=event_id,
                )
                s.commit()
                return _ok(**res)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except (AdapterError, RuleError, EventFieldError) as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def adapter_register(rule: dict) -> dict:
        """运行时注册/更新一条适配器规则——第三方接入的唯一入口，零核心改动。

        规则为声明式 JSON：adapter/event_type/version/date_field/summary/lines。
        金额规格只支持 from/const/ratio/op 四种受限形式，不做表达式求值。
        """
        try:
            from kernel.adapters import register, validate_rule

            validate_rule(rule)
            register(rule)
            return _ok(rule=rule)
        except RuleError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- 审计增强（P1-04） ----------

    @mcp.tool()
    def log_agent_decision(
        ledger_set_id: str,
        actor_id: str,
        prompt: str = "",
        output_summary: str = "",
        tool_calls: list[dict] | None = None,
        include_prompt: bool = False,
        model: str = "",
    ) -> dict:
        """AI 决策留痕：把 prompt 哈希（可选全文）、工具调用、输出摘要写入事件账本。

        默认只存 prompt 的 sha256 与字数，避免敏感上下文进入不可篡改账本；
        需留全文时显式传 include_prompt=true（调用方负责脱敏）。
        """
        with repo.session() as s:
            from kernel.agent_audit import log_agent_decision as _log

            evt = _log(
                s,
                ledger_set_id=ledger_set_id,
                actor={"type": "agent", "id": actor_id},
                prompt=prompt,
                tool_calls=tool_calls,
                output_summary=output_summary,
                include_prompt=include_prompt,
                model=model,
            )
            s.commit()
            return _ok(event_id=evt.id, event_type=evt.event_type,
                       prompt_sha256=evt.payload["prompt_sha256"])

    @mcp.tool()
    def reconcile_ledger(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "small_business",
    ) -> dict:
        """账账核对：逐凭证平衡、投影 vs 凭证明细重算、试算平衡、现金流勾稽。

        返回 ok 与 issues 明细；对不上即说明投影被破坏或存在篡改。
        """
        try:
            with repo.session() as s:
                from kernel.reconcile import reconcile_ledger as _rec

                return _ok(report=_rec(
                    s, ledger_set_id, period_year, period_month, accounting_standard
                ))
        except ReconcileError as e:
            return _err("RECONCILE_ERROR", str(e))

    # ---------- 往来余额（P2-02） ----------

    @mcp.tool()
    def partner_balances(
        ledger_set_id: str,
        year: int = 0,
        month: int = 0,
    ) -> dict:
        """往来余额表：按客户/供应商维度聚合应收与应付，回答「谁欠我、我欠谁」。

        数据来自余额投影（适配器挂账的 aux_dims）；未指定期间取最新 OPEN 期间。
        早期未挂维度的余额单独列 untracked_total，不混入明细（脏数据宁暴露不吞）。
        """
        with repo.session() as s:
            from kernel.adapters.partners import partner_balances as _pb

            return _ok(
                report=_pb(
                    s,
                    ledger_set_id,
                    year=year or None,
                    month=month or None,
                )
            )

    # ---------- 发票 OCR（P2-03） ----------

    @mcp.tool()
    def ocr_ingest_invoice(
        ledger_set_id: str,
        actor_id: str,
        invoice: dict | None = None,
        image_base64: str | None = None,
    ) -> dict:
        """一张发票的完整入账流程：提取→校验→查重→凭证草稿。

        invoice 传结构化 JSON（上游视觉 LLM/人工已提取）；image_base64 传图片
        （需配置视觉通道环境变量）。处置三态：
        ingested=已生成 PUSHED 凭证；flagged=校验不过/低置信度，进人工复核
        （未入账，ocr.invoice.flagged 事件可回放）；DUPLICATE_INVOICE=发票号已
        处理过（防重复报销）。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce
                from kernel.ocr import CompositeExtractor, PipelineError
                from kernel.ocr import ingest_invoice as _ingest

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="voucher:create")
                res = _ingest(
                    s, ledger_set_id=ledger_set_id,
                    source=invoice if invoice is not None else image_base64,
                    actor={"type": "user", "id": actor_id},
                    extractor=CompositeExtractor(),
                )
                s.commit()
                return _ok(**res)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except (PipelineError, PostingError) as e:
            code = e.code if isinstance(e, PipelineError) else e.code
            msg = e.message_zh if isinstance(e, PipelineError) else e.message_zh
            return _err(code, msg, getattr(e, "details", None))

    @mcp.tool()
    def ocr_accuracy_report(samples: list[dict]) -> dict:
        """字段级准确率抽检报告（DoD：抽检 ≥95%）。

        samples: [{"extracted": {...提取器输出}, "ground_truth": {...人工真值}}]。
        逐字段加权比对（金额容差 ±0.01），返回总体正确率与逐样本明细。
        """
        try:
            with repo.session() as s:
                from kernel.ocr import PipelineError
                from kernel.ocr import accuracy_report as _report

                return _ok(report=_report(s, samples=samples))
        except PipelineError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- Drill 建账向导（P0-10） ----------

    @mcp.tool()
    def init_ledger_set(
        name: str, owner_name: str, accounting_standard: str = "small_business"
    ) -> dict:
        """【建账向导 第 1 步】创建新账套：导入准则科目模板 + 建当月 OPEN 期间 + 注册所有者身份。

        幂等：同名列账套已存在则直接返回（replayed=true）。
        返回 ledger_set_id / owner_subject_id，后续调用都用它们。
        """
        from kernel.coa import import_chart_of_accounts, load_template_rows
        from kernel.db.models import LedgerSet

        with repo.session() as s:
            existing = s.scalars(
                select(LedgerSet).where(LedgerSet.name == name)
            ).first()
            if existing is not None:
                owner = s.scalars(
                    select(Subject).where(Subject.display_name == owner_name)
                ).first()
                return _ok(
                    ledger_set_id=existing.id,
                    owner_subject_id=owner.id if owner else "",
                    accounts_created=0,
                    replayed=True,
                    open_period=_open_period_brief(s, existing.id),
                )
            ls = LedgerSet(name=name, accounting_standard=accounting_standard)
            s.add(ls)
            s.flush()
            stats = import_chart_of_accounts(s, ls.id, load_template_rows())
            today = date.today()
            period = s.scalars(
                select(Period).where(
                    Period.ledger_set_id == ls.id,
                    Period.year == today.year,
                    Period.month == today.month,
                )
            ).first()
            if period is None:
                period = Period(
                    ledger_set_id=ls.id,
                    year=today.year,
                    month=today.month,
                    status="OPEN",
                )
                s.add(period)
            owner = Subject(type="user", display_name=owner_name, autonomy_level=3)
            s.add(owner)
            s.flush()
            s.commit()
            # P1-03：所有者自动获得该账套 admin 角色
            from kernel.authz import grant_ledger_role

            grant_ledger_role(s, ledger_set_id=ls.id, subject_id=owner.id, role="admin")
            return _ok(
                ledger_set_id=ls.id,
                owner_subject_id=owner.id,
                accounts_created=stats["created"],
                open_period={"year": period.year, "month": period.month},
            )

    @mcp.tool()
    def ensure_period(ledger_set_id: str, year: int, month: int) -> dict:
        """确保某期间存在且为 OPEN（跨月记账前置）。已存在则原样返回。"""
        with repo.session() as s:
            period = s.scalars(
                select(Period).where(
                    Period.ledger_set_id == ledger_set_id,
                    Period.year == year,
                    Period.month == month,
                )
            ).first()
            if period is None:
                period = Period(ledger_set_id=ledger_set_id, year=year, month=month, status="OPEN")
                s.add(period)
                s.commit()
            return _ok(period={"year": period.year, "month": period.month, "status": period.status})

    @mcp.tool()
    def import_opening_balances(
        ledger_set_id: str,
        actor_id: str,
        lines: list[dict],
        period_year: int | None = None,
        period_month: int | None = None,
    ) -> dict:
        """【建账向导 第 2 步】导入期初余额（试算平衡自动校验）。

        lines: [{"account_code":"1002","debit":"200000","credit":""}, …]；
        借贷合计必须相等，否则整体拒绝（TRIAL_BALANCE_UNBALANCED）。
        成功生成「期初-NNNN」凭证（直接 POSTED）并更新余额投影。
        """
        try:
            with repo.session() as s:
                from kernel.opening import import_opening_balances as _import

                v = _import(
                    s,
                    ledger_set_id=ledger_set_id,
                    actor={"type": "user", "id": actor_id},
                    lines=lines,
                    period_year=period_year,
                    period_month=period_month,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- 助手 ----------

    def _open_period_brief(s: Session, ledger_set_id: str) -> dict:
        period = s.scalars(
            select(Period).where(
                Period.ledger_set_id == ledger_set_id, Period.status == "OPEN"
            )
        ).first()
        return (
            {"year": period.year, "month": period.month}
            if period
            else {}
        )

    def _brief(v: Voucher) -> dict:
        return {"id": v.id, "voucher_no": v.voucher_no, "status": v.status}

    def _snapshot(s: Session, v: Voucher) -> dict:
        ids = [ln.account_id for ln in v.lines]
        cmap = {
            a.id: a.code
            for a in s.scalars(select(Account).where(Account.id.in_(ids)))
        }
        total_debit = sum((ln.debit for ln in v.lines), Decimal("0"))
        return {
            "voucher_no": v.voucher_no,
            "voucher_date": v.voucher_date.isoformat(),
            "summary": v.summary or "",
            "total_debit": _fmt(total_debit),
            "lines": [
                {
                    "account_code": cmap.get(ln.account_id, "?"),
                    "debit": _fmt(ln.debit),
                    "credit": _fmt(ln.credit),
                    "aux_dims": ln.aux_dims or {},
                }
                for ln in v.lines
            ],
        }

    return mcp


if __name__ == "__main__":
    build_server().run()  # stdio transport；WorkBuddy/Claude 以此接入

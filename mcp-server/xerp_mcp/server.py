"""XErp MCP Server — 七工具（ADR-003 契约）。

启动: python mcp-server/server.py        （stdio transport）
库引用: build_server(db_url, profile=None) -> FastMCP  （测试 / 嵌入 WorkBuddy 用）
    profile 为工具分层档位（minimal|standard|pro），仅裁剪暴露面、不改内核。

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

from xerp_mcp.profiles import disabled_for  # noqa: E402 工具分层（P0-B）：需在 sys.path 自举后导入

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
from kernel.events import E  # noqa: E402
from kernel.signing import pending_signers as _pending_signers  # noqa: E402  (D7)
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


def _resolve_standard(
    session, ledger_set_id: str, requested: str | None
) -> tuple[str | None, dict | None]:
    """会计口径单一真源：**以账套设置为准**（P0-A3）。

    不传（空）→ 取账套 LedgerSet.accounting_standard；
    传了 → 必须与账套值一致，否则返回 STANDARD_MISMATCH 错误。

    历史行为是各工具各自硬编码默认 "small_business"，与账套值冲突时无任何定义，
    报表口径错了极难定位。现在口径只有账套一个来源。

    返回 (standard, err)；err 非空时调用方应直接 return 该错误。
    """
    from kernel.db.models import LedgerSet

    ls = session.get(LedgerSet, ledger_set_id)
    if ls is None:
        return None, _err("LEDGER_NOT_FOUND", f"账套 {ledger_set_id} 不存在")
    actual = ls.accounting_standard or "small_business"
    if requested in (None, ""):
        return actual, None
    if requested != actual:
        return None, _err(
            "STANDARD_MISMATCH",
            f"传入的会计口径「{requested}」与账套「{ls.name}」的设置「{actual}」不一致，"
            f"报表口径以账套设置为准，请去掉 accounting_standard 参数",
            {
                "ledger_set_id": ledger_set_id,
                "requested": requested,
                "actual": actual,
            },
        )
    return actual, None


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


def build_server(db_url: str | None = None, profile: str | None = None) -> FastMCP:
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

    def _action_for(status: str, target: str, is_maker: bool) -> str:
        """PUSHED→DRAFT 的权限按执行人区分：撤回是制单人的动作，驳回是审批人的动作。"""
        if (status, target) == ("PUSHED", "DRAFT"):
            return "voucher:push" if is_maker else "voucher:approve"
        return _ACTION_BY_TARGET[target]

    def guarded(voucher_id: str, actor_id: str, target: str, reason: str = "",
                require_maker: bool | None = None) -> dict:
        """状态跃迁统一入口。

        require_maker：None=不校验身份关系；True=必须是制单人本人（撤回）；
        False=必须不是制单人（驳回/审批）。内核 transition 也会兜底判定，
        这里提前校验只为给出更准确的错误信息。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce

                v0 = s.get(Voucher, voucher_id)
                if v0 is not None:
                    is_maker = str(v0.created_by) == str(actor_id)
                    # 先查身份关系再鉴权：身份类错误对操作人更具可操作性——
                    # 「只有制单人本人可以撤回」比「无 voucher:approve 权限」
                    # 更能直接指导下一步该做什么。
                    if require_maker is True and not is_maker:
                        raise PostingError(
                            "NOT_VOUCHER_MAKER",
                            "只有制单人本人可以撤回该凭证",
                            {"voucher_id": voucher_id, "actor_id": actor_id},
                        )
                    if require_maker is False and is_maker:
                        raise PostingError(
                            "NO_SELF_APPROVAL",
                            "制单人不能驳回自己的凭证；如需收回请改用 withdraw_voucher 撤回",
                            {"voucher_id": voucher_id},
                        )
                    enforce(s, actor_id=actor_id, ledger_set_id=v0.ledger_set_id,
                            action=_action_for(v0.status, target, is_maker))
                v = transition(
                    s,
                    voucher_id=voucher_id,
                    actor={"type": "user", "id": actor_id},
                    target=target,
                    reason=reason,
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
            "审批不通过用 reject_voucher 驳回（原因必填，退回制单人修改）；"
            "制单人在审批前反悔用 withdraw_voucher 撤回。"
            "多级签字凭证（create_voucher 的 required_signers 非空）须用 sign_voucher "
            "逐位签署，全部签完自动 APPROVED；签字未齐时 approve_voucher 会被拦截。"
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

    def _session_context() -> dict:
        """会话自举的实际实现（供 get_session_context 与废弃别名共用）。"""
        from kernel.db.models import LedgerSet, Subject

        with repo.session() as s:
            ledgers = [
                {
                    "ledger_set_id": ls.id,
                    "name": ls.name,
                    "accounting_standard": ls.accounting_standard,
                    "status": ls.status,
                    "open_periods": [
                        {"period_year": p.year, "period_month": p.month}
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
    def get_session_context() -> dict:
        """会话自举：账套列表（含 id 与会计口径）、操作者身份（制单人/审批人及其主体 id）、各账套开放期间。

        **会话开始时第一个调用本工具**，取得 ledger_set_id 与 actor_id 后再进行记账。
        期间字段名统一为 period_year / period_month，与全系统其余工具一致。
        """
        return _session_context()

    @mcp.tool()
    def get_workspace() -> dict:
        """[已废弃] 请用 get_session_context 代替；本工具仅作过渡别名保留，行为完全一致。"""
        return _session_context()

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
        voucher_type: str | None = None,
        required_signers: list[str] | None = None,
    ) -> dict:
        """创建草稿凭证并即时硬校验。

        lines 形如 [{"account_code":"6602","debit":"800","credit":""}]，金额字符串。
        不平衡/金额非法/科目不存在将直接拒绝（VOUCHER_UNBALANCED 等）。

        voucher_type 是分类编号开关（经典模式惯例）：
            None  → 统一编号「记-0001」（默认，行为不变）
            "收"  → 收款凭证「收-0001」，借方涉及现金/银行
            "付"  → 付款凭证「付-0001」，贷方涉及现金/银行
            "转"  → 转账凭证「转-0001」，不涉及现金/银行
            "auto"→ 由系统按资金流向自动判定
        指定类别后各类别独立编号（收-1 号与付-1 号并存）。
        不传则完全走原来的统一编号，老账套不受影响。
        """
        try:
            with repo.session() as sess:
                from decimal import Decimal as _D

                from kernel.authz import AuthzError, check_agent_quota, enforce

                enforce(sess, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="voucher:create")
                from kernel.anomaly import AnomalyError, check_breaker

                try:
                    check_breaker(sess, actor_id)
                except AnomalyError as be:
                    return _err("BREAKER_OPEN", be.message_zh)
                total = sum(_D(ln.get("debit") or "0") for ln in (lines or []))
                check_agent_quota(
                    sess, actor_id=actor_id, voucher_amount=total,
                )
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        actor = {"type": "user", "id": actor_id}
        # 与 Web 制单共用内核唯一实现，避免两条路径两套校验
        from kernel.classic import classify_voucher_type, voucher_prefix
        from kernel.voucher_wizard import (
            create_draft_voucher,
            record_voucher_created,
        )

        # 分类编号：未指定 → "记-"统一编号（与历史行为完全一致）
        vtype = (voucher_type or "").strip() or None
        if vtype == "auto":
            vtype = classify_voucher_type(lines or [])
        prefix = voucher_prefix(vtype)

        try:
            with repo.session() as s:
                v, replayed = create_draft_voucher(
                    s,
                    ledger_set_id=ledger_set_id,
                    actor=actor,
                    voucher_date=voucher_date,
                    summary=summary,
                    lines=lines,
                    idempotency_key=idempotency_key,
                    prefix=prefix,
                    per_prefix=bool(vtype),
                    required_signers=required_signers,
                )
                if replayed:
                    return _ok(voucher=_brief(v), replayed=True)
                record_voucher_created(s, v, actor)
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def push_voucher(voucher_id: str, actor_id: str) -> dict:
        """提交待审：DRAFT → PUSHED。"""
        return guarded(voucher_id, actor_id, "PUSHED")

    @mcp.tool()
    def approve_voucher(voucher_id: str, actor_id: str) -> dict:
        """审批通过：PUSHED → APPROVED。制单人与审批人不能相同（NO_SELF_APPROVAL）。

        多级签字凭证（required_signers 非空）在签字未齐时调用本工具会被 PENDING_SIGNATURES
        拦截——请改用 sign_voucher 完成各签字位。
        """
        return guarded(voucher_id, actor_id, "APPROVED")

    @mcp.tool()
    def sign_voucher(
        voucher_id: str,
        actor_id: str,
        slot: str,
        decision: str = "approved",
        reason: str = "",
    ) -> dict:
        """多级签字：签署一个签字位（出纳 cashier / 主管 manager 等，D7）。

        凭证须已声明 required_signers（create_voucher 的 required_signers 参数传入）。
        - 全部签字位签署 approved → 自动审批通过（APPROVED）；
        - 任一签字位 rejected → 凭证退回制单人（DRAFT）。
        Agent 不能签字；签字人不能是制单人。重复签署同一 approved 位幂等放行。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce

                v0 = s.get(Voucher, voucher_id)
                if v0 is None:
                    return _err("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
                enforce(
                    s,
                    actor_id=actor_id,
                    ledger_set_id=v0.ledger_set_id,
                    action="voucher:approve",
                )
                from kernel.signing import sign_voucher as _sign

                v = _sign(
                    s,
                    voucher_id=voucher_id,
                    slot=slot,
                    actor={"type": "user", "id": actor_id},
                    decision=decision,
                    reason=reason,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except (PostingError, AuthzError) as e:
            code = "FORBIDDEN" if isinstance(e, AuthzError) else e.code
            msg = str(e) if isinstance(e, AuthzError) else e.message_zh
            return _err(code, msg, getattr(e, "details", None))

    @mcp.tool()
    def reject_voucher(voucher_id: str, actor_id: str, reason: str) -> dict:
        """审批驳回：PUSHED → DRAFT，退回制单人修改后可重新提交。

        reason 必填——制单人必须知道单据为什么被退回，否则只能靠猜。
        制单人本人不能驳回自己的凭证，请改用 withdraw_voucher。
        Agent 主体不能处置审批队列。
        """
        return guarded(voucher_id, actor_id, "DRAFT", reason=reason, require_maker=False)

    @mcp.tool()
    def withdraw_voucher(voucher_id: str, actor_id: str, reason: str = "") -> dict:
        """制单人撤回：PUSHED → DRAFT，在审批前自行收回修改（reason 选填）。

        仅制单人本人可用。已通过审批或已在审批中被他人驳回的单据不能再撤回。
        """
        return guarded(voucher_id, actor_id, "DRAFT", reason=reason, require_maker=True)

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
        user: str,
        user_type: str = "open_id",
    ) -> dict:
        """把待审凭证推送为飞书审批卡片（PUSHED 状态凭证）。

        通道参数与企微侧语义对齐：**user = 推送给谁**。
        user_type: open_id | chat_id 等（飞书特有）；接收人由 scripts/feishu_ws.py 绑定流程获得。
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
                send_card(receive_id_type=user_type, receive_id=user, card=card)
                return _ok(voucher=_brief(v), sent_to=user)
        except FeishuError as e:
            return _err("FEISHU_ERROR", str(e))

    @mcp.tool()
    def wecom_send_approval(voucher_id: str, user: str = "") -> dict:
        """把待审凭证推送为企业微信模板卡片（批准/驳回按钮，回调写回状态机）。

        user 为企微 userid，缺省用「绑定」指令写入的 WECOM_RECEIVE_USER。
        回调端点 /wecom/callback 需已通过企微管理后台验证（docs/WECOM.md）。
        """
        try:
            with repo.session() as s:
                from kernel import wecom

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
                card = wecom.build_approval_card(
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
                to_user = user or wecom.default_user()
                wecom.send_approval_card(to_user, card)
                return _ok(voucher=_brief(v), sent_to=to_user)
        except wecom.WecomError as e:
            return _err("WECOM_ERROR", str(e))

    @mcp.tool()
    def wecom_send(content: str, msg_type: str = "text", user: str = "") -> dict:
        """企业微信通知推送：text 或 markdown（agent 主动汇报用）。

        user 为企微 userid，缺省用 WECOM_RECEIVE_USER。
        """
        try:
            from kernel import wecom

            to_user = user or wecom.default_user()
            if msg_type == "markdown":
                wecom.send_markdown(to_user, content)
            else:
                wecom.send_text(to_user, content)
            return _ok(sent_to=to_user, msg_type=msg_type)
        except wecom.WecomError as e:
            return _err("WECOM_ERROR", str(e))

    @mcp.tool()
    def wecom_finish_card(voucher_id: str, user: str = "",
                          result_text: str = "已批准 ✅") -> dict:
        """推送完成态展示卡片：无交互按钮，仅展示凭证最终处理结果。

        与 wecom_send_approval（待审交互卡片）互补；不校验状态机（任意状态均可推，
        便于展示已批准/已驳回/已记账等终态）。task_id 用 finish{voucher_id} 规避企微
        42014（task_id 复用或非法字符）。
        """
        try:
            with repo.session() as s:
                from kernel import wecom

                v = s.get(Voucher, voucher_id)
                if v is None:
                    return _err("VOUCHER_NOT_FOUND", f"凭证 {voucher_id} 不存在")
                to_user = user or wecom.default_user()
                resp = wecom.send_finished_card(to_user, v.voucher_no, result_text, v.id)
                return _ok(
                    voucher=_brief(v),
                    sent_to=to_user,
                    task_id=f"finish{v.id}",
                    msgid=resp.get("msgid"),
                )
        except wecom.WecomError as e:
            return _err("WECOM_ERROR", str(e))

    # ---------- 三大报表（P1-01） ----------

    @mcp.tool()
    def report_balance_sheet(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "",
    ) -> dict:
        """资产负债表：按准则模板聚合资产/负债/所有者权益，返回是否平衡与差额校验。

        本期净利润在结转（P1-02）前挂在权益项下，以保证表内平衡。

        accounting_standard 默认留空 = 取账套设置（推荐）；显式传值必须与账套一致。
        """
        try:
            with repo.session() as s:
                from kernel.reporting.statements import balance_sheet

                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                return _ok(report=balance_sheet(
                    s, ledger_set_id, period_year, period_month, standard
                ))
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))

    @mcp.tool()
    def report_income_statement(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "",
    ) -> dict:
        """利润表：营业收入/成本/费用分项 + 净利润（本期发生额口径）。

        accounting_standard 默认留空 = 取账套设置；显式传值必须与账套一致。
        """
        try:
            with repo.session() as s:
                from kernel.reporting.statements import income_statement

                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                return _ok(report=income_statement(
                    s, ledger_set_id, period_year, period_month, standard
                ))
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))

    @mcp.tool()
    def report_cash_flow(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        accounting_standard: str = "",
    ) -> dict:
        """现金流量表（直接法）：经营/投资/筹资三类净额 + 期初-净增加-期末勾稽。

        accounting_standard 默认留空 = 取账套设置；显式传值必须与账套一致。
        """
        try:
            with repo.session() as s:
                from kernel.reporting.statements import cash_flow

                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                return _ok(report=cash_flow(
                    s, ledger_set_id, period_year, period_month, standard
                ))
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))

    @mcp.tool()
    def forecast_statements(
        ledger_set_id: str,
        base_year: int,
        base_month: int,
        horizon: int = 12,
        scenario: str = "base",
        assumptions_json: str = "",
        accounting_standard: str = "",
    ) -> dict:
        """三表前向预测（P1-01 预测）：以 base 期末实际三表为种子，按驱动假设外推未来 horizon 期。

        scenario: base / best / worst / all（all 返回三情景对比，便于做区间）。
        假设默认从上期末实际数自动推导（毛利率、费用率、应收/应付/存货周转天数等）；
        assumptions_json 可覆盖，如 '{"rev_growth":"0.05","gross_margin":"0.55","capex_pct":"0.1"}'
        （rev_growth 为月度收入增长率；ar_days/ap_days/inv_days 为周转天数）。
        返回每期利润表 / 资产负债表 / 现金流量表，三表内部完全勾稽
        （balanced 与 cash_flow.reconcile.ok 均为 true）。预测是物化视图，不写账本。
        """
        try:
            import json
            from dataclasses import replace
            from decimal import Decimal

            from kernel.forecast import (
                extract_seed_from_actuals,
                forecast_from_actuals,
            )

            with repo.session() as s:
                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                override = None
                if assumptions_json:
                    _int_fields = {"ar_days", "ap_days", "inv_days", "periods_per_year"}
                    raw = json.loads(assumptions_json)
                    _, derived = extract_seed_from_actuals(
                        s, ledger_set_id, base_year, base_month, standard
                    )
                    fields = {
                        k: (int(v) if k in _int_fields else Decimal(str(v)))
                        for k, v in raw.items()
                    }
                    override = replace(derived, **fields)
                out = forecast_from_actuals(
                    s, ledger_set_id, base_year, base_month,
                    horizon, scenario, override, standard,
                )
                return _ok(forecast=out)
        except ReportError as e:
            return _err("REPORT_ERROR", str(e))
        except (ValueError, json.JSONDecodeError) as e:
            return _err("FORECAST_BAD_ASSUMPTIONS", f"假设参数无效：{e}")

    @mcp.tool()
    def close_period(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
        actor_id: str,
        accounting_standard: str = "",
    ) -> dict:
        """期末结转：损益类科目余额结转至本年利润（3103），生成「结转-YYYYMM-NNN」凭证。

        幂等保护：同期间重复调用返回 ALREADY_CLOSED。结转后该期间损益科目清零，
        利润表仍可按凭证分录回放。

        accounting_standard 默认留空 = 取账套设置；显式传值必须与账套一致。
        """
        try:
            with repo.session() as s:
                from kernel.closing import close_period as _close

                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                v = _close(
                    s,
                    ledger_set_id=ledger_set_id,
                    year=period_year,
                    month=period_month,
                    actor={"type": "user", "id": actor_id},
                    standard=standard,
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
        accounting_standard: str = "",
    ) -> dict:
        """期初结转：把 (period_year, period_month) 的资产负债类期末余额滚入下一期间。

        前置：该期间已执行期末结转（close_period）。生成「期初-YYYYMM-NNN」凭证，
        新期间自动创建为 OPEN。幂等：重复调用返回 ALREADY_OPENED。

        accounting_standard 默认留空 = 取账套设置；显式传值必须与账套一致。
        """
        try:
            with repo.session() as s:
                from kernel.carryforward import open_next_period as _open

                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                v = _open(
                    s,
                    ledger_set_id=ledger_set_id,
                    year=period_year,
                    month=period_month,
                    actor={"type": "user", "id": actor_id},
                    standard=standard,
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
        accounting_standard: str = "",
    ) -> dict:
        """账账核对：逐凭证平衡、投影 vs 凭证明细重算、试算平衡、现金流勾稽。

        返回 ok 与 issues 明细；对不上即说明投影被破坏或存在篡改。

        accounting_standard 默认留空 = 取账套设置；显式传值必须与账套一致。
        """
        try:
            with repo.session() as s:
                from kernel.reconcile import reconcile_ledger as _rec

                standard, err = _resolve_standard(s, ledger_set_id, accounting_standard)
                if err:
                    return err
                return _ok(report=_rec(
                    s, ledger_set_id, period_year, period_month, standard
                ))
        except ReconcileError as e:
            return _err("RECONCILE_ERROR", str(e))

    # ---------- 往来余额（P2-02） ----------

    @mcp.tool()
    def partner_balances(
        ledger_set_id: str,
        period_year: int = 0,
        period_month: int = 0,
    ) -> dict:
        """往来余额表：按客户/供应商维度聚合应收与应付，回答「谁欠我、我欠谁」。

        数据来自余额投影（适配器挂账的 aux_dims）；未指定期间取最新 OPEN 期间。
        早期未挂维度的余额单独列 untracked_total，不混入明细（脏数据宁暴露不吞）。

        期间统一用 period_year/period_month（与全系统一致）；传 0 表示取最新 OPEN 期间。
        """
        with repo.session() as s:
            from kernel.adapters.partners import partner_balances as _pb

            return _ok(
                report=_pb(
                    s,
                    ledger_set_id,
                    year=period_year or None,
                    month=period_month or None,
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

    # ---------- 银行对账（P2-04） ----------

    @mcp.tool()
    def bank_import_csv(
        ledger_set_id: str,
        actor_id: str,
        csv_text: str,
    ) -> dict:
        """导入银行流水 CSV（表头：date,amount,counterparty,summary,txn_id）。

        amount 正 = 银行收到、负 = 银行支出。流水以 bank.txn.imported 事件落链
        （append-only，流水号幂等——重复导入自动跳过），零新表。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce
                from kernel.bankrec import BankRecError, import_csv

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="ledger:read")
                res = import_csv(s, ledger_set_id=ledger_set_id, csv_text=csv_text,
                                 actor={"type": "user", "id": actor_id})
                s.commit()
                return _ok(**res)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except BankRecError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def bank_reconcile(
        ledger_set_id: str,
        actor_id: str,
        bank_code: str = "100201",
        window_days: int = 15,
        persist: bool = True,
    ) -> dict:
        """自动勾对并输出未达账项报告。

        一对一贪心匹配：金额相等 + 方向一致 + 日期差最小（≤window_days 天）。
        返回 matched（已勾对）/ bank_only（银行已收付企业未记账）/
        book_only（企业已记账银行未到账，在途）。persist=False 只试算不落勾对事件。
        """
        try:
            with repo.session() as s:
                from kernel.bankrec import BankRecError
                from kernel.bankrec import reconcile as _rec

                rep = _rec(s, ledger_set_id=ledger_set_id,
                           actor={"type": "user", "id": actor_id},
                           bank_code=bank_code, window_days=window_days,
                           persist=persist)
                if persist:
                    s.commit()
                return _ok(report=rep)
        except BankRecError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- 关账 Agent（P3-01） ----------

    @mcp.tool()
    def monthend_run(
        ledger_set_id: str,
        actor_id: str,
        period_year: int,
        period_month: int,
        dry_run: bool = False,
    ) -> dict:
        """关账 Agent：检查未审凭证→催办→结转→试算→报表草稿→开下期。

        dry_run=True 只检查+催办不动账。正式执行时若存在未审凭证会中止
        （PENDING_VOUCHERS，催办已发）——Agent 永不代审，人工闸门不绕过。
        全程产出 agent.monthend.run 事件，可回放。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce
                from kernel.monthend import MonthendError
                from kernel.monthend import run_monthend as _run

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="ledger:close")
                rep = _run(
                    s, ledger_set_id=ledger_set_id, year=period_year,
                    month=period_month,
                    actor={"type": "user", "id": actor_id},
                    dry_run=dry_run,
                )
                if not dry_run:
                    s.commit()
                return _ok(report=rep)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except (MonthendError, PostingError) as e:
            code = e.code if isinstance(e, MonthendError) else e.code
            msg = e.message_zh if isinstance(e, MonthendError) else e.message_zh
            return _err(code, msg, getattr(e, "details", None))

    # ---------- 异常侦测（P3-02） ----------

    @mcp.tool()
    def anomaly_scan(
        ledger_set_id: str,
        actor_id: str,
        voucher_id: str,
    ) -> dict:
        """对单张凭证执行异常侦测（规则+LLM 双通道）。

        命中大额/频率规则且创建主体是 Agent → 断路器自动跳闸冻结其自治；
        人类主体只记录事件不冻结。检出结果落 agent.anomaly.detected 事件。
        """
        try:
            with repo.session() as s:
                from kernel.anomaly import scan_voucher
                from kernel.authz import AuthzError, enforce

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="ledger:read")
                v = s.get(Voucher, voucher_id)
                if v is None or v.ledger_set_id != ledger_set_id:
                    return _err("NOT_FOUND", "凭证不存在")
                findings = scan_voucher(
                    s, v, actor={"type": "user", "id": actor_id})
                s.commit()
                return _ok(findings=[{"rule": f.rule, "severity": f.severity,
                                      "message": f.message_zh} for f in findings])
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))

    @mcp.tool()
    def anomaly_release(
        actor_id: str,
        subject_id: str,
        note: str = "",
    ) -> dict:
        """人工解除 Agent 断路器（恢复自治）。Agent 不能自解，需人类 admin 操作。"""
        try:
            with repo.session() as s:
                from kernel.anomaly import release_breaker

                release_breaker(s, subject_id=subject_id,
                                actor={"type": "user", "id": actor_id}, note=note)
                s.commit()
                return _ok(released=subject_id)
        except Exception as e:  # noqa: BLE001
            return _err("RELEASE_FAILED", str(e))

    # ---------- L3 自治档（P3-03） ----------

    @mcp.tool()
    def autonomy_post(
        ledger_set_id: str,
        actor_id: str,
        voucher_date: str,
        summary: str,
        lines: list[dict],
    ) -> dict:
        """L3 自治过账：autonomy_level≥3 且断路器闭合且单日额度内 → 直接 POSTED。

        不是 Agent 自审——是系统规则执行（额度内），全部凭证进入抽检池。
        超额度 QUOTA_EXCEEDED / 断路器开 BREAKER_OPEN / 非 L3 主体 L3_REQUIRED。
        """
        try:
            with repo.session() as s:
                from decimal import Decimal as _D

                from kernel.authz import AuthzError, enforce
                from kernel.autonomy import AutonomyError, autonomous_post

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="voucher:create")
                res = autonomous_post(
                    s, ledger_set_id=ledger_set_id,
                    voucher_date=date.fromisoformat(voucher_date),
                    actor_id=actor_id, summary=summary,
                    lines=[(ln["account_code"],
                            _D(ln.get("debit") or "0"),
                            _D(ln.get("credit") or "0")) for ln in lines],
                )
                s.commit()
                return _ok(**res)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except AutonomyError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def autonomy_audit_list(ledger_set_id: str) -> dict:
        """抽检池：全部 L3 自治过账凭证及其抽检状态（pending/passed/reversed）。"""
        with repo.session() as s:
            from kernel.autonomy import audit_list as _list

            return _ok(**_list(s, ledger_set_id=ledger_set_id))

    @mcp.tool()
    def autonomy_audit_review(
        ledger_set_id: str,
        actor_id: str,
        voucher_id: str,
        verdict: str,
        note: str = "",
    ) -> dict:
        """抽检裁决（人工）：pass=通过 / reverse=推翻（生成红字冲销凭证并过账）。

        已裁决凭证不可重复裁决（ALREADY_REVIEWED）。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce
                from kernel.autonomy import AutonomyError, audit_review

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="ledger:manage")
                res = audit_review(s, voucher_id=voucher_id, verdict=verdict,
                                   reviewer_id=actor_id, note=note)
                s.commit()
                return _ok(**res)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except AutonomyError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def autonomy_replay(voucher_id: str) -> dict:
        """一键回放：按凭证聚合全部事件（创建/推送/审批/过账/AI 决策），审计轨迹不漏一行。"""
        with repo.session() as s:
            from kernel.autonomy import AutonomyError
            from kernel.autonomy import replay as _replay

            try:
                return _ok(**_replay(s, voucher_id=voucher_id))
            except AutonomyError as e:
                return _err(e.code, e.message_zh, e.details)

    # ---------- 账簿查询（复盘 D2） ----------

    @mcp.tool()
    def ledger_detail(
        ledger_set_id: str,
        account_code: str,
        period_year: int,
        period_month: int,
    ) -> dict:
        """科目明细账：期初余额 + 逐笔分录（滚动余额）+ 期末合计，可联查凭证。

        仅 POSTED 凭证（法定账簿口径）；余额方向按科目档案（借/贷）。
        """
        try:
            with repo.session() as s:
                from kernel.ledgerbook import LedgerBookError, ledger_detail

                return _ok(detail=ledger_detail(
                    s, ledger_set_id=ledger_set_id,
                    account_code=account_code,
                    year=period_year, month=period_month,
                ))
        except LedgerBookError as e:
            return _err(e.code, e.message_zh, e.details)

    # ---------- 转账模板（复盘 D3） ----------

    @mcp.tool()
    def transfer_define(template: dict) -> dict:
        """注册/更新转账模板（声明式 JSON：取数公式=科目×scope×ratio）。

        Agent 原生入口：把自然语言转账规则转成模板 JSON 后调用本工具。
        模板分录必须借贷两侧齐全；取数 source 仅支持 balance 投影。
        """
        try:
            from kernel.transfers import TransferError, register_template

            register_template(template)
            return _ok(registered=template["name"])
        except TransferError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def transfer_list() -> dict:
        """列出全部转账模板。"""
        from kernel.transfers import list_templates

        return _ok(templates=list_templates())

    @mcp.tool()
    def transfer_run(
        ledger_set_id: str,
        actor_id: str,
        template_name: str,
        period_year: int,
        period_month: int,
    ) -> dict:
        """执行转账模板：按取数公式生成 PUSHED 凭证（模拟计算待人审）。

        幂等（同期间同名模板 ALREADY_RUN）；不平衡 TEMPLATE_UNBALANCED；
        取数为零 NOTHING_TO_TRANSFER。
        """
        try:
            with repo.session() as s:
                from kernel.authz import AuthzError, enforce
                from kernel.transfers import TransferError, run_template

                enforce(s, actor_id=actor_id, ledger_set_id=ledger_set_id,
                        action="voucher:create")
                res = run_template(s, ledger_set_id=ledger_set_id,
                                   template_name=template_name,
                                   year=period_year, month=period_month,
                                   actor={"type": "user", "id": actor_id})
                s.commit()
                return _ok(**res)
        except AuthzError as e:
            return _err("FORBIDDEN", str(e))
        except TransferError as e:
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
    def ensure_period(ledger_set_id: str, period_year: int, period_month: int) -> dict:
        """确保某期间存在且为 OPEN（跨月记账前置）。已存在则原样返回。

        期间参数统一为 period_year / period_month（与全系统一致）。
        """
        with repo.session() as s:
            period = s.scalars(
                select(Period).where(
                    Period.ledger_set_id == ledger_set_id,
                    Period.year == period_year,
                    Period.month == period_month,
                )
            ).first()
            if period is None:
                period = Period(
                    ledger_set_id=ledger_set_id,
                    year=period_year,
                    month=period_month,
                    status="OPEN",
                )
                s.add(period)
                s.commit()
            return _ok(
                period={
                    "period_year": period.year,
                    "period_month": period.month,
                    "status": period.status,
                }
            )

    @mcp.tool()
    def import_opening_balances(
        ledger_set_id: str,
        actor_id: str,
        lines: list[dict],
        period_year: int | None = None,
        period_month: int | None = None,
        force: bool = False,
    ) -> dict:
        """【建账向导 第 2 步】导入期初余额（试算平衡自动校验 + 防重复导入）。

        lines: [{"account_code":"1002","debit":"200000","credit":""}, …]；
        借贷合计必须相等，否则整体拒绝（TRIAL_BALANCE_UNBALANCED）。
        成功生成「期初-NNNN」凭证（直接 POSTED）并更新余额投影。

        幂等：同一账套已存在期初凭证时，默认返回 OPENING_ALREADY_IMPORTED 拒绝
        （重复导入会直接把期初翻倍，属毁账级事故）。确需重导时显式传 force=true：
        先红字冲销全部旧期初（余额归零 + 审计留痕，原凭证保留），再导入新期初。
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
                    force=force,
                )
                s.flush()
                return _ok(voucher=_brief(v))
        except PostingError as e:
            return _err(e.code, e.message_zh, e.details)

    @mcp.tool()
    def precheck_close(ledger_set_id: str, period_year: int, period_month: int) -> dict:
        """结账前体检：一次查完四道闸门，返回「还差什么」的清单。

        结账之所以需要仪式感，不是因为点了什么按钮，而是因为系统必须
        **明确告诉你还差什么**。本工具不抛异常、不改任何数据，只做体检：

            1. 上月是否已结账（会计期间必须连续闭合）
            2. 本月是否还有未记账凭证
            3. 本月试算是否平衡
            4. 本月损益是否已结转

        返回 can_close + 逐条 checks（item/passed/detail/hint）+ 中文 summary。
        建议结账前先调它，把 summary 原样告诉用户。
        """
        try:
            with repo.session() as s:
                from kernel.classic import precheck_close as _precheck

                return _ok(
                    **_precheck(
                        s,
                        ledger_set_id=ledger_set_id,
                        year=period_year,
                        month=period_month,
                    )
                )
        except Exception as e:  # pragma: no cover - 兜底，保持 _ok/_err 契约
            return _err("PRECHECK_FAILED", f"结账体检失败：{e}")

    @mcp.tool()
    def month_end_guide(
        ledger_set_id: str,
        period_year: int,
        period_month: int,
    ) -> dict:
        """账套状态引导：把"这个月我该怎么走"讲给财务新手听（阶段0）。

        只读、不写、不抛账务错误。一次返回你本月所处阶段 + 最该做的一件事：

            phase      no_period 期间未建 / closed 已结账 / empty 尚无记账
                       daily_pending 有凭证未处理完 / closing_pending 结账前待办
                       closing_ready 可结转结账
            counts    本期间各状态凭证数（草稿/待审/已审/已记账）
            next_action 一句话：现在最该做的那件事（新人可直接照着做）
            close     结账闸门明细（复用 precheck_close；仅在进入期末阶段时给出）

        设计要点：刚建账、本期还没记过任何凭证的账套，会被引导去**录期初/记账**，
        而不是被误导去做损益结转——那是 precheck_close 单点会犯的错。
        """
        try:
            with repo.session() as s:
                from kernel.period_guide import month_end_guide as _guide

                return _ok(
                    **_guide(
                        s,
                        ledger_set_id=ledger_set_id,
                        year=period_year,
                        month=period_month,
                    )
                )
        except Exception as e:  # pragma: no cover - 兜底，保持 _ok/_err 契约
            return _err("GUIDE_FAILED", f"账套状态引导失败：{e}")

    @mcp.tool()
    def suggest_summaries(
        ledger_set_id: str, account_code: str | None = None, limit: int = 5
    ) -> dict:
        """常用摘要推荐：某科目历史上用得最多的摘要，按使用次数降序。

        老会计制单不重新打字，只从常用摘要里挑。这里不加表不加缓存——
        摘要本来就是凭证的一部分，历史凭证就是最好的摘要库。

        account_code 为空则统计整个账套。返回 [{summary, used_count}]。
        制单时用户没给摘要，可拿首条作为默认值并告知用户。
        """
        try:
            with repo.session() as s:
                from kernel.classic import suggest_summaries as _suggest

                return _ok(
                    account_code=account_code or "",
                    items=_suggest(
                        s,
                        ledger_set_id=ledger_set_id,
                        account_code=account_code,
                        limit=limit,
                    ),
                )
        except Exception as e:  # pragma: no cover
            return _err("SUGGEST_FAILED", f"摘要推荐失败：{e}")

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
        # status_zh 是给人和 LLM 看的中文状态（POSTED → 已记账）。
        # 枚举值本身不变，新增字段向后兼容。
        from kernel.classic import status_zh

        return {
            "id": v.id,
            "voucher_no": v.voucher_no,
            "status": v.status,
            "status_zh": status_zh(v.status),
            "required_signers": list(v.required_signers or []),
            "pending_signers": (
                _pending_signers(v) if v.required_signers else []
            ),
        }

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

    # ---------- 工具分层（P0-B）----------
    # profile 来自 mcp.json 的 disabledTools / 环境变量 XERP_PROFILE；
    # pro 或 None 不裁剪，其余档位按 profiles.py 单一真源禁用对应工具。
    if profile:
        off = disabled_for(profile)
        if off:
            mcp.disable(names=set(off))

    return mcp


if __name__ == "__main__":
    # stdio transport；WorkBuddy/Claude 以此接入。XERP_PROFILE 可选（minimal|standard|pro）
    build_server(profile=os.environ.get("XERP_PROFILE") or None).run()

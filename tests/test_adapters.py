"""P2-01 TDD：事件适配器框架——第三方业务事件按声明式规则生成凭证。

覆盖：内置规则加载、事件→凭证、幂等重放、规则静态校验、金额受限算子、
非叶子科目拒绝、缺字段拒绝、不平衡拒绝、审计可追溯、零核心改动。
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters import (
    RuleError,
    clear,
    get_rule,
    ingest_event,
    list_rules,
    preview,
    register,
)
from kernel.adapters.engine import AdapterError
from kernel.adapters.spec import EventFieldError, resolve_amount, validate_rule
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Event, Voucher
from kernel.seed import seed_demo_ledger


@pytest.fixture()
def ctx():
    clear()  # 每个用例从干净注册表开始，内置规则按需惰性加载
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    return {"s": s, "ids": ids, "actor": {"type": "user", "id": ids["subject_id"]}}


def _accounts(s: Session) -> dict[str, Account]:
    return {a.code: a for a in s.scalars(select(Account)).all()}


# ---------- 规则注册表 ----------


def test_builtin_rules_loaded():
    """内置规则来自 JSON，无需改代码即可扩展。"""
    clear()
    rules = list_rules()
    assert len(rules) >= 4
    keys = {(r["adapter"], r["event_type"]) for r in rules}
    assert ("ar", "invoice.issued") in keys
    assert ("ap", "payment.made") in keys


def test_register_custom_rule_and_reject_bad_ones():
    """第三方注册自己的规则；非法规则在注册期即被拒绝。"""
    good = {
        "adapter": "crm",
        "event_type": "deal.won",
        "version": "v1",
        "date_field": "won_at",
        "summary": "商机成交 {deal_id}",
        "lines": [
            {"side": "debit", "account": "1122", "amount": {"from": "amount"}},
            {"side": "credit", "account": "6001", "amount": {"from": "amount"}},
        ],
    }
    register(good)
    assert get_rule("crm", "deal.won") is not None

    # 单边：两条都是借方（不是只给一条，那是 TOO_FEW_LINES）
    one_sided = dict(good, lines=[
        {"side": "debit", "account": "1122", "amount": {"from": "amount"}},
        {"side": "debit", "account": "6001", "amount": {"from": "amount"}},
    ])
    with pytest.raises(RuleError) as ei:
        register(one_sided)
    assert ei.value.code == "ONE_SIDED"

    with pytest.raises(RuleError) as ei0:
        register(dict(good, lines=[good["lines"][0]]))
    assert ei0.value.code == "TOO_FEW_LINES"

    with pytest.raises(RuleError) as ei2:
        register({**good, "lines": [
            {"side": "debit", "account": "1122",
             "amount": {"op": "pow", "left": 1, "right": 2}},
            good["lines"][1],
        ]})
    assert ei2.value.code == "BAD_OP"

    # 字段路径以 _ 开头 → 拒绝（防属性逃逸）
    with pytest.raises(RuleError) as ei3:
        register({**good, "lines": [
            {"side": "debit", "account": "1122", "amount": {"from": "__class__"}},
            good["lines"][1],
        ]})
    assert ei3.value.code == "BAD_FIELD_PATH"


# ---------- 金额受限算子 ----------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"from": "total"}, Decimal("100.00")),
        ({"const": "7.50"}, Decimal("7.50")),
        ("42", Decimal("42.00")),
        ({"ratio": 0.01, "of": {"from": "total"}}, Decimal("1.00")),
        ({"op": "add", "left": {"from": "total"}, "right": {"const": "0.5"}},
         Decimal("100.50")),
        ({"op": "sub", "left": {"from": "total"}, "right": {"const": "0.5"}},
         Decimal("99.50")),
        ({"op": "mul", "left": {"from": "total"}, "right": {"const": "1.06"}},
         Decimal("106.00")),
        ({"op": "div", "left": {"from": "total"}, "right": {"const": "4"}},
         Decimal("25.00")),
    ],
)
def test_amount_operators(spec, expected):
    assert resolve_amount(spec, {"total": "100.00"}) == expected


def test_amount_div_by_zero_rejected():
    with pytest.raises(EventFieldError) as ei:
        resolve_amount({"op": "div", "left": {"from": "a"}, "right": {"const": "0"}},
                       {"a": "10"})
    assert ei.value.code == "DIV_BY_ZERO"


def test_no_eval_in_amount_spec():
    """规则里出现表达式字符串必须被拒绝，而不是被求值。"""
    with pytest.raises(RuleError):
        validate_rule({
            "adapter": "x", "event_type": "y", "version": "v1",
            "date_field": "d",
            "lines": [
                {"side": "debit", "account": "1122", "amount": "__import__('os')"},
                {"side": "credit", "account": "6001", "amount": {"const": "1"}},
            ],
        })


# ---------- 事件 → 凭证 ----------


def test_ingest_creates_pushed_voucher(ctx):
    """开票事件 → PUSHED 凭证（待人审，绝不自动过账）。"""
    s, ids = ctx["s"], ctx["ids"]
    event = {
        "event_id": "INV-1", "invoice_no": "INV-1", "issued_at": "2026-08-10",
        "net_amount": "1000.00", "tax_amount": "10.00", "total_amount": "1010.00",
    }
    res = ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="invoice.issued", event=event, actor=ctx["actor"],
    )
    s.commit()
    assert res["replayed"] is False
    assert res["voucher"]["status"] == "PUSHED"
    assert res["voucher"]["summary"] == "销售开票 INV-1"
    accs = _accounts(s)
    v = s.get(Voucher, res["voucher"]["id"])
    lines = {(a.code, ln.debit, ln.credit) for ln, a in
             ((ln, next(x for x in accs.values() if x.id == ln.account_id))
              for ln in v.lines)}
    assert ("1122", Decimal("1010.00"), Decimal("0.00")) in lines
    assert ("6001", Decimal("0.00"), Decimal("1000.00")) in lines
    assert ("222101", Decimal("0.00"), Decimal("10.00")) in lines


def test_ingest_is_idempotent(ctx):
    """同一事件重复投喂只入账一次（外部系统重试友好）。"""
    s, ids = ctx["s"], ctx["ids"]
    event = {
        "event_id": "INV-2", "invoice_no": "INV-2", "issued_at": "2026-08-11",
        "net_amount": "500.00", "tax_amount": "5.00", "total_amount": "505.00",
    }
    first = ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="invoice.issued", event=event, actor=ctx["actor"])
    s.commit()
    again = ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
        event_type="invoice.issued", event=event, actor=ctx["actor"])
    s.commit()
    assert first["replayed"] is False and again["replayed"] is True
    assert first["voucher"]["id"] == again["voucher"]["id"]
    count = len(s.scalars(select(Voucher).where(
        Voucher.ledger_set_id == ids["ledger_set_id"])).all())
    assert count == 1


def test_ingest_appends_traceable_event(ctx):
    """每条消费追加 adapter.event.consumed，保留来源事件 id。"""
    s, ids = ctx["s"], ctx["ids"]
    event = {
        "event_id": "PAY-9", "payee": "云服务商", "paid_at": "2026-08-12",
        "amount": "3600.00",
    }
    res = ingest_event(
        s, ledger_set_id=ids["ledger_set_id"], adapter="ap",
        event_type="payment.made", event=event, actor=ctx["actor"])
    s.commit()
    ev = s.scalars(select(Event).where(
        Event.event_type == "adapter.event.consumed")).one()
    assert ev.payload["external_event_id"] == "PAY-9"
    assert ev.payload["voucher_no"] == res["voucher"]["voucher_no"]
    assert ev.aggregate_id == res["voucher"]["id"]


def test_ingest_rejects_non_leaf_account(ctx):
    """自动入账只允许落在最明细科目（内核不校验，适配器必须更严）。"""
    s, ids, actor = ctx["s"], ctx["ids"], ctx["actor"]
    register({
        "adapter": "bad", "event_type": "x", "version": "v1",
        "date_field": "d", "summary": "非叶子",
        "lines": [
            {"side": "debit", "account": "1002", "amount": {"from": "amount"}},
            {"side": "credit", "account": "6001", "amount": {"from": "amount"}},
        ],
    })
    with pytest.raises(AdapterError) as ei:
        ingest_event(s, ledger_set_id=ids["ledger_set_id"], adapter="bad",
                     event_type="x", event={"d": "2026-08-10", "amount": "10.00"},
                     actor=actor)
    assert ei.value.code == "ACCOUNT_NOT_LEAF"


def test_ingest_rejects_missing_field(ctx):
    s, ids = ctx["s"], ctx["ids"]
    with pytest.raises(EventFieldError) as ei:
        ingest_event(
            s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
            event_type="invoice.issued",
            event={"event_id": "X", "invoice_no": "X", "issued_at": "2026-08-10"},
            actor=ctx["actor"])
    assert ei.value.code == "FIELD_MISSING"


def test_ingest_rejects_unbalanced_output(ctx):
    s, ids = ctx["s"], ctx["ids"]
    register({
        "adapter": "unbal", "event_type": "y", "version": "v1",
        "date_field": "d", "summary": "不平衡",
        "lines": [
            {"side": "debit", "account": "1122", "amount": {"from": "a"}},
            {"side": "credit", "account": "6001", "amount": {"from": "b"}},
        ],
    })
    with pytest.raises(AdapterError) as ei:
        ingest_event(s, ledger_set_id=ids["ledger_set_id"], adapter="unbal",
                     event_type="y",
                     event={"d": "2026-08-10", "a": "100.00", "b": "90.00"},
                     actor=ctx["actor"])
    assert ei.value.code == "ADAPTER_UNBALANCED"


def test_ingest_rejects_unknown_rule_and_period(ctx):
    s, ids = ctx["s"], ctx["ids"]
    from kernel.adapters.registry import RuleNotFoundError

    with pytest.raises(RuleNotFoundError):
        ingest_event(s, ledger_set_id=ids["ledger_set_id"], adapter="nope",
                     event_type="nope", event={}, actor=ctx["actor"])

    # 期间不存在 → 拒绝（提示先开账）
    with pytest.raises(AdapterError) as ei:
        ingest_event(
            s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
            event_type="invoice.issued",
            event={"event_id": "Z", "invoice_no": "Z", "issued_at": "2030-01-01",
                   "net_amount": "1", "tax_amount": "0", "total_amount": "1"},
            actor=ctx["actor"])
    assert ei.value.code == "PERIOD_NOT_FOUND"


# ---------- 预览 ----------


def test_preview_does_not_persist(ctx):
    rule = get_rule("ar", "invoice.issued")
    out = preview(rule, {
        "invoice_no": "INV-P", "net_amount": "100.00", "tax_amount": "1.00",
        "total_amount": "101.00", "issued_at": "2026-08-10",
    })
    assert out["balanced"] is True
    assert out["summary"] == "销售开票 INV-P"
    assert out["debit"] == "101.00"
    assert ctx["s"].scalars(select(Voucher)).all() == []


# ---------- 零核心改动 ----------


def test_adapters_do_not_reach_into_core_privates():
    """「零核心改动」的机器可验证表述：适配器不依赖内核私有符号。

    允许引用 kernel.db.models / kernel.state.transition / kernel.ledger.append_event
    等公开 API；一旦出现下划线开头的内部函数，说明耦合泄漏。
    """
    pkg = Path(__file__).resolve().parent.parent / "kernel" / "adapters"
    offenders = []
    pattern = re.compile(r"^\s*(?:from|import)\s+([^\s]+)(?:\s+import\s+([^\n(]+))?",
                         re.MULTILINE)
    for path in pkg.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for module, names in pattern.findall(text):
            if not module.startswith("kernel."):
                continue
            if module in {"kernel.db.models", "kernel.state", "kernel.ledger",
                          "kernel.adapters", "kernel.adapters.spec",
                          "kernel.adapters.registry"}:
                continue
            for name in (names or "").split(","):
                name = name.strip()
                if name.startswith("_") or module.split(".")[-1].startswith("_"):
                    offenders.append(f"{path.name}: {module} -> {name}")
    assert offenders == [], f"适配器依赖了内核私有实现：{offenders}"


def test_builtin_rule_files_are_valid_json():
    import json

    d = Path(__file__).resolve().parent.parent / "kernel" / "data" / "adapters"
    files = sorted(d.glob("*.json"))
    assert len(files) >= 4
    for f in files:
        validate_rule(json.loads(f.read_text(encoding="utf-8")))

"""D8 契约测试：Integration ↔ Ledger Core 边界（零核心改动断言）。

债务清单 D8：Integration 无 ACL —— 适配器直连内部符号。债务建议：
「保持零核心改动断言即可，暂不动」——即不建完整防腐层（P4 级），
而是把边界契约落成机器可验证断言（边界规则见 ``kernel/adapters/__init__.py``）。

本文件锁死四条结构契约 + 一条运行时契约：
1) Ledger Core 不得反向 import 适配器层（依赖方向：外围→核心）；
2) 适配器不得调用核心内部投影累加器（``_accumulate_balances`` 等下划线前缀）；
3) 适配器不得直写 ``Balance`` 投影（投影只由 ``post_voucher`` 触发）；
4) 适配器驱动凭证状态只经 ``kernel.state.transition`` 公开路径；
5) 运行时证明：适配器 ingest 后产生 ``PUSHED`` 凭证，但绝不落 ``Balance`` 投影行。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.adapters import clear, ingest_event, register
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import Account, Balance, Voucher
from kernel.seed import seed_demo_ledger

_KERNEL_ROOT = Path("kernel")

# Ledger Core 之外的外围模块允许依赖适配器（外围→外围 是合法方向）。
ALLOWED_ADAPTER_IMPORTERS = ("kernel/adapters/", "kernel/ocr/")


def _imports_adapters(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return "from kernel.adapters" in text or "import kernel.adapters" in text


def test_core_ledger_does_not_import_adapters():
    """T1: Ledger Core 模块不得反向 import 适配器层（依赖方向：外围→核心）。"""
    violations = []
    for p in _KERNEL_ROOT.rglob("*.py"):
        rel = str(p).replace("\\", "/")
        if not _imports_adapters(p):
            continue
        if rel.startswith(ALLOWED_ADAPTER_IMPORTERS):
            continue
        violations.append(rel)
    assert violations == [], (
        "Ledger Core 反向依赖了适配器层（违反零核心改动）：" + ", ".join(violations)
    )


def test_adapters_do_not_call_projection_internals():
    """T2: 适配器不得调用核心内部投影累加器（下划线前缀内部符号）。

    仅检测「调用形态」``_accumulate_balances(``；包文档里的反引号说明
    （`` `_accumulate_balances` ``）不带括号，不误伤。
    """
    hits = [
        str(p).replace("\\", "/")
        for p in Path("kernel/adapters").rglob("*.py")
        if "_accumulate_balances(" in p.read_text(encoding="utf-8")
    ]
    assert hits == [], "适配器直连核心投影累加器：" + ", ".join(hits)


def test_adapters_do_not_write_balance_projection():
    """T3: 适配器不得直写 Balance 投影（投影只由 post_voucher 触发）。"""
    hits = []
    for p in Path("kernel/adapters").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "Balance(" in text or "Balance." in text or "session.add(Balance" in text:
            hits.append(str(p).replace("\\", "/"))
    assert hits == [], "适配器直写 Balance 投影：" + ", ".join(hits)


def test_adapters_drive_status_only_via_transition():
    """T4: 适配器驱动凭证状态只经 state.transition；不得裸写 voucher.status=。"""
    text = Path("kernel/adapters/engine.py").read_text(encoding="utf-8")
    # 必须走公开状态机
    assert "transition(" in text, "适配器未走 state.transition 公开路径"
    # 除构造关键字 status=\"DRAFT\" 外，不得有任何裸状态改写（.status = / voucher.status=）
    raw = [
        ln.strip()
        for ln in text.splitlines()
        if ".status =" in ln or "voucher.status=" in ln
    ]
    assert raw == [], "适配器存在裸状态改写（应只经 transition）：" + "; ".join(raw)


# ---- 运行时契约：适配器 ingest 不落 Balance 投影 ----

@pytest.fixture()
def ctx():
    clear()  # 每个用例从干净注册表开始，内置规则按需惰性加载
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = Session(engine)
    ids = seed_demo_ledger(s)
    import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
    s.commit()
    return {
        "s": s,
        "ids": ids,
        "actor": {"type": "user", "id": ids["subject_id"]},
    }


def test_adapter_ingest_yields_pushed_voucher_without_balance(ctx):
    """T5: 运行时证明——适配器产生 PUSHED 凭证，但绝不落 Balance 投影行。"""
    s = ctx["s"]
    ids = ctx["ids"]
    # 动态挑两个「叶子 + 无 aux_dims」科目，规避非叶子拒绝与维度校验
    no_aux_leaf = [
        a
        for a in s.scalars(
            select(Account).where(
                Account.ledger_set_id == ids["ledger_set_id"],
                Account.is_leaf == True,
            )
        ).all()
        if not a.aux_dim_defs
    ]
    assert len(no_aux_leaf) >= 2, "demo 账套缺少足够的叶子无维度科目"
    dr, cr = no_aux_leaf[0], no_aux_leaf[1]
    register(
        {
            "adapter": "cashtest",
            "event_type": "transfer.in",
            "version": "v1",
            "target_status": "PUSHED",
            "summary": "现金转入",
            "date_field": "date",
            "lines": [
                {"side": "debit", "account": dr.code, "amount": {"from": "amount"}},
                {"side": "credit", "account": cr.code, "amount": {"from": "amount"}},
            ],
        }
    )
    out = ingest_event(
        s,
        ledger_set_id=ids["ledger_set_id"],
        adapter="cashtest",
        event_type="transfer.in",
        event={"date": "2026-08-15", "amount": "100.00"},
        actor=ctx["actor"],
    )
    s.commit()
    assert out["voucher"]["status"] == "PUSHED"

    v = s.scalars(select(Voucher).where(Voucher.id == out["voucher"]["id"])).first()
    assert v is not None and v.status == "PUSHED"

    # 投影只由 post_voucher 触发：ingest 之后不应有任何 Balance 行
    bal_rows = s.scalars(
        select(Balance).where(Balance.ledger_set_id == ids["ledger_set_id"])
    ).all()
    assert bal_rows == [], "适配器直写了 Balance 投影（应只由 post_voucher 落）"

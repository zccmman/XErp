"""v1.6 智能体层 O18：账本精灵 7×24 主动推送（推送 ≠ 执行）。

验证：
- 单一推送源 kernel.sprite_push.sprite_push_items 产出结构化提醒清单（月结/异常/
  财报卡片/健康），Web 提醒、MCP 推送、CLI、企微全部同源消费（守 ADR-002）。
- 推送是**只读生成**：不制单、不过账、不结账——任何终态动作仍由 Boss 确认。
- _boss_data 的「账本精灵主动提醒」与 sprite_push_items 同源（单源，不复制）。
- format_wecom_card / format_cli_text 渲染正确，且都不含「已执行/已结账」类动词。
"""

from decimal import Decimal
from tempfile import mkdtemp

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kernel.adapters import ingest_event
from kernel.coa import import_chart_of_accounts, load_template_rows
from kernel.db.base import Base
from kernel.db.models import LedgerSet, Period, Voucher
from kernel.reporting.credit import set_credit_limit
from kernel.reporting.statements import income_statement
from kernel.seed import seed_demo_ledger
from kernel.sprite_push import (
    format_cli_text,
    format_wecom_card,
    sprite_push_items,
)
from kernel.webapp import _boss_data, build_app
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def env():
    d = mkdtemp()
    url = f"sqlite:///{d}/sprite.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        s.commit()
    engine.dispose()
    return {"url": url, "ids": ids}


@pytest.fixture(scope="module")
def ls_info(env):
    engine = create_engine(env["url"])
    with Session(engine) as s:
        ls = s.get(LedgerSet, env["ids"]["ledger_set_id"])
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ls.id)
        ).first()
    return (ls.id, ls.accounting_standard, per.year, per.month)


VALID_TYPES = {"month_end", "anomaly", "report_card", "health", "credit", "collections", "receipt_matching"}
VALID_SEV = {"info", "warn", "alert"}


def test_sprite_push_items_structure(env, ls_info):
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        payload = sprite_push_items(s, ls_id, yr, mo, std)
    assert "items" in payload and "summary" in payload
    assert isinstance(payload["summary"], str) and payload["summary"]
    items = payload["items"]
    assert items, "至少要有一类主动推送（月结/异常/财报卡片）"
    for it in items:
        assert it["type"] in VALID_TYPES, it
        assert it["severity"] in VALID_SEV, it
        for k in ("title", "text", "html", "action_hint"):
            assert k in it and isinstance(it[k], str), it
    # 财报卡片必含：这是 O18 三类事件之一
    assert any(it["type"] == "report_card" for it in items)


def test_sprite_push_report_card_reuses_statements(env, ls_info):
    """财报卡片数字来自内核 income_statement，复用而非复制配平。"""
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        payload = sprite_push_items(s, ls_id, yr, mo, std)
        rev = income_statement(s, ls_id, yr, mo, std)["revenue"]
    rc = next(it for it in payload["items"] if it["type"] == "report_card")
    expect = f"营业收入 {rev:,.2f}"
    assert expect in rc["text"], "财报卡片未复用内核 income_statement 营收"
    assert expect in rc["html"]


def test_sprite_push_is_readonly_no_execution(env, ls_info):
    """推送是只读生成：调用前后凭证数、期间状态不变（不制单/不过账/不结账）。"""
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        before_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ls_id)
        ).first()
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ls_id)
        ).first()
        before_status = per.status
        # 反复调用多次，模拟多端多次拉取推送清单
        for _ in range(3):
            sprite_push_items(s, ls_id, yr, mo, std)
        after_v = s.scalars(
            select(func.count(Voucher.id)).where(Voucher.ledger_set_id == ls_id)
        ).first()
        after_status = s.scalars(
            select(Period).where(Period.ledger_set_id == ls_id)
        ).first().status
    assert before_v == after_v, "sprite_push_items 不应写入凭证"
    assert before_status == after_status, "sprite_push_items 不应改动期间状态"


def test_sprite_push_boss_tips_single_source(env, ls_info):
    """Web 的「账本精灵主动提醒」与 sprite_push_items 同源（单源，不复制文本）。"""
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        d = _boss_data(s, ls_id, yr, mo, std)
        sp = sprite_push_items(s, ls_id, yr, mo, std)
    expected = [it["html"] for it in sp["items"]
                if it["type"] in ("month_end", "anomaly", "health", "credit", "collections",
                                  "receipt_matching")]
    assert d["tips"] == expected, "Web 提醒未与单一推送源 sprite_push_items 对齐"


def test_format_wecom_card_no_execution_verb(env, ls_info):
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        payload = sprite_push_items(s, ls_id, yr, mo, std)
    card = format_wecom_card(payload)
    assert "账本精灵" in card
    assert "人是 Boss" in card, "企微卡片必须明示「人是 Boss」，推送≠执行"
    # 推送 ≠ 执行：卡片里不能出现「已结账/已过账/已执行」等终态动词
    for bad in ("已结账", "已过账", "已执行", "已审批"):
        assert bad not in card, f"企微卡片含执行动词 {bad!r}，违反推送≠执行"


def test_format_cli_text(env, ls_info):
    ls_id, std, yr, mo = ls_info
    engine = create_engine(env["url"])
    with Session(engine) as s:
        payload = sprite_push_items(s, ls_id, yr, mo, std)
    text = format_cli_text(payload)
    assert payload["summary"] in text
    assert any(tag in text for tag in ("[信息]", "[提醒]", "[异常]"))


def test_remind_action_wired():
    """CLI 的 remind 动作已接入 install.py 解析器（不依赖数据库）。"""
    import importlib.util
    from pathlib import Path

    pkg_root = Path(__file__).resolve().parents[2]
    install_py = pkg_root / "xerp-customer-pack" / "install.py"
    if not install_py.exists():
        pytest.skip("xerp-customer-pack/install.py 不在预期位置")
    spec = importlib.util.spec_from_file_location("xerp_install", install_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = mod.build_parser().parse_args(["remind", "--source", str(pkg_root)])
    assert args.action == "remind"


def test_sprite_push_credit_and_collections_items():
    """账本精灵应主动推送信用超额（alert）与逾期催收（L3）提醒（Phase B·AI Runtime）。

    推送 ≠ 执行：credit/collections 项只展示风险与催收草稿，action_hint 不含终态动词。
    """
    d = mkdtemp()
    url = f"sqlite:///{d}/sprite_credit.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        # 一笔历史应收：会逾期（开票日远早于今天）且超授信额度
        s.add(Period(ledger_set_id=ids["ledger_set_id"], year=2026,
                     month=6, status="OPEN"))
        s.commit()
        actor = {"type": "user", "id": ids["subject_id"]}
        ingest_event(
            s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
            event_type="invoice.issued",
            event={
                "event_id": "INV-OVERDUE", "invoice_no": "INV-OVERDUE",
                "customer": "测试逾期客户", "issued_at": "2026-06-01",
                "net_amount": "990.00", "tax_amount": "10.00",
                "total_amount": "1000.00",
            },
            actor=actor,
        )
        set_credit_limit(s, ledger_set_id=ids["ledger_set_id"], dim_key="customer",
                         partner="测试逾期客户", limit="500.00", actor=actor)
        s.commit()

        ls = s.get(LedgerSet, ids["ledger_set_id"])
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ids["ledger_set_id"])
        ).first()
        payload = sprite_push_items(
            s, ids["ledger_set_id"], per.year, per.month, ls.accounting_standard
        )

    credit_items = [it for it in payload["items"] if it["type"] == "credit"]
    coll_items = [it for it in payload["items"] if it["type"] == "collections"]
    breach = next((it for it in credit_items
                   if "测试逾期客户" in it["title"] and it["severity"] == "alert"), None)
    assert breach is not None, "信用超额应推 alert 级 credit 项"
    assert "超额" in breach["title"]
    l3 = next((it for it in coll_items
               if "测试逾期客户" in it["title"] and "催收·L3" in it["title"]), None)
    assert l3 is not None, "严重逾期应推 L3 collections 项"
    # 推送 ≠ 执行：action_hint 不得出现终态动词
    for it in credit_items + coll_items:
        assert "已结账" not in it["action_hint"]
        assert "已执行" not in it["action_hint"]


def test_sprite_push_receipt_matching_item():
    """账本精灵应主动推送「待匹配收款」提醒（Phase C·AI Runtime）。

    推送 ≠ 执行：receipt_matching 项只展示待匹配笔数与金额，action_hint 指向
    arap_propose_receipt_match（不替 Boss 自动核销）。
    """
    d = mkdtemp()
    url = f"sqlite:///{d}/sprite_receipt.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())
        s.add(Period(ledger_set_id=ids["ledger_set_id"], year=2026,
                     month=6, status="OPEN"))
        s.commit()
        actor = {"type": "user", "id": ids["subject_id"]}
        # 一张已开票未清的应收
        ingest_event(
            s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
            event_type="invoice.issued",
            event={
                "event_id": "INV-RM", "invoice_no": "INV-RM",
                "customer": "匹配测试客户", "issued_at": "2026-06-10",
                "net_amount": "990.00", "tax_amount": "10.00",
                "total_amount": "1000.00",
            },
            actor=actor,
        )
        # 一笔未匹配的回款（仅收到钱、还没核销到发票）
        ingest_event(
            s, ledger_set_id=ids["ledger_set_id"], adapter="ar",
            event_type="payment.received",
            event={
                "event_id": "PAY-RM", "customer": "匹配测试客户",
                "received_at": "2026-06-20", "amount": "600.00",
            },
            actor=actor,
        )
        s.commit()

        ls = s.get(LedgerSet, ids["ledger_set_id"])
        per = s.scalars(
            select(Period).where(Period.ledger_set_id == ids["ledger_set_id"])
        ).first()
        payload = sprite_push_items(
            s, ids["ledger_set_id"], per.year, per.month, ls.accounting_standard
        )

    rm_items = [it for it in payload["items"] if it["type"] == "receipt_matching"]
    assert rm_items, "有未匹配回款应推 receipt_matching 项"
    assert rm_items[0]["severity"] == "warn"
    assert "待匹配收款" in rm_items[0]["title"]
    assert "arap_propose_receipt_match" in rm_items[0]["action_hint"]
    assert "已核销" not in rm_items[0]["action_hint"]

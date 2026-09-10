"""P1-06 dogfood：OPC 一人公司月末结账验证（可复用）。

双保险设计，绝不污染真机账套：
  A. 真机库（默认 ledgeros_dev.db）只跑 run_monthend(dry_run=True) —— 检查+催办，
     不动账，如实呈现「还有几张待处理凭证 / 账账核对是否通过」。
  B. 副本库（--copy 指定）删掉已被驳回的测试噪声草稿（记-0016/记-0023，纯噪声，
     与记-0012/19/20 同口径），再跑完整结账 2026-08 → 2026-09，验证：
       · 结账后期间 status=CLOSED（锁期，P1-06 修复点）；
       · 次月 precheck_close 闸门1（上月须 CLOSED）通过（期间链不断裂）；
       · 九月三表干净、资产负债表平衡。

用法：
  python scripts/dogfood_opc_close.py                 # 默认真机库 ledgeros_dev.db + 副本 ./_opc_close_copy.db
  python scripts/dogfood_opc_close.py --copy /tmp/x.db # 指定副本路径
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select, delete
from sqlalchemy.orm import Session

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp-server"))

from kernel.classic import precheck_close  # noqa: E402
from kernel.db.models import Event, Period, Voucher  # noqa: E402
from kernel.monthend import run_monthend  # noqa: E402
from kernel.reporting.statements import balance_sheet, cash_flow, income_statement  # noqa: E402

LS = "2e69a9386c2740f8859cd1323366bf2b"
LIVE = REPO / "ledgeros_dev.db"
ACTOR = {"type": "user", "id": "b42455e7e5b8476ba30df0b1be8f504f"}  # 丞辰


def _d(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.01')):,.2f}"


def live_dry_run(db_path: Path) -> None:
    print("\n========== A. 真机库 DRY-RUN（不动账）==========")
    eng = create_engine(f"sqlite:///{db_path}")
    with Session(eng) as s:
        rep = run_monthend(s, ledger_set_id=LS, year=2026, month=9,
                           actor=ACTOR, dry_run=True)
        print(f"  period         : 2026-09")
        print(f"  dry_run        : {rep['dry_run']}")
        chk = rep["steps"]["check"]
        print(f"  状态计数        : {chk['status_counts']}")
        print(f"  账账核对        : {'通过' if chk['reconcile_ok'] else '存在问题 ' + str(len(chk['reconcile_issues'])) + ' 项'}")
        ch = rep["steps"]["chase"]
        print(f"  待处理凭证数    : {ch['pending_count']}")
        for v in ch["items"]:
            print(f"    [{v['status']}] {v['voucher_no']} {v['summary']}")
        print(f"  结论            : {rep['conclusion']}")
        print("  → 真机 9 月仍含被驳回草稿（记-0016/记-0023），正式结账前需人工处置（删除或补审）。")


def _delete_noise_drafts(eng) -> list[str]:
    """副本库：删掉被驳回的测试噪声草稿（纯噪声，无经济实质）。返回删除的凭证明细。"""
    removed: list[str] = []
    with Session(eng) as s:
        for no in ("记-0016", "记-0023"):
            v = s.scalars(select(Voucher).where(
                Voucher.ledger_set_id == LS, Voucher.voucher_no == no)).first()
            if v is None:
                continue
            s.execute(delete(Event).where(Event.aggregate_id == v.id))
            s.delete(v)
            removed.append(no)
        s.commit()
    return removed


def full_close_on_copy(copy_path: Path) -> None:
    print("\n========== B. 副本库 完整结账（2026-08 → 2026-09）==========")
    eng = create_engine(f"sqlite:///{copy_path}")
    removed = _delete_noise_drafts(eng)
    print(f"  副本删除噪声草稿: {removed or '（无）'}")

    # 关 8 月
    with Session(eng) as s:
        run_monthend(s, ledger_set_id=LS, year=2026, month=8, actor=ACTOR)
        s.commit()
    # 关 9 月
    with Session(eng) as s:
        run_monthend(s, ledger_set_id=LS, year=2026, month=9, actor=ACTOR)
        s.commit()

    # 校验锁期 + 期间链
    with Session(eng) as s:
        def _st(y, m):
            p = s.scalars(select(Period).where(
                Period.ledger_set_id == LS, Period.year == y, Period.month == m)).first()
            return p.status if p else "MISSING"

        print(f"  2026-08 status : {_st(2026, 8)}  (期望 CLOSED)")
        print(f"  2026-09 status : {_st(2026, 9)}  (期望 CLOSED)")
        print(f"  2026-10 status : {_st(2026, 10)}  (期望 OPEN，开下期)")
        assert _st(2026, 8) == "CLOSED"
        assert _st(2026, 9) == "CLOSED"
        assert _st(2026, 10) == "OPEN"

        # 次月 precheck_close 闸门1（P1-06 修复点：上月须 CLOSED 才能连续闭合）
        rep = precheck_close(s, ledger_set_id=LS, year=2026, month=10)
        gate1 = next((c for c in rep["checks"] if c["item"] == "上月已结账"), None)
        print(f"  2026-10 关账体检 can_close={rep['can_close']}")
        for c in rep["checks"]:
            print(f"    闸门[{'√' if c['passed'] else '×'}] {c['item']}：{c['detail']}")
        # 锁期修复验证：闸门1（上月已结账）必须 True。
        # 注：can_close 可能为 False 仅因 10 月是刚开出的空新期间、闸门4
        # 「损益已结转」要求 结转-202610 凭证，而空月份 close_period 抛
        # NOTHING_TO_CLOSE 无法生成——此边界与锁期修复无关，10 月跑真实业务后即正常。
        assert gate1 is not None and gate1["passed"] is True

        # 九月三表
        inc = income_statement(s, LS, 2026, 9)
        bs = balance_sheet(s, LS, 2026, 9)
        cf = cash_flow(s, LS, 2026, 9)
        print("\n  --- 九月三表（副本结账后）---")
        print(f"  利润表  收入={_d(inc['revenue'])} 费用={_d(inc['expense'])} "
              f"净利润={_d(inc['net_profit'])}")
        print(f"  资产负债表 资产={_d(bs['assets']['total'])} "
              f"负债={_d(bs['liabilities']['total'])} 权益={_d(bs['equity']['total'])} "
              f"平衡={bs['balanced']} diff={_d(bs['check']['diff'])}")
        print(f"  现金流量表 净增加={_d(cf['net_increase'])} 勾稽={cf['reconcile']}")
        assert bs["balanced"] is True and bs["check"]["diff"] == Decimal("0.00")
    print("\n  ✅ 副本库结账验证通过：锁期生效、期间链连续、九月三表平衡。")


def _copy_db(src: Path, dst: Path) -> None:
    """用 sqlite backup 做一致快照（含 WAL 内容），避免副本库读到半成品。"""
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(str(src)) as csrc, sqlite3.connect(str(dst)) as cdst:
        csrc.backup(cdst)


def main() -> None:
    copy_arg = None
    if "--copy" in sys.argv:
        copy_arg = Path(sys.argv[sys.argv.index("--copy") + 1])
    live_dry_run(LIVE)
    copy_path = copy_arg or (REPO / "_opc_close_copy.db")
    _copy_db(LIVE, copy_path)
    full_close_on_copy(copy_path)


if __name__ == "__main__":
    main()

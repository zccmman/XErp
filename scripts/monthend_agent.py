"""关账 Agent 单次执行 CLI（P3-01）。

调度归外部（cron / Windows 任务计划 / WorkBuddy automation），本脚本只负责
「被拉起时执行一次」。示例（每天 18:00 检查，月末关账日手动触发正式执行）：

    # 每日 dry-run（只检查+催办，不动账）
    python scripts/monthend_agent.py --year 2026 --month 9 --dry-run

    # 月末正式关账（有未审凭证会中止并发催办）
    python scripts/monthend_agent.py --year 2026 --month 9

退出码：0 成功 / 1 业务错误（PENDING_VOUCHERS 等）/ 2 异常。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "mcp-server"))

import os  # noqa: E402

from sqlalchemy import create_engine, select  # noqa: E402

from kernel.db.models import Subject  # noqa: E402
from kernel.monthend import MonthendError, run_monthend  # noqa: E402


def _get_engine():
    url = (
        os.environ.get("XERP_DB")
        or os.environ.get("LEDGEROS_DB")
        or f"sqlite:///{REPO / 'ledgeros_dev.db'}"
    )
    return create_engine(url)


def main() -> int:
    parser = argparse.ArgumentParser(description="XErp 月度关账 Agent（单次执行）")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument("--ledger-set", default=None,
                        help="账套 id；缺省取账套列表第一个")
    parser.add_argument("--actor", default=None, help="执行主体 display_name")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查+催办，不动账")
    args = parser.parse_args()

    from sqlalchemy.orm import Session

    engine = _get_engine()
    with Session(engine) as s:
        if args.ledger_set:
            ls_id = args.ledger_set
        else:
            from kernel.db.models import LedgerSet

            ls = s.scalars(select(LedgerSet)).first()
            if ls is None:
                print("错误：数据库中没有任何账套", file=sys.stderr)
                return 2
            ls_id = ls.id
        name = args.actor or "关账Agent"
        subject = s.scalars(
            select(Subject).where(Subject.display_name == name)
        ).first()
        if subject is None:
            subject = s.scalars(select(Subject)).first()
        actor = {"type": "user", "id": subject.id}

        try:
            report = run_monthend(
                s, ledger_set_id=ls_id, year=args.year, month=args.month,
                actor=actor, dry_run=args.dry_run,
            )
        except MonthendError as e:
            print(f"[关账中止] {e.code}: {e.message_zh}", file=sys.stderr)
            if e.details:
                print(json.dumps(e.details, ensure_ascii=False, indent=2),
                      file=sys.stderr)
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""审计回放 CLI（P0-14）：事件流导出(JSONL) 与链完整性校验报告。

用法：
    python -m kernel.audit export --ledger-set LS [--out events.jsonl]
    python -m kernel.audit verify --ledger-set LS

退出码协议：0=链完整；2=链异常；1=参数/环境错误。
数据库来源：--db 或 LEDGEROS_DB 环境变量，默认仓库根 ledgeros_dev.db。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select  # noqa: E402  供 cmd_* 使用

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _db_url(args) -> str:
    return args.db or os.environ.get("LEDGEROS_DB") or f"sqlite:///{_REPO_ROOT / 'ledgeros_dev.db'}"


def cmd_export(session, args) -> int:
    from kernel.db.models import Event

    events = session.scalars(
        select(Event)
        .where(Event.ledger_set_id == args.ledger_set)
        .order_by(Event.id)
    ).all()
    if args.out:
        fh = open(args.out, "w", encoding="utf-8")
        close = True
    else:
        fh = sys.stdout
        close = False
    try:
        for e in events:
            rec = {
                "id": e.id,
                "ledger_set_id": e.ledger_set_id,
                "event_type": e.event_type,
                "aggregate_id": e.aggregate_id,
                "payload": e.payload,
                "actor": e.actor,
                "occurred_at": e.occurred_at.isoformat(),
                "prev_hash": e.prev_hash,
                "hash": e.hash,
            }
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if close:
            fh.close()
    print(f"[export] {args.ledger_set}: {len(events)} 条事件 → {args.out or 'stdout'}",
          file=sys.stderr)
    return 0


def cmd_verify(session, args) -> int:
    from kernel.ledger import verify_chain
    from kernel.db.models import Event

    count = len(
        session.scalars(
            select(Event.id).where(Event.ledger_set_id == args.ledger_set)
        ).all()
    )
    ok, problem = verify_chain(session, args.ledger_set)
    report = {
        "ledger_set": args.ledger_set,
        "events": count,
        "chain_ok": ok,
        "problem": problem,
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kernel.audit", description="LedgerOS 审计回放 CLI")
    parser.add_argument("--db", default=None, help="SQLAlchemy URL；缺省读 LEDGEROS_DB / 仓库演示库")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="按账套导出事件流 JSONL")
    p_exp.add_argument("--ledger-set", required=True)
    p_exp.add_argument("--out", default=None, help="输出文件路径（缺省打到 stdout）")

    p_ver = sub.add_parser("verify", help="校验账套事件哈希链完整性")
    p_ver.add_argument("--ledger-set", required=True)

    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from kernel.db.base import Base
    from kernel.db.models import Event  # noqa: F401 注册模型

    engine = create_engine(_db_url(args))
    with Session(engine) as session:
        Base.metadata.create_all(engine)  # 只读语义下兜底建表，避免空库报错困惑
        if args.cmd == "export":
            return cmd_export(session, args)
        if args.cmd == "verify":
            return cmd_verify(session, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

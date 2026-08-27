"""初始化演示库：建表 + 演示账套 + 144 科目模板 + 制单/审批双身份。

用法（managed venv）:
    python scripts/init_demo.py            # 默认 sqlite:///<repo>/ledgeros_dev.db
    LEDGEROS_DB=sqlite:///... python scripts/init_demo.py
产出: deploy/demo_state.json（供人工核对；运行时以 get_workspace 工具为准）
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Subject  # noqa: E402
from kernel.seed import seed_demo_ledger  # noqa: E402


def main() -> None:
    url = os.environ.get("LEDGEROS_DB", f"sqlite:///{REPO / 'ledgeros_dev.db'}")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ids = seed_demo_ledger(s)
        stats = import_chart_of_accounts(s, ids["ledger_set_id"], load_template_rows())

        reviewer = s.scalars(
            select(Subject).where(Subject.display_name == "审批人")
        ).first()
        if reviewer is None:
            reviewer = Subject(type="user", display_name="审批人", autonomy_level=3)
            s.add(reviewer)
        s.commit()

        state = {
            "db_url": url,
            "ledger_set": {"id": ids["ledger_set_id"], "name": "演示账套"},
            "open_period": {"year": 2026, "month": 8},
            "actors": {
                "制单人(丞辰)": ids["subject_id"],
                "审批人": reviewer.id,
            },
            "coa_created": stats["created"],
        }
        out = REPO / "deploy" / "demo_state.json"
        out.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

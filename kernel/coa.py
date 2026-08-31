"""科目表（COA）导入工具 — 小企业会计准则内置模板 + 通用 CSV 导入。

规则（ADR-001/003 精神）：
- 编码 4 位为一级、6 位为二级，子科目编码 = 父编码 + 2 位（前缀断裂即拒绝）
- 类别与前缀强一致：1*asset / 2*liability / 3*equity / 5*cost / 6*pnl
- 幂等：同账套同编码已存在则跳过
- 往来明细不建子科目，用 aux_dims 辅助维度承载（Ontology 设计核心）
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from kernel.db.models import Account

_CATEGORY_BY_PREFIX = {"1": "asset", "2": "liability", "3": "equity", "5": "cost", "6": "pnl"}
_EXPECT_DIR = {
    "asset": {"debit", "credit"},  # 备抵类科目的正常余额方向为 credit
    "liability": {"credit"},
    "equity": {"credit"},
    "cost": {"debit"},
    "pnl": {"debit", "credit"},  # 收入 credit / 费用 debit
}


class CoaImportError(ValueError):
    """科目表导入失败：信息可直接展示给用户。"""


def builtin_template_path() -> Path:
    return Path(__file__).parent / "data" / "coa_small_business.csv"


def load_template_rows() -> list[dict[str, str]]:
    with builtin_template_path().open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _parse_dims(raw: str | None) -> list[str]:
    """aux_dims 格式：竖线分隔的维度名，如 `department|project`；空即无维度。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    dims = [d.strip() for d in raw.split("|") if d.strip()]
    if not all(d.isidentifier() for d in dims):
        raise CoaImportError(f"aux_dims 含非法维度名: {raw!r}")
    return dims


def import_chart_of_accounts(
    session: Session,
    ledger_set_id: str,
    rows: TextIO | list[dict[str, str]],
) -> dict[str, int]:
    """将科目行导入指定账套。幂等；返回 {created, skipped}。"""
    if isinstance(rows, TextIO) or hasattr(rows, "read"):
        reader: list[dict[str, str]] = list(csv.DictReader(rows))
    else:
        reader = rows

    if not reader:
        raise CoaImportError("科目表为空")

    seen_in_file: set[str] = set()
    parsed: list[tuple[dict, str | None]] = []
    for r in reader:
        code = (r.get("code") or "").strip()
        name = (r.get("name") or "").strip()
        direction = (r.get("direction") or "").strip()
        category = (r.get("category") or "").strip()
        parent_code = (r.get("parent_code") or "").strip() or None

        if not code.isdigit() or len(code) not in (4, 6):
            raise CoaImportError(f"非法科目编码: {code!r}（须为 4 位或 6 位数字）")
        if not name:
            raise CoaImportError(f"{code}: 科目名称缺失")
        if code in seen_in_file:
            raise CoaImportError(f"文件内重复编码: {code}")
        seen_in_file.add(code)

        expected_cat = _CATEGORY_BY_PREFIX[code[0]]
        if category != expected_cat:
            raise CoaImportError(
                f"{code}: 类别 {category} 与编码前缀期望 {expected_cat} 不符"
            )
        if direction not in _EXPECT_DIR[category]:
            raise CoaImportError(f"{code}: 方向 {direction!r} 对类别 {category} 非法")

        if len(code) == 4:
            parent_code = None
        else:
            p = code[:-2]
            if parent_code is None:
                parent_code = p  # 未填则按前缀自动推断
            elif parent_code != p:
                raise CoaImportError(f"{code}: parent_code={parent_code} 与前缀 {p} 断裂")
        parsed.append((r, parent_code))

    # 数据库内既有科目（幂等跳过依据）
    existing = {
        a.code: a
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        )
    }
    created = skipped = 0
    pending_parent: list[tuple[Account, str]] = []
    for r, parent_code in parsed:
        code = r["code"]
        if code in existing:
            skipped += 1
            # 幂等不再等于"完全跳过"：模板是科目表的权威定义，
            # 旧种子（如 seed_demo_ledger）建的科目可能缺辅助维度声明；
            # 只补维度，不动名称/方向等其余字段。
            acc0 = existing[code]
            want_dims = _parse_dims(r.get("aux_dims"))
            if want_dims and not acc0.aux_dim_defs:
                acc0.aux_dim_defs = want_dims
            continue
        if parent_code and parent_code not in existing:
            raise CoaImportError(f"{code}: 父科目 {parent_code} 不存在（须先定义父级）")
        acc = Account(
            ledger_set_id=ledger_set_id,
            code=code,
            name=r["name"],
            direction=r["direction"],
            category=r["category"],
            aux_dim_defs=_parse_dims(r.get("aux_dims")),
        )
        session.add(acc)
        existing[code] = acc
        if parent_code:
            pending_parent.append((acc, parent_code))
        created += 1
    session.flush()

    # 父子用关系对象赋值（同 flush 单元内新对象尚无 id，不能直接引用外键值）
    for acc, parent_code in pending_parent:
        acc.parent = existing[parent_code]
    session.flush()

    # 叶子标记：某编码被任何科目作父 → 非叶子
    parents_in_use = {
        a.parent_id
        for a in session.scalars(
            select(Account).where(Account.ledger_set_id == ledger_set_id)
        )
        if a.parent_id
    }
    for a in session.scalars(
        select(Account).where(Account.ledger_set_id == ledger_set_id)
    ):
        a.is_leaf = a.id not in parents_in_use
    session.flush()

    return {"created": created, "skipped": skipped}

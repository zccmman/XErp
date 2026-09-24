"""WorkBuddy「项目即账套 + Copilot 即专家」可复制模式（Phase 1 + Phase 2 落地）。

一个 WorkBuddy 项目 = 一个 XErp 账套：
- 账库 DB 物理落在项目工作区 ``<project_dir>/xerp/ledger.db`` —— 复制项目目录 = 复制账套（数据自持）。
- ``<project_dir>/xerp.project.json`` 记录 项目↔账套 映射（ledger_set_id / 双身份 / 科目统计）。
- 零 WorkBuddy 依赖：本模块只 import kernel + sqlalchemy。WB 升级、MCP 断连、技能下架均不影响
  （L0 内核直连逃生舱，见本目录 README 的分层韧性）。
- HITL 红线：本模块只做「建账 + 只读问答 + 自检」，绝不触碰 push/approve/post/close 终态。

CLI（managed venv，任意工作目录可用）::

    python integration/workbuddy/xerp_project.py init   <project_dir> [--name 我的账套] [--owner 老板]
    python integration/workbuddy/xerp_project.py ask    <project_dir> "这个月经营情况如何"
    python integration/workbuddy/xerp_project.py doctor <project_dir>

新电脑快速复制验证：克隆本仓库 → 上面三条命令 → doctor 全 [PASS] 即可开工。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from kernel.approval import bind_subject_external_ref  # noqa: E402
from kernel.authz import grant_ledger_role  # noqa: E402
from kernel.coa import import_chart_of_accounts, load_template_rows  # noqa: E402
from kernel.copilot import ask as copilot_ask  # noqa: E402
from kernel.db.base import Base  # noqa: E402
from kernel.db.models import Account, LedgerSet, Period, Subject, Voucher  # noqa: E402

MANIFEST_NAME = "xerp.project.json"
DB_REL = "xerp/ledger.db"
SCHEMA_VERSION = 1
REVIEWER_NAME = "审批人"

__all__ = [
    "MANIFEST_NAME",
    "DB_REL",
    "init_project",
    "load_manifest",
    "open_session",
    "ask_project",
    "doctor",
    "main",
]


def _sqlite_url(db_file: Path) -> str:
    """sqlite URL 统一正斜杠（Windows 反斜杠会破坏 sqlalchemy URL）。"""
    return "sqlite:///" + str(db_file.resolve()).replace("\\", "/")


def manifest_path(project_dir: str | Path) -> Path:
    return Path(project_dir).expanduser().resolve() / MANIFEST_NAME


def db_file(project_dir: str | Path) -> Path:
    return Path(project_dir).expanduser().resolve() / DB_REL


def load_manifest(project_dir: str | Path) -> dict:
    mp = manifest_path(project_dir)
    if not mp.exists():
        raise FileNotFoundError(f"未找到 {mp}；请先运行 init 子命令")
    return json.loads(mp.read_text(encoding="utf-8"))


def init_project(
    project_dir: str | Path,
    *,
    ledger_name: str = "我的账套",
    owner_name: str = "老板",
    accounting_standard: str = "small_business",
    owner_ref: str = "",
    reviewer_ref: str = "",
) -> dict:
    """把一个目录初始化为「XErp 账套项目」。幂等：重复调用直接 replayed=true。

    产出：``<project_dir>/xerp/ledger.db`` + ``<project_dir>/xerp.project.json``。
    双身份（老板 admin / 审批人 accountant+reviewer）+ 144 科目模板 + 当期 OPEN 期间。
    """
    p = Path(project_dir).expanduser().resolve()
    (p / "xerp").mkdir(parents=True, exist_ok=True)
    mp = manifest_path(p)

    if mp.exists():
        m = json.loads(mp.read_text(encoding="utf-8"))
        try:
            with Session(create_engine(_sqlite_url(p / m["db"]))) as s:
                if s.get(LedgerSet, m["ledger_set_id"]) is not None:
                    m["replayed"] = True
                    return m
        except Exception:  # manifest 在但库损坏 → 落到下面重建
            pass

    engine = create_engine(_sqlite_url(p / DB_REL))
    Base.metadata.create_all(engine)
    today = date.today()
    with Session(engine) as s:
        ls = s.scalars(select(LedgerSet).where(LedgerSet.name == ledger_name)).first()
        if ls is None:
            ls = LedgerSet(name=ledger_name, accounting_standard=accounting_standard)
            s.add(ls)
            s.flush()
        stats = import_chart_of_accounts(s, ls.id, load_template_rows())

        period = s.scalars(
            select(Period).where(
                Period.ledger_set_id == ls.id,
                Period.year == today.year,
                Period.month == today.month,
            )
        ).first()
        if period is None:
            s.add(Period(ledger_set_id=ls.id, year=today.year, month=today.month, status="OPEN"))
            s.flush()

        owner = s.scalars(
            select(Subject).where(Subject.display_name == owner_name, Subject.type == "user")
        ).first()
        if owner is None:
            owner = Subject(type="user", display_name=owner_name, autonomy_level=3)
            s.add(owner)
            s.flush()
        reviewer = s.scalars(
            select(Subject).where(Subject.display_name == REVIEWER_NAME, Subject.type == "user")
        ).first()
        if reviewer is None:
            reviewer = Subject(type="user", display_name=REVIEWER_NAME, autonomy_level=3)
            s.add(reviewer)
            s.flush()

        s.commit()  # SQLite：先落盘主体再授权（防 casbin 自锁）
        grant_ledger_role(s, ledger_set_id=ls.id, subject_id=owner.id, role="admin")
        grant_ledger_role(s, ledger_set_id=ls.id, subject_id=reviewer.id, role="accountant")
        grant_ledger_role(s, ledger_set_id=ls.id, subject_id=reviewer.id, role="reviewer")
        # WB 原生审批闭环（P0-3）：可选绑定外部身份键（WB user id / 飞书 open_id 等）
        if owner_ref:
            bind_subject_external_ref(s, subject_id=owner.id, external_ref=owner_ref)
        if reviewer_ref:
            bind_subject_external_ref(s, subject_id=reviewer.id, external_ref=reviewer_ref)
        # commit 会 expire 属性；会话关闭前捕获 id，避免 DetachedInstanceError
        s.commit()
        ls_id, owner_id, reviewer_id = ls.id, owner.id, reviewer.id

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "ledger_name": ledger_name,
        "ledger_set_id": ls_id,
        "owner_subject_id": owner_id,
        "reviewer_subject_id": reviewer_id,
        "accounting_standard": accounting_standard,
        "db": DB_REL,
        "accounts": stats,
        "replayed": False,
    }
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


@contextmanager
def open_session(project_dir: str | Path) -> Iterator[Session]:
    """按项目清单打开该账套的只读/事务会话（commit/rollback 自动管理）。"""
    m = load_manifest(project_dir)
    p = Path(project_dir).expanduser().resolve()
    engine = create_engine(_sqlite_url(p / m["db"]))
    s = Session(engine)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ask_project(
    project_dir: str | Path,
    question_zh: str,
    as_of_date: date | None = None,
) -> dict:
    """Copilot 即专家（Phase 2）：项目目录内直接只读问答，零 MCP 依赖。

    返回 {answer_zh, intent, tool_calls, evidence, followups, severity}；绝不写账。
    """
    m = load_manifest(project_dir)
    with open_session(project_dir) as s:
        return copilot_ask(
            s, ledger_set_id=m["ledger_set_id"], question_zh=question_zh, as_of_date=as_of_date
        )


def doctor(project_dir: str | Path) -> dict:
    """自检：manifest → DB → 账套/科目/期间/双身份 → Copilot 只读冒烟 → 韧性红线。

    全 [PASS] 才算「复制到新电脑成功」。
    """
    p = Path(project_dir).expanduser().resolve()
    checks: list[dict] = []

    def _add(name: str, ok: object, detail: object = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})

    try:
        m = load_manifest(p)
        _add("01_manifest", True, f"账套={m.get('ledger_name')} db={m.get('db')}")
    except Exception as exc:
        _add("01_manifest", False, exc)
        return {"project_dir": str(p), "all_ok": False, "checks": checks}

    dbf = p / m.get("db", DB_REL)
    _add("02_db_file", dbf.exists(), dbf)

    try:
        with open_session(p) as s:
            ls = s.get(LedgerSet, m["ledger_set_id"])
            _add("03_ledger_set", ls is not None, ls.name if ls else "账套缺失")
            if ls is not None:
                n_acc = len(
                    s.scalars(select(Account).where(Account.ledger_set_id == ls.id)).all()
                )
                _add("04_chart_of_accounts", n_acc > 0, f"{n_acc} 个科目")

                today = date.today()
                period = s.scalars(
                    select(Period).where(
                        Period.ledger_set_id == ls.id,
                        Period.year == today.year,
                        Period.month == today.month,
                    )
                ).first()
                _add(
                    "05_open_period",
                    period is not None and period.status == "OPEN",
                    f"{today.year}-{today.month:02d}"
                    + ("" if period is None else f" status={period.status}"),
                )

                owner = s.get(Subject, m.get("owner_subject_id", ""))
                reviewer = s.get(Subject, m.get("reviewer_subject_id", ""))
                _add(
                    "06_dual_identity",
                    owner is not None and reviewer is not None,
                    f"{owner.display_name if owner else '缺老板'}/"
                    f"{reviewer.display_name if reviewer else '缺审批人'}",
                )

                # 只读冒烟：ask 前后凭证数必须不变（HITL/只读铁律的可执行证明）
                n_before = len(
                    s.scalars(select(Voucher).where(Voucher.ledger_set_id == ls.id)).all()
                )
                res = copilot_ask(s, ledger_set_id=ls.id, question_zh="总体经营情况如何")
                n_after = len(
                    s.scalars(select(Voucher).where(Voucher.ledger_set_id == ls.id)).all()
                )
                s.rollback()
                _add(
                    "07_copilot_smoke",
                    bool(res.get("answer_zh")) and bool(res.get("intent")),
                    f"intent={res.get('intent')} severity={res.get('severity')}",
                )
                _add("08_readonly_guarantee", n_before == n_after, f"凭证数 {n_before}→{n_after}")
    except Exception as exc:
        _add("09_session", False, f"会话/冒烟异常: {exc}")

    # 韧性红线：本模块零宿主依赖（WB 升级/断连也拿不走的能力）
    src = Path(__file__).read_text(encoding="utf-8")
    leaked = re.findall(r"^\s*(?:from|import)\s+workbuddy\b", src, flags=re.MULTILINE)
    _add(
        "10_no_workbuddy_import",
        not leaked,
        "零 WorkBuddy 依赖，L0 逃生舱成立" if not leaked else f"发现宿主依赖: {leaked}",
    )

    return {"project_dir": str(p), "all_ok": all(c["ok"] for c in checks), "checks": checks}


def _print_doctor(result: dict) -> None:
    for c in result["checks"]:
        mark = "[PASS]" if c["ok"] else "[FAIL]"
        print(f"{mark} {c['check']}: {c['detail']}")
    print("==> " + ("全部通过，项目即账套就绪" if result["all_ok"] else "存在失败项，请按上方明细排查"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="XErp × WorkBuddy：项目即账套")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_init = sub.add_parser("init", help="在项目目录初始化账套（幂等）")
    ap_init.add_argument("project_dir")
    ap_init.add_argument("--name", default="我的账套")
    ap_init.add_argument("--owner", default="老板")
    ap_init.add_argument("--standard", default="small_business")
    ap_init.add_argument("--owner-ref", default="", help="老板外部身份键（WB user id 等）")
    ap_init.add_argument("--reviewer-ref", default="", help="审批人外部身份键")

    ap_ask = sub.add_parser("ask", help="只读问答（确定性 Copilot，零 MCP 依赖）")
    ap_ask.add_argument("project_dir")
    ap_ask.add_argument("question", nargs="+")

    ap_doc = sub.add_parser("doctor", help="自检（全 PASS 才算复制成功）")
    ap_doc.add_argument("project_dir")

    args = ap.parse_args(argv)

    if args.cmd == "init":
        m = init_project(
            args.project_dir,
            ledger_name=args.name,
            owner_name=args.owner,
            accounting_standard=args.standard,
            owner_ref=args.owner_ref,
            reviewer_ref=args.reviewer_ref,
        )
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "ask":
        res = ask_project(args.project_dir, " ".join(args.question))
        print(res.get("answer_zh", ""))
        print(f"\n[intent] {res.get('intent')}  [severity] {res.get('severity')}")
        for tc in res.get("tool_calls", []) or []:
            print(f"  <- {tc}")
        return 0
    if args.cmd == "doctor":
        result = doctor(args.project_dir)
        _print_doctor(result)
        return 0 if result["all_ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""钉死「WorkBuddy 项目即账套」可复制模式。

覆盖：幂等建账 / 清单落盘 / 只读问答路由 / doctor 全绿 / 零宿主依赖红线（L0 逃生舱）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integration.workbuddy.xerp_project import ask_project, doctor, init_project

_MODULE = Path(__file__).resolve().parents[1] / "integration" / "workbuddy" / "xerp_project.py"


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def test_init_creates_db_manifest_coa_period(project_dir: Path) -> None:
    m = init_project(project_dir, ledger_name="测试账套", owner_name="张三")
    assert m["replayed"] is False
    assert m["ledger_name"] == "测试账套"
    assert (project_dir / "xerp" / "ledger.db").exists()
    on_disk = json.loads((project_dir / "xerp.project.json").read_text(encoding="utf-8"))
    assert on_disk["ledger_set_id"] == m["ledger_set_id"]
    assert on_disk["owner_subject_id"] and on_disk["reviewer_subject_id"]
    assert on_disk["accounts"]["created"] > 0  # 144 科目模板已导入


def test_init_idempotent(project_dir: Path) -> None:
    m1 = init_project(project_dir, ledger_name="测试账套", owner_name="张三")
    m2 = init_project(project_dir, ledger_name="测试账套", owner_name="张三")
    assert m2["replayed"] is True
    assert m2["ledger_set_id"] == m1["ledger_set_id"]


def test_ask_readonly_and_routed(project_dir: Path) -> None:
    init_project(project_dir)
    res = ask_project(project_dir, "总体经营情况如何")
    assert res.get("intent")
    assert res.get("answer_zh")
    assert isinstance(res.get("tool_calls"), list)


def test_doctor_all_green(project_dir: Path) -> None:
    init_project(project_dir)
    result = doctor(project_dir)
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["all_ok"], failed


def test_doctor_without_init_fails_clean(project_dir: Path) -> None:
    result = doctor(project_dir)
    assert result["all_ok"] is False
    assert result["checks"][0]["check"] == "01_manifest"


def test_no_workbuddy_dependency() -> None:
    """L0 韧性红线：集成层零宿主依赖，也不得绕进 adapters（A1 同款方向约束）。"""
    src = _MODULE.read_text(encoding="utf-8")
    assert "from workbuddy" not in src and "import workbuddy" not in src
    assert "from kernel.adapters" not in src

"""P0-14 TDD：审计回放 CLI —— export(JSONL) / verify(退出码协议) / stdio 握手冒烟入 CI。

退出码协议：0=链完整，2=链异常（供脚本化判据）。
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kernel.db.base import Base
from kernel.ledger import append_event

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/audit.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for i in (1, 2, 3):
            append_event(
                s,
                ledger_set_id="LS",
                event_type="voucher.created",
                aggregate_id=f"v-{i}",
                payload={"n": i},
                actor={"type": "user", "id": "u1"},
            )
        s.commit()
    engine.dispose()
    return url


def _run(db_url, *args):
    env = {**os.environ, "LEDGEROS_DB": db_url}
    return subprocess.run(
        [sys.executable, "-m", "kernel.audit", *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_export_writes_jsonl(db_url, tmp_path):
    out = tmp_path / "events.jsonl"
    r = _run(db_url, "export", "--ledger-set", "LS", "--out", str(out))
    assert r.returncode == 0, r.stderr
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert [x["aggregate_id"] for x in lines] == ["v-1", "v-2", "v-3"]
    assert all(len(x["hash"]) == 64 for x in lines)


def test_verify_exit_protocol(db_url):
    ok = _run(db_url, "verify", "--ledger-set", "LS")
    body = json.loads(ok.stdout[ok.stdout.index("{"):])
    assert ok.returncode == 0 and body["chain_ok"] is True


def test_verify_detects_tamper(db_url):
    # 模拟攻击者绕过应用直改 DB
    engine = create_engine(db_url)
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("UPDATE events SET payload='2' WHERE id=2"))
    engine.dispose()
    r = _run(db_url, "verify", "--ledger-set", "LS")
    assert r.returncode == 2
    data = json.loads(r.stdout[r.stdout.index("{"):])
    assert data["chain_ok"] is False and data["problem"]["event_id"] == 2


def test_stdio_handshake_smoke():
    """制度化的启动冒烟：按客户端方式直跑 server.py 脚本并管道握手。

    防 P0-07/-32000 类回归（路径自举断裂只在进程级启动时暴露）。
    """
    d = tempfile.mkdtemp()
    env = {
        **os.environ,
        "LEDGEROS_DB": f"sqlite:///{d}/smoke.db",
        "PYTHONIOENCODING": "utf-8",
    }
    init_req = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ci-smoke", "version": "0"},
            },
        }
    )
    r = subprocess.run(
        [
            sys.executable,
            str(REPO / "mcp-server" / "ledgeros_mcp" / "server.py"),
        ],
        input=init_req + "\n",
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(REPO),
        env=env,
    )
    assert '"LedgerOS"' in r.stdout, (
        f"握手失败\nstdout={r.stdout[:400]}\nstderr={r.stderr[:800]}"
    )

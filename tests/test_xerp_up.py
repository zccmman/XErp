"""xerp up / stop 一键起编排测试（O13 易部署）。

覆盖：Web 探头、.env 读取、端口解析，以及真实拉起 dev 库 Web + doctor 自检。
集成部分需要 ledgeros/.venv（含 uvicorn），缺失时自动跳过。
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent              # ledgeros/
CUSTOMER_PACK = REPO.parent / "xerp-customer-pack"
sys.path.insert(0, str(CUSTOMER_PACK))
import install as installer                                # noqa: E402

VENV = REPO / ".venv" / "Scripts" / "python.exe"
HAS_VENV = VENV.exists()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_web_down():
    # 一个刚分配的空闲端口，必然无人监听
    assert installer.probe_web(_free_port(), timeout=2) is False


def test_read_env_value(tmp_path):
    f = tmp_path / ".env"
    f.write_text("XERP_DB=sqlite:///x.db\nXERP_WEB_PASSWORD=abc\n# c\n", encoding="utf-8")
    assert installer.read_env_value(f, "XERP_WEB_PASSWORD") == "abc"
    assert installer.read_env_value(f, "XERP_DB") == "sqlite:///x.db"
    assert installer.read_env_value(f, "MISSING") is None


def test_web_port(tmp_path):
    f = tmp_path / ".env"
    f.write_text("PORT=9000\n", encoding="utf-8")
    assert installer.web_port(f, 0) == 9000
    assert installer.web_port(f, 7777) == 7777
    assert installer.web_port(tmp_path / "nope.env", 0) == 8001


@pytest.mark.skipif(not HAS_VENV, reason="需要 ledgeros/.venv 才能起 Web")
def test_launch_web_and_doctor():
    port = _free_port()
    db_url = "sqlite:///" + str((REPO / "ledgeros_dev.db").resolve())
    try:
        ok = installer.launch_web(REPO, str(VENV), db_url, REPO / ".env", port)
        assert ok is True
        assert installer.probe_web(port, timeout=3) is True

        # up 的终态：doctor 自检全绿
        dr = subprocess.run(
            [str(VENV), str(CUSTOMER_PACK / "tools" / "xerp_doctor.py"),
             "--source", str(REPO), "--db", db_url],
            capture_output=True, text=True, timeout=180,
        )
        assert dr.returncode == 0, dr.stdout + dr.stderr
    finally:
        pid_file = REPO / "xerp_web.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                                   capture_output=True)
                else:
                    os.kill(pid, 9)
            except Exception:
                pass
            pid_file.unlink(missing_ok=True)

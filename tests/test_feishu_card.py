"""P0-11：审批卡片构建器单测（离线，不打网络）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp-server"))

from ledgeros_mcp.feishu import build_approval_card, load_env


def test_card_structure():
    card = build_approval_card(
        voucher_no="记-0001",
        status="PUSHED",
        summary="报销招待费",
        lines=[
            {"account_code": "660204", "account_name": "业务招待费", "debit": "800.00", "credit": "0.00"},
            {"account_code": "1001", "account_name": "库存现金", "debit": "0.00", "credit": "800.00"},
        ],
        voucher_id="v-123",
    )
    assert card["header"]["title"]["content"].startswith("LedgerOS 审批请求 · 记-0001")
    actions = card["elements"][-1]["actions"]
    assert [a["value"]["action"] for a in actions] == ["approve", "reject"]
    assert all(a["value"]["voucher_id"] == "v-123" for a in actions)
    body = card["elements"][3]["text"]["content"]
    assert "660204" in body and "800.00" in body and "1001" in body


def test_env_loader_reads_keys():
    env = load_env()
    assert env.get("FEISHU_APP_ID", "").startswith("cli_")

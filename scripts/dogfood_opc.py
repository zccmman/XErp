"""P0-15 真实 dogfood：OPC（一人公司）2026-08 十笔真实业务录入脚本。

走完整工具链：init_ledger_set → import_opening_balances →
create_voucher → push_voucher → approve_voucher(换人审批) → post_voucher。
用法: python scripts/dogfood_opc.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "mcp-server"))

from fastmcp import Client  # noqa: E402
from ledgeros_mcp.server import build_server  # noqa: E402

DB = f"sqlite:///{REPO / 'ledgeros_dev.db'}"

# ---- 期初余额（2026-08-01） 借 170,000 = 贷 170,000 ----
OPENING = [
    {"account_code": "100201", "debit": "120000.00", "credit": ""},
    {"account_code": "1001", "debit": "5000.00", "credit": ""},
    {"account_code": "1122", "debit": "30000.00", "credit": ""},
    {"account_code": "160103", "debit": "15000.00", "credit": ""},
    {"account_code": "3001", "debit": "", "credit": "100000.00"},
    {"account_code": "2202", "debit": "", "credit": "15000.00"},
    {"account_code": "3103", "debit": "", "credit": "55000.00"},
]

# ---- 十笔业务（小规模纳税人，征收率 1%，价税分离） ----
TXNS = [
    ("2026-08-02", "线上AI课程收入到账（微信/支付宝归集）", [
        ("100201", "19800.00", ""),
        ("6001", "", "19603.96"),
        ("222101", "", "196.04"),
    ]),
    ("2026-08-04", "支付云服务器年费（阿里云）", [
        ("660202", "3600.00", ""),
        ("100201", "", "3600.00"),
    ]),
    ("2026-08-06", "报销客户拜访差旅（高铁+住宿，现金支付）", [
        ("660203", "2480.00", ""),
        ("1001", "", "2480.00"),
    ]),
    ("2026-08-08", "公众号流量主收入到账", [
        ("100201", "1200.00", ""),
        ("6051", "", "1188.12"),
        ("222101", "", "11.88"),
    ]),
    ("2026-08-11", "购入笔记本（电子设备，一次性计入固定资产）", [
        ("160103", "8999.00", ""),
        ("100201", "", "8999.00"),
    ]),
    ("2026-08-13", "客户回款冲销应收账款", [
        ("100201", "20000.00", ""),
        ("1122", "", "20000.00"),
    ]),
    ("2026-08-15", "外包课程剪辑服务费（主营业务成本）", [
        ("6401", "4000.00", ""),
        ("100201", "", "4000.00"),
    ]),
    ("2026-08-18", "数字产品销售收入（闲鱼/小红书）", [
        ("100201", "6000.00", ""),
        ("6001", "", "5940.59"),
        ("222101", "", "59.41"),
    ]),
    ("2026-08-21", "小红书/公众号推广投放", [
        ("660102", "1500.00", ""),
        ("100201", "", "1500.00"),
    ]),
    ("2026-08-25", "计提并发放 8 月工资（一人公司，简化直发）", [
        ("660201", "8000.00", ""),
        ("100201", "", "8000.00"),
    ]),
]


async def main() -> None:
    server = build_server(DB)
    async with Client(server) as c:
        async def call(tool, **args):
            res = await c.call_tool(tool, args)
            if getattr(res, "data", None) is not None:
                return res.data
            return json.loads(res.content[0].text)

        # 1) 建账套
        r = await call("init_ledger_set", name="OPC 一人公司", owner_name="丞辰")
        assert r["ok"], r
        ls = r["ledger_set_id"]
        owner = r["owner_subject_id"]
        print(f"[建账] OPC 一人公司  ledger_set_id={ls}")

        # 2) 建立审批人（换人审批，铁律）
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from kernel.db.models import Subject

        eng = create_engine(DB)
        with Session(eng) as s:
            rev = s.scalars(
                select(Subject).where(Subject.display_name == "代账会计-李会计")
            ).first()
            if rev is None:
                rev = Subject(type="user", display_name="代账会计-李会计", autonomy_level=3)
                s.add(rev)
                s.commit()
            reviewer = rev.id
        print(f"[身份] 制单人={owner[:8]}…  审批人=代账会计-李会计({reviewer[:8]}…)")

        # 3) 期初
        r = await call("import_opening_balances", ledger_set_id=ls,
                       actor_id=owner, lines=OPENING)
        assert r["ok"], r
        print(f"[期初] {r['voucher']['voucher_no']} 导入成功（试算平衡）")

        # 4) 十笔业务
        results = []
        for i, (dt, summary, lines) in enumerate(TXNS, start=1):
            payload = [
                {"account_code": code, "debit": d, "credit": c} for code, d, c in lines
            ]
            cv = await call("create_voucher", ledger_set_id=ls, voucher_date=dt,
                            actor_id=owner, summary=summary, lines=payload)
            assert cv["ok"], cv
            vid = cv["voucher"]["id"]
            no = cv["voucher"]["voucher_no"]
            await call("push_voucher", voucher_id=vid, actor_id=owner)
            ap = await call("approve_voucher", voucher_id=vid, actor_id=reviewer)
            assert ap["ok"], ap
            po = await call("post_voucher", voucher_id=vid, actor_id=owner)
            assert po["ok"], po
            results.append((no, dt, summary, po["voucher"]["status"]))
            print(f"  {i:2d}. {no} {dt} {summary} → {po['voucher']['status']}")

        # 5) 余额回读（8 月）
        bal = await call("query_balances", ledger_set_id=ls, period_year=2026, period_month=8)
        print(f"\n[对账] 发生额投影共 {len(bal['balances'])} 行")
        out = REPO / "docs" / "dogfood_opc_balances.json"
        out.write_text(
            json.dumps(bal["balances"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[落盘] {out.relative_to(REPO)}")
        print(f"[完成] 十笔业务全部 POSTED；账套 id={ls}")


if __name__ == "__main__":
    asyncio.run(main())

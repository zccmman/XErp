"""报表科目映射配置（P1-01）。

两套准则模板：small_business（小企业会计准则）/ enterprise（企业会计准则）。
映射规则按**科目编码前缀**匹配，新增准则只需加一份配置，不改报表代码。

- balance_sheet: 大类 → 项目 → 科目前缀列表
- income_statement: 项目 → 科目前缀列表 + 取数方向（credit=贷方发生额为正）
- cash_flow: 对方科目前缀 → 现金流类别（经营/投资/筹资）
- cash_accounts: 现金及现金等价物科目前缀
"""

from __future__ import annotations

SMALL_BUSINESS = {
    "balance_sheet": {
        "资产": {
            "流动资产": ["1001", "1002", "1012", "1101", "1121", "1122", "1123",
                         "1131", "1221", "1231", "1403", "1405", "1408", "1471"],
            "非流动资产": ["1501", "1503", "1601", "1602", "1604", "1701", "1702",
                           "1801", "1901"],
        },
        "负债": {
            "流动负债": ["2001", "2101", "2201", "2202", "2203", "2211", "2221",
                         "2231", "2232", "2241"],
            "非流动负债": ["2501", "2701", "2711", "2801"],
        },
        "所有者权益": {
            "所有者权益": ["3001", "3002", "3101", "3103", "3104"],
        },
    },
    "income_statement": [
        ("营业收入", ["6001", "6051"], "credit"),
        ("营业成本", ["6401", "6402"], "debit"),
        ("税金及附加", ["6403"], "debit"),
        ("销售费用", ["6601"], "debit"),
        ("管理费用", ["6602"], "debit"),
        ("财务费用", ["6603"], "debit"),
        ("营业外收入", ["6301"], "credit"),
        ("营业外支出", ["6711"], "debit"),
    ],
    "closing": {"profit_account": "3103"},
    "cash_accounts": ["1001", "1002"],
    "cash_flow": [
        ("经营活动-流入", ["6001", "6051", "6301", "1121", "1122", "1123", "6051"], "in"),
        ("经营活动-流出", ["2201", "2202", "2211", "2221", "2231", "2232",
                           "6401", "6402", "6403", "6601", "6602", "6603",
                           "6711", "1221"], "out"),
        ("投资活动-流入", ["1601", "1602", "1701", "1501", "1503"], "in"),
        ("投资活动-流出", ["1601", "1602", "1701", "1501", "1503"], "out"),
        ("筹资活动-流入", ["3001", "3002", "2001", "2501"], "in"),
        ("筹资活动-流出", ["3001", "2001", "2501"], "out"),
    ],
}

# 企业会计准则：结构一致，新增研发费用/使用权资产等项目（示例扩展点）
ENTERPRISE = {
    "balance_sheet": {
        "资产": {
            "流动资产": SMALL_BUSINESS["balance_sheet"]["资产"]["流动资产"],
            "非流动资产": SMALL_BUSINESS["balance_sheet"]["资产"]["非流动资产"]
            + ["1703", "1801"],
        },
        "负债": dict(SMALL_BUSINESS["balance_sheet"]["负债"]),
        "所有者权益": dict(SMALL_BUSINESS["balance_sheet"]["所有者权益"]),
    },
    "income_statement": [
        ("营业收入", ["6001", "6051"], "credit"),
        ("营业成本", ["6401", "6402"], "debit"),
        ("税金及附加", ["6403"], "debit"),
        ("销售费用", ["6601"], "debit"),
        ("管理费用", ["6602"], "debit"),
        ("研发费用", ["6604"], "debit"),
        ("财务费用", ["6603"], "debit"),
        ("营业外收入", ["6301"], "credit"),
        ("营业外支出", ["6711"], "debit"),
    ],
    "closing": {"profit_account": "3103"},
    "cash_accounts": ["1001", "1002"],
    "cash_flow": SMALL_BUSINESS["cash_flow"],
}

MAPPINGS = {
    "small_business": SMALL_BUSINESS,
    "enterprise": ENTERPRISE,
}


def get_mapping(standard: str) -> dict:
    return MAPPINGS.get(standard, SMALL_BUSINESS)


def _match(code: str, prefixes: list[str]) -> bool:
    return any(code.startswith(p) for p in prefixes)


def balance_sheet_group(mapping: dict, code: str) -> tuple[str, str] | None:
    for major, groups in mapping["balance_sheet"].items():
        for group, prefixes in groups.items():
            if _match(code, prefixes):
                return major, group
    return None


def income_statement_item(mapping: dict, code: str) -> tuple[str, str] | None:
    """返回 (项目名, 取数方向)。"""
    for item, prefixes, side in mapping["income_statement"]:
        if _match(code, prefixes):
            return item, side
    return None


def cash_flow_bucket(mapping: dict, code: str) -> str | None:
    for label, prefixes, _direction in mapping["cash_flow"]:
        if _match(code, prefixes):
            return label
    return None


def is_cash_account(mapping: dict, code: str) -> bool:
    return _match(code, mapping["cash_accounts"])

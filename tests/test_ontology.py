"""阶段1本体底座 TDD：rules/relations 两表 + coa attrs 列 + ontology 聚合层。

铁律：文件是唯一真源；ontology.py 只读聚合；任何 code 必须先在 coa 模板中存在。
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kernel.coa import (
    CoaImportError,
    import_chart_of_accounts,
    load_template_rows,
    parse_attrs,
)
from kernel.db.base import Base
from kernel.db.models import Account, LedgerSet
from kernel.ontology import (
    OntologyError,
    check_lines,
    load_relations,
    load_rules,
    subject_semantics,
    template_attrs,
)

STD = "small_business"


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        ls = LedgerSet(name="本体测试账套")
        s.add(ls)
        s.flush()
        yield s, ls.id


# ---------- 真源完整性与交叉校验 ----------

def test_rules_load_and_scope_codes_exist():
    rules = load_rules(STD)
    assert rules, "规则表不能为空"
    template_codes = {r["code"] for r in load_template_rows()}
    for r in rules:
        assert r["rule_id"].startswith("R-"), r
        base = r["scope"].rstrip("*")
        assert base in template_codes, f"规则 {r['rule_id']} 的 scope {r['scope']} 不在模板科目中"
        assert r["severity"] in ("error", "recommend"), r
        assert r["message_zh"], r


def test_relations_codes_exist_and_rel_types_valid():
    rels = load_relations(STD)
    assert rels, "关系表不能为空"
    template_codes = {r["code"] for r in load_template_rows()}
    for r in rels:
        assert r["subj_code"] in template_codes, f"subj_code {r['subj_code']} 不在模板中"
        assert r["obj_code"] in template_codes, f"obj_code {r['obj_code']} 不在模板中"
        assert r["rel_type"] in ("contra_of", "reclass_pairs", "carryforward_from"), r
        assert r["note_zh"], r


def test_template_attrs_spot_checks():
    attrs = template_attrs(STD)
    assert attrs["1001"] == {"cash_flow": "yes"}
    assert attrs["1122"] == {"bad_debt": "eligible"}
    assert attrs["160101"] == {"depreciate": "yes"}
    assert attrs["170102"] == {"amortize": "yes"}
    assert attrs["660204"] == {"deduction": "catering60"}
    # ② 现金流项目语义：6001 主营业务收入声明式指定所属现金流项目（覆盖按对方科目前缀默认归类）
    assert attrs["6001"] == {"cash_flow_item": "销售商品、提供劳务收到的现金"}


def test_parse_attrs_format_guards():
    assert parse_attrs(None) == {}
    assert parse_attrs("") == {}
    assert parse_attrs("a=1; b=2") == {"a": "1", "b": "2"}
    with pytest.raises(CoaImportError):
        parse_attrs("缺等号")
    with pytest.raises(CoaImportError):
        parse_attrs("k=")
    with pytest.raises(CoaImportError):
        parse_attrs("=v")


def test_unknown_standard_rejected():
    with pytest.raises(OntologyError):
        load_rules("no_such_standard")
    with pytest.raises(OntologyError):
        template_attrs("ga_2024")


# ---------- subject_semantics 聚合 ----------

def test_subject_semantics_1122_aggregates_all_sources():
    sem = subject_semantics(STD, "1122")
    assert sem["attrs"] == {"bad_debt": "eligible"}
    rule_ids = {r["rule_id"] for r in sem["rules"]}
    assert "R-1122-01" in rule_ids
    # 关系方向按文件行定义：2203,reclass_pairs,1122 与 1231,contra_of,1122
    # 都以 1122 为 obj → 均为入向
    rel_out = {(r["rel_type"], r["other_code"]) for r in sem["related"] if r["dir"] == "out"}
    rel_in = {(r["rel_type"], r["other_code"]) for r in sem["related"] if r["dir"] == "in"}
    assert ("reclass_pairs", "2203") in rel_in
    assert ("contra_of", "1231") in rel_in
    assert rel_out == set()


def test_subject_semantics_prefix_rule_hits_children():
    sem = subject_semantics(STD, "660201")  # 管理费用子科目
    assert any(r["rule_id"] == "R-6602-01" for r in sem["rules"])


def test_subject_semantics_unknown_code_rejected():
    with pytest.raises(OntologyError):
        subject_semantics(STD, "9999")


# ---------- check_lines 确定性预检 ----------

def test_check_lines_error_missing_aux():
    findings = check_lines(STD, [{"code": "1122", "aux_dims": None}])
    assert len(findings) == 1
    f = findings[0]
    assert f["rule_id"] == "R-1122-01"
    assert f["severity"] == "error"
    assert f["index"] == 0


def test_check_lines_pass_with_dims():
    assert check_lines(STD, [{"code": "1122", "aux_dims": ["customer"]}]) == []
    assert check_lines(STD, [{"code": "1002", "aux_dims": None}]) == []


def test_check_lines_prefix_recommend():
    findings = check_lines(STD, [{"code": "660202", "aux_dims": []}])
    assert [f["rule_id"] for f in findings] == ["R-6602-01"]
    assert findings[0]["severity"] == "recommend"


def test_check_lines_deduction_only_on_debit():
    # 660204 同时命中 6602* 前缀规则，须补 department 维度以隔离 deduction 断言
    base = {"code": "660204", "aux_dims": ["department"]}
    assert check_lines(STD, [{**base, "debit": 0}]) == []
    findings = check_lines(STD, [{**base, "debit": "800.00"}])
    assert [f["rule_id"] for f in findings] == ["R-660204-01"]


def test_check_lines_object_lines_and_bad_debit():
    class Line:
        code = "1123"
        aux_dims = None
        debit = None

    findings = check_lines(STD, [Line()])
    assert [f["rule_id"] for f in findings] == ["R-1123-01"]
    # debit 非法值不抛异常，按 0 处理；6602* 前缀规则仍会命中
    findings2 = check_lines(STD, [{"code": "660204", "debit": "abc"}])
    assert [f["rule_id"] for f in findings2] == ["R-6602-01"]


# ---------- attrs 落库（导入 + 幂等回补） ----------

def test_import_persists_attrs(session):
    s, ls_id = session
    import_chart_of_accounts(s, ls_id, load_template_rows())
    acc = s.scalar(select(Account).where(Account.ledger_set_id == ls_id,
                                         Account.code == "1002"))
    assert acc.attrs == {"cash_flow": "yes"}
    acc2 = s.scalar(select(Account).where(Account.ledger_set_id == ls_id,
                                          Account.code == "6001"))
    # ② 现金流项目语义：6001 经模板导入后保留 cash_flow_item 声明
    assert acc2.attrs == {"cash_flow_item": "销售商品、提供劳务收到的现金"}


def test_reimport_backfills_attrs_for_legacy_seeded_account(session):
    """旧种子科目无 attrs → 重导入模板幂等回补（同 aux_dims 语义）。"""
    s, ls_id = session
    import_chart_of_accounts(s, ls_id, load_template_rows())
    acc = s.scalar(select(Account).where(Account.ledger_set_id == ls_id,
                                         Account.code == "1122"))
    acc.attrs = None  # 模拟旧种子
    s.flush()
    res = import_chart_of_accounts(s, ls_id, load_template_rows())
    assert res["created"] == 0
    s.flush()
    s.refresh(acc)
    assert acc.attrs == {"bad_debt": "eligible"}

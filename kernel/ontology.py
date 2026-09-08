"""语义本体层（阶段1底座）— 确定性只读：加载、校验、聚合，不写库不改账。

真源与范式（ADR 精神）：
- coa 模板 attrs 列：科目级声明属性（`k=v;k=v`，经 kernel.coa.parse_attrs 解析）
- data/ontology/rules.csv：流程级治理规则；scope 为精确编码或 `前缀*`
- data/ontology/relations.csv：跨科目语义关系（contra_of 备抵 / reclass_pairs
  报表重分类对冲 / carryforward_from 结转对应）

三者唯一真源是文件，随会计准则 profile 组织（当前仅 small_business 试点）。
本模块是消费端唯一入口：AI 引导、制单预检、报表重分类一律经此查询，
禁止绕过本模块直读文件。rel_type/attrs 键值只做声明，消费端渐进接入。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path

from kernel.coa import load_template_rows, parse_attrs

DATA_DIR = Path(__file__).parent / "data"

_STANDARDS = ("small_business",)

RULE_COLUMNS = ("rule_id", "scope", "rule_type", "params", "severity", "message_zh")
RELATION_COLUMNS = ("subj_code", "rel_type", "obj_code", "note_zh")

SEVERITIES = ("error", "recommend")
RULE_TYPES = ("aux_required", "deduction_limit")
REL_TYPES = ("contra_of", "reclass_pairs", "carryforward_from")


class OntologyError(ValueError):
    """本体文件缺失/格式非法或查询科目不在模板中：信息可直接展示给用户。"""


def _ontology_dir(standard: str) -> Path:
    if standard not in _STANDARDS:
        raise OntologyError(f"未知会计准则本体: {standard!r}（当前支持 {_STANDARDS}）")
    return DATA_DIR / "ontology"


def _read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.exists():
        raise OntologyError(f"本体文件缺失: {path.name}")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise OntologyError(f"{path.name} 缺列: {missing}")
        rows = list(reader)
    for i, r in enumerate(rows, start=2):  # 第 1 行是表头
        for c in columns:
            if c == "params":
                continue  # deduction_limit 等规则允许 params 为空
            if not (r.get(c) or "").strip():
                raise OntologyError(f"{path.name} 第{i}行 {c} 为空")
    return rows


def load_rules(standard: str = "small_business") -> list[dict[str, str]]:
    """治理规则表。确定性校验：severity/rule_type 值域 + aux_required 必带 params。"""
    rows = _read_csv(_ontology_dir(standard) / "rules.csv", RULE_COLUMNS)
    for r in rows:
        if r["severity"] not in SEVERITIES:
            raise OntologyError(f"规则 {r['rule_id']}: 非法 severity {r['severity']!r}")
        if r["rule_type"] not in RULE_TYPES:
            raise OntologyError(f"规则 {r['rule_id']}: 非法 rule_type {r['rule_type']!r}")
        if r["rule_type"] == "aux_required" and not r["params"].strip():
            raise OntologyError(f"规则 {r['rule_id']}: aux_required 必须给 params 维度名")
    return rows


def load_relations(standard: str = "small_business") -> list[dict[str, str]]:
    """语义关系表。确定性校验：rel_type 值域。"""
    rows = _read_csv(_ontology_dir(standard) / "relations.csv", RELATION_COLUMNS)
    for r in rows:
        if r["rel_type"] not in REL_TYPES:
            raise OntologyError(
                f"关系 {r['subj_code']}->{r['obj_code']}: 非法 rel_type {r['rel_type']!r}"
            )
    return rows


def template_attrs(standard: str = "small_business") -> dict[str, dict[str, str]]:
    """模板全量科目属性：{code: attrs_dict}。attrs 列可全空。"""
    if standard not in _STANDARDS:
        raise OntologyError(f"未知会计准则本体: {standard!r}（当前支持 {_STANDARDS}）")
    return {r["code"]: parse_attrs(r.get("attrs")) for r in load_template_rows()}


def _scope_match(scope: str, code: str) -> bool:
    if scope.endswith("*"):
        return code.startswith(scope[:-1])
    return code == scope


def _getter(line: Mapping | object):
    if isinstance(line, Mapping):
        return line.get
    return lambda k, _l=line: getattr(_l, k, None)


def _finding(index: int, code: str, rule: dict[str, str]) -> dict:
    return {
        "index": index,
        "code": code,
        "rule_id": rule["rule_id"],
        "rule_type": rule["rule_type"],
        "severity": rule["severity"],
        "message_zh": rule["message_zh"],
    }


def subject_semantics(standard: str, code: str) -> dict:
    """聚合单科目本体语义：attrs + 命中规则 + 相关关系（含出入两个方向）。"""
    attrs = template_attrs(standard)
    if code not in attrs:
        raise OntologyError(f"科目 {code} 不在 {standard} 模板中，无本体语义")
    rules = [r for r in load_rules(standard) if _scope_match(r["scope"], code)]
    related: list[dict] = []
    for r in load_relations(standard):
        if r["subj_code"] == code:
            related.append(
                {"dir": "out", "rel_type": r["rel_type"],
                 "other_code": r["obj_code"], "note_zh": r["note_zh"]}
            )
        elif r["obj_code"] == code:
            related.append(
                {"dir": "in", "rel_type": r["rel_type"],
                 "other_code": r["subj_code"], "note_zh": r["note_zh"]}
            )
    return {
        "standard": standard,
        "code": code,
        "attrs": attrs[code],
        "rules": rules,
        "related": related,
    }


def check_lines(standard: str, lines: Iterable[Mapping | object]) -> list[dict]:
    """确定性凭证行预检（只读，不拦截不改账；error/recommend 由调用方定夺）。

    lines 每项为 Mapping 或对象，取 code / aux_dims / debit（可缺省）。
    返回 findings 列表：{index, code, rule_id, rule_type, severity, message_zh}。
    """
    rules = load_rules(standard)
    findings: list[dict] = []
    for i, line in enumerate(lines):
        get = _getter(line)
        code = str(get("code") or "").strip()
        if not code:
            continue
        try:
            debit = float(get("debit") or 0)
        except (TypeError, ValueError):
            debit = 0.0
        dims = set(get("aux_dims") or ())
        for r in rules:
            if not _scope_match(r["scope"], code):
                continue
            if r["rule_type"] == "aux_required" and r["params"] not in dims:
                findings.append(_finding(i, code, r))
            elif r["rule_type"] == "deduction_limit" and debit > 0:
                findings.append(_finding(i, code, r))
    return findings

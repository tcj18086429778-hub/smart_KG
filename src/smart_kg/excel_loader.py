from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd

from .data_loader import write_json


FIELD_MAP = {
    "S_TYPE_CODE": "feature_type_code",
    "S_TYP_CD": "feature_type_code",
    "S_SUB_TYPE_CODE": "feature_subtype_code",
    "S_STYP_CD": "feature_subtype_code",
    "S_LEVEL": "feature_level",
    "S_LVL": "feature_level",
}


TARGET_MAP = {
    "ALL": "BOTH",
    "TOWER": "TOWER_SITE",
    "LINE": "LINE_SEGMENT",
    "BOTH": "BOTH",
    "TOWER_SITE": "TOWER_SITE",
    "LINE_SEGMENT": "LINE_SEGMENT",
}


SPECIAL_VALUE_STATUS = {
    "?": "NEGOTIABLE",
    "-1": "NEGOTIABLE",
    "/": "IGNORED",
    "NULL": "UNCLASSIFIED_OR_EMPTY",
}


def standardize_excel_rules(excel_path: Path, out_path: Path) -> list[dict[str, Any]]:
    rules = standardize_all_excel_rules(excel_path)
    write_json(out_path, rules)
    base_cost_out = out_path.with_name("base_cost_rules_from_excel.json")
    write_json(base_cost_out, standardize_base_cost_rules(excel_path))
    return rules


def standardize_all_excel_rules(excel_path: Path) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    rules.extend(standardize_base_rules(excel_path))
    rules.extend(standardize_crossing_rules(excel_path))
    rules.extend(standardize_scaling_rules(excel_path, sheet_name="地质气象条件成本对应表", category="geo_weather"))
    rules.extend(standardize_scaling_rules(excel_path, sheet_name="高程成本对应表", category="elevation"))
    return rules


def standardize_base_rules(excel_path: Path) -> list[dict[str, Any]]:
    rows = read_base_rule_rows(excel_path)
    rules: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        raw_condition = str(row.get("匹配条件") or "").strip()
        calc_mode = str(row.get("C_CALC_MODE") or "").strip()
        effect_target_raw = str(row.get("C_EFFECT_TARGET_TYPE") or "").strip()
        effect_value = normalize_effect_value(row.get("C_EFFECT_VALUE"))
        effect_attr = normalize_nullable(row.get("C_SPATIAL_INTERSECT_ATTR"))
        rule_prefix = "rule:base" if calc_mode == "FORBIDDEN" else "cost_rule:base"
        rules.append(
            {
                "rule_id": f"{rule_prefix}:{idx:04d}",
                "rule_name": str(row.get("规则名称") or f"规则{idx}").strip(),
                "calc_mode": calc_mode,
                "effect_target": TARGET_MAP.get(effect_target_raw, effect_target_raw),
                "match_condition_raw": raw_condition,
                "match_condition_json": parse_raw_condition(raw_condition),
                "effect_value": effect_value,
                "effect_value_status": effect_value_status(effect_value),
                "effect_attr": effect_attr,
                "effect_unit": effect_unit_for_attr(effect_attr),
                "voltage_level": None,
                "rule_category": "base",
                "source_table": "BASE规则配置表",
                "source_row": idx + 2,
                "enabled": bool(calc_mode),
            }
        )
    return rules


def standardize_crossing_rules(excel_path: Path) -> list[dict[str, Any]]:
    sheet_name = "交跨成本对应表"
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    rules: list[dict[str, Any]] = []
    valid_idx = 0
    for df_idx, row in df.iterrows():
        voltage = normalize_nullable(row.get("电压等级"))
        feature_name = normalize_nullable(row.get("被跨要素S_SUB_TYPE_NAME"))
        feature_code = normalize_code(row.get("被跨要素SUB_TYPE_CODE"))
        feature_level = normalize_nullable(row.get("被跨对象S_LEVEL"))
        effect_value = normalize_effect_value(row.get("单次跨越成本"))
        if not voltage or not feature_name or not feature_code or effect_value_status(effect_value) == "IGNORED":
            continue
        valid_idx += 1
        conditions = [
            {"field": "voltage_level", "operator": "eq", "value": voltage},
            {"field": "feature_subtype_code", "operator": "eq", "value": feature_code},
            {"field": "relation", "operator": "eq", "value": "CROSSES"},
        ]
        if feature_level and feature_level != "/":
            conditions.append({"field": "feature_level", "operator": "eq", "value": feature_level})
        level_id = slug_value("any" if feature_level == "/" else (feature_level or "any"))
        rules.append(
            {
                "rule_id": f"cost_rule:cross:{slug_value(voltage)}:{feature_code}:{level_id}",
                "rule_name": f"{voltage}跨越{feature_name}",
                "calc_mode": "CROSS_EVENT",
                "effect_target": "LINE_SEGMENT",
                "match_condition_raw": f"voltage_level='{voltage}' && S_SUB_TYPE_CODE='{feature_code}' && relation='CROSSES'",
                "match_condition_json": {"logic": "AND", "conditions": conditions},
                "effect_value": effect_value,
                "effect_value_status": effect_value_status(effect_value),
                "effect_attr": "S_CNT",
                "effect_unit": "万元/次",
                "voltage_level": voltage,
                "rule_category": "cross",
                "source_table": sheet_name,
                "source_row": int(df_idx) + 2,
                "enabled": True,
            }
        )
    return rules


def standardize_scaling_rules(excel_path: Path, sheet_name: str, category: str) -> list[dict[str, Any]]:
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    subtype_name_col = "S_SUB_TYPE_NAME" if "S_SUB_TYPE_NAME" in df.columns else "S_SUB_TYPE"
    rules: list[dict[str, Any]] = []
    for df_idx, row in df.iterrows():
        voltage = normalize_nullable(row.get("电压等级"))
        feature_name = normalize_nullable(row.get(subtype_name_col))
        feature_code = normalize_code(row.get("S_SUB_TYPE_CODE"))
        feature_level = normalize_nullable(row.get("S_LEVEL"))
        effect_value = normalize_effect_value(row.get("成本系数"))
        if not voltage or not feature_name or not feature_code or feature_name == "..." or effect_value_status(effect_value) != "NUMERIC":
            continue
        conditions = [
            {"field": "voltage_level", "operator": "eq", "value": voltage},
            {"field": "feature_subtype_code", "operator": "eq", "value": feature_code},
            {"field": "feature_level", "operator": "eq", "value": feature_level},
            {"field": "relation", "operator": "in", "value": ["LOCATED_IN", "INTERSECTS", "WITHIN_BUFFER"]},
        ]
        rules.append(
            {
                "rule_id": f"cost_rule:{category}:{slug_value(voltage)}:{feature_code}:{slug_value(feature_level)}",
                "rule_name": f"{voltage}{feature_name}{feature_level}成本系数",
                "calc_mode": "MAIN_COST_SCALING",
                "effect_target": "BOTH",
                "match_condition_raw": f"voltage_level='{voltage}' && S_SUB_TYPE_CODE='{feature_code}' && S_LEVEL='{feature_level}'",
                "match_condition_json": {"logic": "AND", "conditions": conditions},
                "effect_value": effect_value,
                "effect_value_status": effect_value_status(effect_value),
                "effect_attr": None,
                "effect_unit": "系数",
                "voltage_level": voltage,
                "rule_category": category,
                "source_table": sheet_name,
                "source_row": int(df_idx) + 2,
                "enabled": True,
            }
        )
    return rules


def standardize_base_cost_rules(excel_path: Path) -> list[dict[str, Any]]:
    sheet_name = "本体造价对应表"
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    rules: list[dict[str, Any]] = []
    for df_idx, row in df.iterrows():
        voltage = normalize_nullable(row.get("电压等级"))
        line_unit_cost = normalize_effect_value(row.get("线路（长度）单位造价"))
        tower_unit_cost = normalize_effect_value(row.get("杆塔单位造价"))
        if not voltage or effect_value_status(line_unit_cost) != "NUMERIC" or effect_value_status(tower_unit_cost) != "NUMERIC":
            continue
        rules.append(
            {
                "id": f"base_cost:{slug_value(voltage)}",
                "voltage_level": voltage,
                "line_unit_cost": line_unit_cost,
                "line_unit": "万元/公里",
                "tower_unit_cost": tower_unit_cost,
                "tower_unit": "万元/基",
                "source_table": sheet_name,
                "source_row": int(df_idx) + 2,
                "enabled": True,
            }
        )
    return rules


def read_base_rule_rows(excel_path: Path) -> list[dict[str, Any]]:
    raw = pd.read_excel(excel_path, sheet_name="BASE规则配置表", header=None)
    header_idx = None
    for idx, row in raw.iterrows():
        values = [str(item).strip() for item in row.tolist() if pd.notna(item)]
        if "规则名称" in values and "匹配条件" in values:
            header_idx = idx
            break
    if header_idx is None:
        raise ValueError("Cannot find header row in BASE规则配置表.")
    df = pd.read_excel(excel_path, sheet_name="BASE规则配置表", header=header_idx)
    df = df.dropna(how="all")
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        item = {str(key).strip(): normalize_nullable(value) for key, value in row.to_dict().items()}
        if item.get("规则名称") and item.get("匹配条件"):
            rows.append(item)
    return rows


def parse_raw_condition(raw_condition: str) -> dict[str, Any]:
    parts = [part.strip() for part in re.split(r"\s*&&\s*", raw_condition) if part.strip()]
    conditions = [parse_condition_part(part) for part in parts]
    if not conditions:
        return {"field": "__always__", "operator": "eq", "value": True}
    if len(conditions) == 1:
        return conditions[0]
    return {"logic": "AND", "conditions": conditions}


def parse_condition_part(part: str) -> dict[str, Any]:
    compact = part.strip()
    not_in_match = re.match(r"^([A-Za-z_]+)\s+not\s+in\s*\((.*)\)$", compact, flags=re.I)
    if not_in_match:
        return {
            "field": map_field(not_in_match.group(1)),
            "operator": "not_in",
            "value": parse_value_list(not_in_match.group(2)),
        }
    in_match = re.match(r"^([A-Za-z_]+)\s+in\s*\((.*)\)$", compact, flags=re.I)
    if in_match:
        return {
            "field": map_field(in_match.group(1)),
            "operator": "in",
            "value": parse_value_list(in_match.group(2)),
        }
    eq_match = re.match(r"^([A-Za-z_]+)\s*=\s*['\"]?([^'\"]+)['\"]?$", compact)
    if eq_match:
        return {
            "field": map_field(eq_match.group(1)),
            "operator": "eq",
            "value": normalize_nullable(eq_match.group(2)),
        }
    raise ValueError(f"Unsupported condition expression: {part}")


def parse_value_list(raw_values: str) -> list[Any]:
    values: list[Any] = []
    for item in raw_values.split(","):
        value = item.strip().strip("'\"")
        values.append(normalize_nullable(value))
    return values


def map_field(field: str) -> str:
    return FIELD_MAP.get(field.strip(), field.strip())


def normalize_nullable(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.strip()
        if value in {"", "NULL", "null", "None"}:
            return None
    return value


def normalize_effect_value(value: Any) -> Any:
    value = normalize_nullable(value)
    if isinstance(value, str) and value.strip() in SPECIAL_VALUE_STATUS:
        return value.strip()
    if value == -1:
        return -1
    return value


def effect_value_status(value: Any) -> str:
    if value is None:
        return "NULL"
    key = str(value)
    return SPECIAL_VALUE_STATUS.get(key, "NUMERIC")


def normalize_code(value: Any) -> str | None:
    value = normalize_nullable(value)
    if value is None or value == "...":
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def effect_unit_for_attr(effect_attr: Any) -> str | None:
    if effect_attr == "S_AREA":
        return "万元/亩"
    if effect_attr in {"S_CNT", "S_COUNT"}:
        return "万元/处"
    if effect_attr == "S_LTH":
        return "万元/公里"
    return None


def slug_value(value: Any) -> str:
    value = normalize_nullable(value)
    if value is None:
        return "any"
    text = str(value).strip()
    return (
        text.replace(" ", "")
        .replace("/", "_")
        .replace("\\", "_")
        .replace("（", "_")
        .replace("）", "")
        .replace("(", "_")
        .replace(")", "")
        .replace("°", "度")
        .replace("＞", "gt")
        .replace(">", "gt")
    )

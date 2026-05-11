from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .paths import CONFIG_DIR, DATA_DIR
from .schemas import GeoFeature, LineSegment, Rule, SpatialRelation, TowerSite


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_csv_dicts(path: Path) -> list[dict[str, str | None]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def clean_empty(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    return value


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: clean_empty(value) for key, value in row.items()}


def load_geo_features(path: Path | None = None) -> dict[str, GeoFeature]:
    path = path or DATA_DIR / "raw" / "sample_geo_features.csv"
    features = [GeoFeature.model_validate(clean_row(row)) for row in read_csv_dicts(path)]
    return {item.id: item for item in features}


def load_tower_sites(path: Path | None = None) -> dict[str, TowerSite]:
    path = path or DATA_DIR / "raw" / "sample_tower_sites.csv"
    sites = [TowerSite.model_validate(clean_row(row)) for row in read_csv_dicts(path)]
    return {item.id: item for item in sites}


def load_line_segments(path: Path | None = None) -> dict[str, LineSegment]:
    path = path or DATA_DIR / "raw" / "sample_line_segments.csv"
    segments: list[LineSegment] = []
    for row in read_csv_dicts(path):
        clean = clean_row(row)
        if clean.get("length_km") is not None:
            clean["length_km"] = float(clean["length_km"])
        segments.append(LineSegment.model_validate(clean))
    return {item.id: item for item in segments}


def load_spatial_relations(path: Path | None = None) -> list[SpatialRelation]:
    path = path or DATA_DIR / "spatial_relations" / "sample_spatial_relations.json"
    return [SpatialRelation.model_validate(item) for item in read_json(path)]


def load_rules(path: Path | None = None) -> list[Rule]:
    path = path or default_rules_path()
    return [Rule.model_validate(item) for item in read_json(path)]


def load_base_cost_rules(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or DATA_DIR / "standardized" / "base_cost_rules_from_excel.json"
    if not path.exists():
        return []
    return list(read_json(path))


def default_rules_path() -> Path:
    standardized = DATA_DIR / "standardized" / "rules_from_excel.json"
    if standardized.exists():
        return standardized
    return CONFIG_DIR / "rules.json"


def load_demo_bundle() -> dict[str, Any]:
    return {
        "features": load_geo_features(),
        "tower_sites": load_tower_sites(),
        "line_segments": load_line_segments(),
        "spatial_relations": load_spatial_relations(),
        "rules": load_rules(),
    }

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .data_loader import load_demo_bundle
from .rule_engine import RuleEngine


app = FastAPI(title="smart_KG", version="0.1.0")


def build_engine() -> RuleEngine:
    bundle = load_demo_bundle()
    return RuleEngine(**bundle)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/rules")
def rules() -> list[dict]:
    bundle = load_demo_bundle()
    return [rule.model_dump(mode="json") for rule in bundle["rules"]]


@app.get("/spatial-relations")
def spatial_relations() -> list[dict]:
    bundle = load_demo_bundle()
    return [relation.model_dump(mode="json") for relation in bundle["spatial_relations"]]


@app.get("/evaluate")
def evaluate() -> dict:
    return build_engine().evaluate().model_dump(mode="json")


@app.get("/explain/line-segment/{segment_id}")
def explain_line_segment(segment_id: str) -> dict:
    result = build_engine().explain_subject(segment_id)
    if result["status"] == "available" and not result["constraints"] and not result["cost_rules"]:
        bundle = load_demo_bundle()
        if segment_id not in bundle["line_segments"]:
            raise HTTPException(status_code=404, detail="Line segment not found.")
    return result


@app.get("/explain/tower-site/{site_id}")
def explain_tower_site(site_id: str) -> dict:
    result = build_engine().explain_subject(site_id)
    if result["status"] == "available" and not result["constraints"] and not result["cost_rules"]:
        bundle = load_demo_bundle()
        if site_id not in bundle["tower_sites"]:
            raise HTTPException(status_code=404, detail="Tower site not found.")
    return result

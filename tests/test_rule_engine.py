from smart_kg.data_loader import load_demo_bundle
from smart_kg.rule_engine import RuleEngine


def test_demo_rule_engine_triggers_constraints_and_costs() -> None:
    report = RuleEngine(**load_demo_bundle()).evaluate()
    assert report.status_by_subject["line_segment:demo:A:B"] == "blocked"
    assert report.status_by_subject["tower_site:demo:A"] == "cost_affected"
    assert any(trigger.rule_id == "rule:base:0001" for trigger in report.triggers)
    assert any(trigger.rule_id == "cost_rule:base:0005" for trigger in report.triggers)
    assert any(trigger.rule_id == "cost_rule:cross:10kV:3001:any" for trigger in report.triggers)
    assert any(trigger.rule_id == "cost_rule:elevation:10kV:2803:25度-35度" for trigger in report.triggers)

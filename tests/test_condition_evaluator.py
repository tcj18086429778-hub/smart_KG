from smart_kg.condition_evaluator import evaluate_condition


def test_leaf_condition_eq() -> None:
    condition = {"field": "feature_type_code", "operator": "eq", "value": "1100"}
    assert evaluate_condition(condition, {"feature_type_code": "1100"})


def test_group_condition_and() -> None:
    condition = {
        "logic": "AND",
        "conditions": [
            {"field": "feature_type_code", "operator": "eq", "value": "1100"},
            {"field": "relation", "operator": "in", "value": ["INTERSECTS", "LOCATED_IN"]},
        ],
    }
    assert evaluate_condition(condition, {"feature_type_code": "1100", "relation": "INTERSECTS"})
    assert not evaluate_condition(condition, {"feature_type_code": "1100", "relation": "CROSSES"})

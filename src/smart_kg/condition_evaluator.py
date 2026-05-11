from __future__ import annotations

from typing import Any

from .schemas import Condition, Logic, Operator


NULL_LIKE = {"", "NULL", "null", "None", "none"}


def is_null_like(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in NULL_LIKE
    return False


def normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if value in NULL_LIKE:
            return None
    return value


def get_context_value(context: dict[str, Any], field: str) -> Any:
    if field in context:
        return context[field]
    current: Any = context
    for part in field.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def compare_numeric(left: Any, right: Any, operator: Operator) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError):
        return False
    if operator == Operator.GT:
        return left_number > right_number
    if operator == Operator.GTE:
        return left_number >= right_number
    if operator == Operator.LT:
        return left_number < right_number
    if operator == Operator.LTE:
        return left_number <= right_number
    raise ValueError(f"Unsupported numeric operator: {operator}")


def evaluate_condition(condition: Condition | dict[str, Any], context: dict[str, Any]) -> bool:
    if not isinstance(condition, Condition):
        condition = Condition.model_validate(condition)

    if condition.logic:
        results = [evaluate_condition(child, context) for child in condition.conditions or []]
        if condition.logic == Logic.AND:
            return all(results)
        if condition.logic == Logic.OR:
            return any(results)
        raise ValueError(f"Unsupported logic: {condition.logic}")

    assert condition.field is not None
    assert condition.operator is not None

    actual = normalize_value(get_context_value(context, condition.field))
    expected = condition.value
    if isinstance(expected, list):
        expected = [normalize_value(item) for item in expected]
    else:
        expected = normalize_value(expected)

    op = condition.operator
    if op == Operator.EQ:
        return actual == expected
    if op == Operator.NEQ:
        return actual != expected
    if op == Operator.IN:
        values = expected if isinstance(expected, list) else [expected]
        return actual in values
    if op == Operator.NOT_IN:
        values = expected if isinstance(expected, list) else [expected]
        return actual not in values
    if op == Operator.IS_NULL:
        return is_null_like(actual)
    if op == Operator.EXISTS:
        return not is_null_like(actual)
    if op in {Operator.GT, Operator.GTE, Operator.LT, Operator.LTE}:
        return compare_numeric(actual, expected, op)
    raise ValueError(f"Unsupported operator: {op}")

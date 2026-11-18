"""Safe, deliberately small Python strategy format used by AutoInvest uploads.

Uploaded files are parsed and interpreted. They are never passed to exec/eval and
cannot import modules, access files, use the network, or run loops.
"""

import ast
import math
from functools import lru_cache
from typing import Any

MAX_SOURCE_BYTES = 64 * 1024
MAX_PARAMETERS = 24
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)


class StrategyValidationError(ValueError):
    pass


@lru_cache(maxsize=64)
def _source_parts(source: str) -> tuple[dict[str, Any], ast.FunctionDef]:
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise StrategyValidationError("Strategy file exceeds the 64 KB limit.")
    try:
        module = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise StrategyValidationError(f"Python syntax error on line {exc.lineno}: {exc.msg}") from exc
    except RecursionError as exc:
        raise StrategyValidationError("Strategy file is nested too deeply.") from exc
    nodes = list(ast.walk(module))
    if len(nodes) > 1200:
        raise StrategyValidationError("Strategy file is too complex; keep it under 1,200 syntax nodes.")
    pending = [(module, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 32:
            raise StrategyValidationError("Strategy nesting cannot exceed 32 levels.")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))

    metadata = None
    function = None
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "STRATEGY":
            if metadata is not None:
                raise StrategyValidationError("Define STRATEGY only once.")
            try:
                metadata = ast.literal_eval(node.value)
            except (ValueError, TypeError, RecursionError) as exc:
                raise StrategyValidationError("STRATEGY must be a literal dictionary.") from exc
        elif isinstance(node, ast.FunctionDef) and node.name == "evaluate":
            if function is not None:
                raise StrategyValidationError("Define evaluate(candles, params) only once.")
            if node.decorator_list or node.args.vararg or node.args.kwarg or node.args.kwonlyargs or node.args.defaults:
                raise StrategyValidationError("evaluate must be a plain function with no decorators or default arguments.")
            if [arg.arg for arg in node.args.args] != ["candles", "params"]:
                raise StrategyValidationError("Use the function signature evaluate(candles, params).")
            function = node
        else:
            raise StrategyValidationError("Only the STRATEGY dictionary and evaluate(candles, params) function are allowed.")

    if not isinstance(metadata, dict) or function is None:
        raise StrategyValidationError("File needs a STRATEGY dictionary and evaluate(candles, params) function.")
    try:
        _validate_metadata(metadata)
        _validate_statements(function.body)
    except RecursionError as exc:
        raise StrategyValidationError("Strategy file is nested too deeply.") from exc
    return metadata, function


def _validate_metadata(metadata: dict[str, Any]) -> None:
    if not isinstance(metadata.get("name"), str) or not metadata["name"].strip() or len(metadata["name"]) > 80:
        raise StrategyValidationError("STRATEGY.name must be a non-empty string up to 80 characters.")
    if not isinstance(metadata.get("description", ""), str) or len(metadata.get("description", "")) > 400:
        raise StrategyValidationError("STRATEGY.description must be a string up to 400 characters.")
    parameters = metadata.get("parameters", [])
    if not isinstance(parameters, list) or len(parameters) > MAX_PARAMETERS:
        raise StrategyValidationError("STRATEGY.parameters must be a list with no more than 24 items.")
    seen = set()
    for item in parameters:
        if not isinstance(item, dict):
            raise StrategyValidationError("Each strategy parameter must be a dictionary.")
        key, label, kind = item.get("key"), item.get("label"), item.get("type")
        if not isinstance(key, str) or not key.isidentifier() or key in seen:
            raise StrategyValidationError("Parameter keys must be unique Python identifiers.")
        if not isinstance(label, str) or not label.strip() or len(label) > 60:
            raise StrategyValidationError(f"Parameter {key} needs a short label.")
        if kind not in {"integer", "number", "boolean"}:
            raise StrategyValidationError(f"Parameter {key} type must be integer, number, or boolean.")
        seen.add(key)
        if kind == "boolean":
            if not isinstance(item.get("default"), bool):
                raise StrategyValidationError(f"Parameter {key} needs a boolean default.")
        else:
            minimum, maximum, default = item.get("min"), item.get("max"), item.get("default")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= 1e12 for v in (minimum, maximum, default)):
                raise StrategyValidationError(f"Parameter {key} needs finite min, max, and default values.")
            if minimum > default or default > maximum or minimum == maximum:
                raise StrategyValidationError(f"Parameter {key} default must be between min and max.")
            if kind == "integer" and not all(isinstance(v, int) for v in (minimum, maximum, default)):
                raise StrategyValidationError(f"Parameter {key} requires integer bounds and default.")
        if not isinstance(item.get("advanced", False), bool):
            raise StrategyValidationError(f"Parameter {key}.advanced must be boolean.")


def _validate_statements(statements: list[ast.stmt]) -> None:
    for statement in statements:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            _validate_expression(statement.value)
        elif isinstance(statement, ast.If):
            _validate_expression(statement.test)
            _validate_statements(statement.body)
            _validate_statements(statement.orelse)
        elif isinstance(statement, ast.Return):
            if statement.value is not None:
                _validate_expression(statement.value)
        else:
            raise StrategyValidationError(f"Unsupported statement: {type(statement).__name__}. Use assignments, if, and return.")


def _validate_expression(node: ast.expr) -> None:
    if isinstance(node, ast.Constant):
        if node.value is not None and not isinstance(node.value, (str, bool, int, float)):
            raise StrategyValidationError("Only text, booleans, and real numeric literals are allowed.")
        if isinstance(node.value, str) and len(node.value) > 4096:
            raise StrategyValidationError("String literals may contain at most 4,096 characters.")
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if not math.isfinite(node.value) or abs(node.value) > 1e12:
                raise StrategyValidationError("Numeric literals must be finite and within ±1,000,000,000,000.")
        return
    if isinstance(node, ast.Name):
        return
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        if len(node.elts) > 128:
            raise StrategyValidationError("Literal lists may contain at most 128 items.")
        for item in node.elts:
            _validate_expression(item)
        return
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if key is None:
                raise StrategyValidationError("Dictionary unpacking is not supported.")
            _validate_expression(key)
            _validate_expression(value)
        return
    if isinstance(node, ast.Subscript):
        _validate_expression(node.value)
        _validate_expression(node.slice)
        return
    if isinstance(node, ast.Slice):
        for value in (node.lower, node.upper, node.step):
            if value is not None:
                _validate_expression(value)
        return
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        _validate_expression(node.left)
        _validate_expression(node.right)
        return
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.UAdd, ast.USub)):
        _validate_expression(node.operand)
        return
    if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
        for value in node.values:
            _validate_expression(value)
        return
    if isinstance(node, ast.Compare) and all(isinstance(op, _ALLOWED_CMPOPS) for op in node.ops):
        _validate_expression(node.left)
        for value in node.comparators:
            _validate_expression(value)
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _HELPERS:
        if node.keywords:
            raise StrategyValidationError("Strategy helper calls use positional arguments only.")
        for arg in node.args:
            _validate_expression(arg)
        return
    raise StrategyValidationError(f"Unsupported expression: {type(node).__name__}. Imports, attributes, comprehensions, and arbitrary calls are not allowed.")


def inspect_strategy(source: str) -> dict[str, Any]:
    metadata, _ = _source_parts(source)
    return metadata


def _sma(candles: list[dict[str, Any]], period: int, field: str = "close") -> list[float | None]:
    if not isinstance(period, int) or period < 1 or period > 500:
        raise ValueError("SMA period must be an integer from 1 to 500.")
    values = [float(candle[field]) for candle in candles]
    return [None if i + 1 < period else sum(values[i + 1 - period:i + 1]) / period for i in range(len(values))]


def _highest(candles: list[dict[str, Any]], field: str, period: int) -> float:
    if not isinstance(period, int) or period < 1 or period > 500:
        raise ValueError("highest period must be an integer from 1 to 500.")
    values = [float(candle[field]) for candle in candles[-period:]]
    if not values:
        raise ValueError("highest needs at least one candle.")
    return max(values)


def _lowest(candles: list[dict[str, Any]], field: str, period: int) -> float:
    if not isinstance(period, int) or period < 1 or period > 500:
        raise ValueError("lowest period must be an integer from 1 to 500.")
    values = [float(candle[field]) for candle in candles[-period:]]
    if not values:
        raise ValueError("lowest needs at least one candle.")
    return min(values)


def _crossed_above(left: list[Any], right: list[Any]) -> bool:
    return len(left) >= 2 and len(right) >= 2 and None not in (left[-2], left[-1], right[-2], right[-1]) and left[-2] <= right[-2] and left[-1] > right[-1]


def _crossed_below(left: list[Any], right: list[Any]) -> bool:
    return len(left) >= 2 and len(right) >= 2 and None not in (left[-2], left[-1], right[-2], right[-1]) and left[-2] >= right[-2] and left[-1] < right[-1]


def _volume_multiple(candles: list[dict[str, Any]], period: int) -> float:
    if not isinstance(period, int) or isinstance(period, bool) or period < 1 or period > 500 or len(candles) <= period:
        raise ValueError("volume_multiple needs more candles than the lookback period.")
    baseline = sum(float(row["volume"]) for row in candles[-period - 1:-1]) / period
    return float(candles[-1]["volume"]) / baseline if baseline else 0.0


_HELPERS = {
    "sma": _sma,
    "highest": _highest,
    "lowest": _lowest,
    "crossed_above": _crossed_above,
    "crossed_below": _crossed_below,
    "volume_multiple": _volume_multiple,
    "abs": abs,
    "len": len,
    "sum": sum,
    "min": min,
    "max": max,
}


def _bounded_value(value):
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if visited > 12000 or depth > 32:
            raise ValueError("Strategy data exceeds its size or nesting budget.")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for pair in item.items() for v in pair)
        elif isinstance(item, (list, tuple, set)):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, (int, float)) and (not math.isfinite(item) or abs(item) > 1e15):
            raise ValueError("Strategy numeric value exceeds its budget.")
        elif not isinstance(item, (str, int, float, bool, type(None))):
            raise ValueError("Unsupported strategy data value.")
    return value


def _evaluate_expression(node: ast.expr, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in {"True", "False", "None"}:
            return {"True": True, "False": False, "None": None}[node.id]
        if node.id not in scope:
            raise ValueError(f"Unknown strategy name: {node.id}")
        return scope[node.id]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_evaluate_expression(item, scope) for item in node.elts]
        return values if isinstance(node, ast.List) else tuple(values) if isinstance(node, ast.Tuple) else set(values)
    if isinstance(node, ast.Dict):
        return {_evaluate_expression(k, scope): _evaluate_expression(v, scope) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Subscript):
        return _evaluate_expression(node.value, scope)[_evaluate_expression(node.slice, scope)]
    if isinstance(node, ast.Slice):
        return slice(*[_evaluate_expression(v, scope) if v is not None else None for v in (node.lower, node.upper, node.step)])
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_expression(node.operand, scope)
        return not value if isinstance(node.op, ast.Not) else +value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left, right = _evaluate_expression(node.left, scope), _evaluate_expression(node.right, scope)
        # Text formatting/repetition and sequence algebra can allocate arbitrary
        # memory before a post-operation size check. Only real arithmetic is allowed.
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= 1e15 for v in (left, right)):
            raise ValueError("Strategy arithmetic requires finite real numbers within the supported range.")
        if isinstance(node.op, ast.Mult) and isinstance(right, (str, list, tuple)) and isinstance(left, int):
            if len(right) * max(0, left) > 1000:
                raise ValueError("Strategy sequence operations are limited to 1,000 items.")
        if isinstance(node.op, ast.Mult) and isinstance(left, (str, list, tuple)) and isinstance(right, int):
            if len(left) * max(0, right) > 1000:
                raise ValueError("Strategy sequence operations are limited to 1,000 items.")
        if isinstance(node.op, ast.Add) and isinstance(left, (str, list, tuple)) and isinstance(right, type(left)):
            if len(left) + len(right) > 1000:
                raise ValueError("Strategy sequence operations are limited to 1,000 items.")
        if isinstance(node.op, ast.Pow) and (not isinstance(right, (int, float)) or abs(right) > 8 or not isinstance(left, (int, float)) or abs(left) > 1e15):
            raise ValueError("Strategy exponentiation is limited to small numeric values.")
        operations = {ast.Add: lambda: left + right, ast.Sub: lambda: left - right, ast.Mult: lambda: left * right, ast.Div: lambda: left / right, ast.FloorDiv: lambda: left // right, ast.Mod: lambda: left % right, ast.Pow: lambda: left ** right}
        result = operations[type(node.op)]()
        if isinstance(result, (int, float)) and not isinstance(result, bool) and (not math.isfinite(result) or abs(result) > 1e15):
            raise ValueError("Strategy arithmetic exceeded the supported numeric range.")
        return result
    if isinstance(node, ast.BoolOp):
        # Match Python's short-circuit semantics, including the returned operand.
        value = None
        for child in node.values:
            value = _evaluate_expression(child, scope)
            if isinstance(node.op, ast.And) and not value or isinstance(node.op, ast.Or) and value:
                break
        return value
    if isinstance(node, ast.Compare):
        left = _evaluate_expression(node.left, scope)
        for operation, comparator in zip(node.ops, node.comparators):
            right = _evaluate_expression(comparator, scope)
            if isinstance(left, (list, tuple, set, dict)) or (not isinstance(operation, (ast.In, ast.NotIn)) and isinstance(right, (list, tuple, set, dict))):
                raise ValueError("Compare scalar values rather than nested strategy containers.")
            compare = {ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right, ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right, ast.Gt: lambda: left > right, ast.GtE: lambda: left >= right, ast.In: lambda: left in right, ast.NotIn: lambda: left not in right}[type(operation)]
            if not compare():
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        result = _HELPERS[node.func.id](*[_bounded_value(_evaluate_expression(arg, scope)) for arg in node.args])
        if isinstance(result, (list, tuple)) and len(result) > 500:
            raise ValueError("A strategy helper returned too many values.")
        return _bounded_value(result)
    raise ValueError("Unsupported strategy expression.")


def evaluate_strategy(source: str, candles: list[dict[str, Any]], params: dict[str, Any]) -> Any:
    if not candles or len(candles) > 500:
        raise ValueError("Strategies require between 1 and 500 candles.")
    _, function = _source_parts(source)
    scope: dict[str, Any] = {"candles": candles, "params": params, **_HELPERS}

    def run(statements: list[ast.stmt]) -> tuple[bool, Any]:
        for statement in statements:
            if isinstance(statement, ast.Assign):
                scope[statement.targets[0].id] = _bounded_value(_evaluate_expression(statement.value, scope))
            elif isinstance(statement, ast.If):
                branch = statement.body if _evaluate_expression(statement.test, scope) else statement.orelse
                returned, value = run(branch)
                if returned:
                    return returned, value
            elif isinstance(statement, ast.Return):
                return True, None if statement.value is None else _bounded_value(_evaluate_expression(statement.value, scope))
        return False, None

    returned, value = run(function.body)
    if not returned:
        raise ValueError("evaluate must return a result on every path.")
    return value

"""A deliberately small JSON Schema interpreter; unsupported schema keywords fail closed."""
from __future__ import annotations

import math
import re
from pathlib import Path

from ._data import Json, loads, object_value

SCHEMA_DIRECTORY = Path(__file__).resolve().parent / "_resources" / "schemas"
SCHEMA_NAMES = frozenset({"task", "plan", "research", "review", "review-digest", "result", "gates"})


class SchemaError(ValueError):
    pass


def validate(value: Json, schema: dict[str, Json], path: str = "$") -> None:
    _check_schema(schema, path)
    _validate(value, schema, path)


def _check_schema(schema: dict[str, Json], path: str) -> None:
    allowed = {"$schema", "title", "description", "type", "required", "properties",
               "additionalProperties", "enum", "items", "anyOf", "pattern"}
    if schema.keys() - allowed:
        raise SchemaError(f"{path}: unsupported schema keywords")
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    if ("type" in schema or "anyOf" not in schema) and (not types or any(
            item not in ("null", "boolean", "integer", "number", "string", "array", "object") for item in types)):
        raise SchemaError(f"{path}: invalid schema type")
    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise SchemaError(f"{path}: anyOf must be a nonempty schema array")
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                raise SchemaError(f"{path}: anyOf entries must be schemas")
            _check_schema(alternative, path)
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            raise SchemaError(f"{path}: pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SchemaError(f"{path}: invalid pattern") from exc
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise SchemaError(f"{path}: enum must be an array")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise SchemaError(f"{path}: required must be a string array")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise SchemaError(f"{path}: additionalProperties must be boolean")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise SchemaError(f"{path}: properties must be an object")
    for name, item in properties.items():
        if not isinstance(item, dict):
            raise SchemaError(f"{path}.{name}: property schema must be an object")
        _check_schema(item, f"{path}.{name}")
    if "array" in types or "items" in schema:
        items = schema.get("items")
        if not isinstance(items, dict):
            raise SchemaError(f"{path}: items schema required")
        _check_schema(items, f"{path}[]")


def _validate(value: Json, schema: dict[str, Json], path: str) -> None:
    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        assert isinstance(alternatives, list)
        for alternative in alternatives:
            try:
                _validate(value, object_value(alternative, path), path)
                break
            except SchemaError:
                continue
        else:
            raise SchemaError(f"{path}: no anyOf alternative matched")
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    matches = {"null": value is None, "boolean": isinstance(value, bool),
               "integer": type(value) is int or (isinstance(value, float) and value.is_integer()),
               "number": type(value) in (int, float),
               "string": isinstance(value, str), "array": isinstance(value, list),
               "object": isinstance(value, dict)}
    if "type" in schema and not any(matches.get(str(item), False) for item in types):
        raise SchemaError(f"{path}: expected {expected}")
    if isinstance(value, str) and "pattern" in schema:
        pattern = schema["pattern"]
        assert isinstance(pattern, str)
        if re.fullmatch(pattern, value) is None:
            raise SchemaError(f"{path}: string does not match pattern")
    if isinstance(value, float) and not math.isfinite(value):
        raise SchemaError(f"{path}: nonfinite numbers are not JSON")
    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list) or not any(_equal(value, item) for item in options):
            raise SchemaError(f"{path}: value outside enum")
    if isinstance(value, dict):
        properties = object_value(schema.get("properties", {}), f"{path}.properties")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise SchemaError(f"{path}: malformed required schema")
        for name in required:
            if isinstance(name, str) and name not in value:
                raise SchemaError(f"{path}.{name}: required field missing")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise SchemaError(f"{path}: additional properties forbidden")
        for name, item in value.items():
            if name in properties:
                _validate(item, object_value(properties[name], f"{path}.{name}"), f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        items = object_value(schema.get("items"), f"{path}.items")
        for index, item in enumerate(value):
            _validate(item, items, f"{path}[{index}]")


def _equal(left: Json, right: Json) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(item, right[name]) for name, item in left.items())
    return type(left) is type(right) and left == right


def load_schema(name: str) -> dict[str, Json]:
    if name not in SCHEMA_NAMES:
        raise SchemaError("Unknown artifact schema")
    return object_value(loads((SCHEMA_DIRECTORY / f"{name}.json").read_text(encoding="utf-8")), name)


def validate_artifact(name: str, value: Json) -> None:
    validate(value, load_schema(name))


def normalize_input_plan(value: dict[str, Json]) -> dict[str, Json]:
    """Validate an input plan, allowing legacy steps without role; never mutate it."""
    normalized = dict(value)
    steps = value.get("steps")
    if isinstance(steps, list):
        normalized["steps"] = [{"role": None, **step} if isinstance(step, dict) else step for step in steps]
    validate_artifact("plan", normalized)
    return normalized


def validate_file(name: str, path: Path) -> Json:
    value = loads(path.read_text(encoding="utf-8"))
    validate_artifact(name, value)
    return value

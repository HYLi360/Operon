"""Validation for Operon's packaged file contracts, without extra dependencies.

The validator implements only the keywords used by the bundled schemas. It is
not a general-purpose JSON Schema implementation or a plugin execution API.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from operon.errors import ValidationError


def _reject_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value}")


def _parse_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"JSON number is not finite: {value}")
    return result


def decode_json(text: str) -> Any:
    """Decode plugin JSON with finite numeric values only."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_parse_float)


def read_json(source: str | Path) -> Any:
    """Read JSON and report malformed input as a domain validation error."""
    try:
        return decode_json(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValidationError(f"{source}: invalid JSON: {exc}") from exc


def validate_payload(payload: Any, name: str) -> None:
    """Validate against one shipped contract (measure, hits or events)."""
    if name not in {"measure", "hits", "events"}:
        raise ValidationError(f"unknown file contract {name!r}")
    path = (
        Path(__file__).parent.parent / "qc" / "measure.schema.json"
        if name == "measure"
        else Path(__file__).parent / f"{name}.schema.json"
    )
    _validate(payload, read_json(path), "$", name)


def _finite_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate(value: Any, schema: dict[str, Any], path: str, name: str) -> None:
    def fail(message: str) -> None:
        raise ValidationError(f"{name} payload {path}: {message}")

    for branch in schema.get("allOf", []):
        _validate(value, branch, path, name)
    if "if" in schema:
        try:
            _validate(value, schema["if"], path, name)
        except ValidationError:
            pass
        else:
            _validate(value, schema["then"], path, name)
    types = schema.get("type", [])
    if isinstance(types, str):
        types = [types]
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": _finite_number(value),
        "null": value is None,
    }
    if types and not any(matches[kind] for kind in types):
        fail(f"expected {' or '.join(types)}")
    if "const" in schema and value != schema["const"]:
        fail(f"unsupported value {value!r}; expected {schema['const']!r}")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            fail(f"missing fields {sorted(missing)}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}", name)
            elif additional is False:
                fail(f"unknown field {key!r}")
            elif isinstance(additional, dict):
                _validate(item, additional, f"{path}.{key}", name)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            fail("too few items")
        for index, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{index}]", name)
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            fail("string is empty")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            fail("string does not match the required pattern")
    elif type(value) in {int, float}:
        if "minimum" in schema and value < schema["minimum"]:
            fail(f"number must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            fail(f"number must be <= {schema['maximum']}")

"""Validate model JSON output against the Gemini responseSchema subset we use."""
from __future__ import annotations

import json
import re
from typing import Any

_TYPES = {
    "OBJECT": dict, "ARRAY": list, "STRING": str, "INTEGER": int, "NUMBER": (int, float), "BOOLEAN": bool,
}


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = str(schema.get("type", "")).upper()
    if expected:
        py = _TYPES.get(expected)
        if py is None:
            return [f"{path}: unsupported schema type {expected}"]
        if expected in ("INTEGER", "NUMBER") and isinstance(value, bool):
            return [f"{path}: expected {expected}, got boolean"]
        if not isinstance(value, py):
            if expected == "INTEGER" and isinstance(value, float) and value.is_integer():
                pass
            elif value is None and schema.get("nullable"):
                return []
            else:
                return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if expected == "OBJECT":
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required '{key}'")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
    elif expected == "ARRAY" and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
    return errors


def parse_json(text: str) -> Any:
    """Parse JSON, tolerating code fences or prose around a single JSON value."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON found in model output")

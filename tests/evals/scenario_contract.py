from __future__ import annotations

import json
import re
from typing import Any


class ScenarioContractError(ValueError):
    """A JSON value does not conform to the supported schema-v1 vocabulary."""


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]


def validate_against_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the deliberately small JSON-schema subset used by eval v1."""

    if "type" in schema and not _is_type(value, schema["type"]):
        raise ScenarioContractError(f"{path}: expected {schema['type']}")
    if "const" in schema and value != schema["const"]:
        raise ScenarioContractError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ScenarioContractError(f"{path}: value is not in enum")
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                validate_against_schema(value, candidate, path)
            except ScenarioContractError:
                continue
            matches += 1
        if matches != 1:
            raise ScenarioContractError(f"{path}: expected exactly one allowed shape")
        return
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ScenarioContractError(f"{path}: string is too short")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ScenarioContractError(f"{path}: string does not match pattern")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ScenarioContractError(f"{path}: array has too few items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                raise ScenarioContractError(f"{path}: array items are not unique")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_against_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        required = set(schema.get("required", ()))
        missing = required - value.keys()
        if missing:
            raise ScenarioContractError(f"{path}: missing {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = value.keys() - properties.keys()
            if extra:
                raise ScenarioContractError(f"{path}: unexpected {', '.join(sorted(extra))}")
        for key, child in value.items():
            if key in properties:
                validate_against_schema(child, properties[key], f"{path}.{key}")

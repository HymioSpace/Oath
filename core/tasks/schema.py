"""Shared task schemas for model tools and Storage validation."""

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

DEFINITIONS = json.loads(Path(__file__).with_name("contracts.json").read_text())


def schema(name: str) -> dict:
    if name not in DEFINITIONS:
        raise ValueError(f"Unknown task contract: {name}")
    reachable = {}

    def visit(value):
        if isinstance(value, dict):
            target = value.get("$ref", "").removeprefix("#/$defs/")
            if target in DEFINITIONS and target not in reachable:
                reachable[target] = DEFINITIONS[target]
                visit(DEFINITIONS[target])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit({"$ref": f"#/$defs/{name}"})
    return {"type": "object", "$ref": f"#/$defs/{name}", "$defs": reachable}


def validate(name: str, value: object) -> None:
    Draft202012Validator(schema(name), format_checker=FormatChecker()).validate(value)

#!/usr/bin/env python3
"""
Generate TypeScript interfaces from Xueban runtime Pydantic contracts.

Usage:
    python scripts/generate_ts_types.py

Output:
    src/core/types/workflow.ts

Fields are kept in snake_case to match JSON payloads.
camelCase conversion is handled by src/adapter/caseConverter.ts at the fetch boundary.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from xueban.runtime.contracts import (  # noqa: E402
    ArtifactRef,
    BlockDef,
    CheckpointRef,
    HandoffRef,
    IterationRecord,
    LaneDef,
    LoopFrame,
    LoopRecord,
    MemoryEvent,
    RunRecord,
    StageDef,
    StepRecord,
    TaskRecord,
    WorkflowAssemblySpec,
    WorkflowDefaults,
    WorkflowPolicyMatrixSpec,
)

# 7 core runtime objects + Story 3.4 assembly spec models (AR16).
# Supporting types (WritebackRef, CheckpointState, …) are pulled in automatically
# via each model's $defs when their JSON Schema is generated.
CORE_MODELS = [
    TaskRecord,
    RunRecord,
    StepRecord,
    ArtifactRef,
    CheckpointRef,
    MemoryEvent,
    HandoffRef,
    # Story 3.4 assembly spec types (AR16: Pydantic → TS sync)
    BlockDef,
    LaneDef,
    StageDef,
    WorkflowDefaults,
    WorkflowPolicyMatrixSpec,
    WorkflowAssemblySpec,
    # Loop Engine types (LE-0: cybernetic loop / PDCA fractal)
    LoopFrame,
    IterationRecord,
    LoopRecord,
]

OUTPUT_PATH = PROJECT_ROOT / "src" / "core" / "types" / "workflow.ts"


def _ts_type(node: dict[str, Any], defs: dict[str, Any]) -> str:
    """Recursively convert a JSON Schema node to a TypeScript type string."""
    if not node:
        return "unknown"

    # $ref — resolve to the referenced interface name
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]

    # anyOf — union type (covers Optional[T] → T | null)
    if "anyOf" in node:
        parts = [_ts_type(s, defs) for s in node["anyOf"]]
        unique: list[str] = []
        seen: set[str] = set()
        for p in parts:
            if p not in seen:
                unique.append(p)
                seen.add(p)
        return " | ".join(unique)

    # allOf — single-item wrapper is common in Pydantic v2 for constrained fields
    if "allOf" in node:
        items = node["allOf"]
        if not items:
            return "unknown"
        if len(items) == 1:
            return _ts_type(items[0], defs)
        return " & ".join(_ts_type(s, defs) for s in items)

    # enum — Literal union (e.g. Literal["a", "b"])
    if "enum" in node:
        return " | ".join(json.dumps(v) for v in node["enum"])

    # const — single Literal value
    if "const" in node:
        return json.dumps(node["const"])

    t = node.get("type")

    if t == "null":
        return "null"
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"

    if t == "array":
        items = node.get("items")
        if not items or isinstance(items, bool):
            return "unknown[]"
        item_t = _ts_type(items, defs)
        # Wrap union in parens for unambiguous array notation: (A | B)[]
        if " | " in item_t:
            return f"({item_t})[]"
        return f"{item_t}[]"

    if t == "object" or "properties" in node or "additionalProperties" in node:
        add_props = node.get("additionalProperties")
        properties = node.get("properties", {})

        if not properties:
            if add_props and isinstance(add_props, dict):
                value_t = _ts_type(add_props, defs)
                return f"Record<string, {value_t}>"
            return "Record<string, unknown>"

        # Inline object — emit as an anonymous object literal
        req = set(node.get("required", []))
        fields = []
        for prop, schema in properties.items():
            opt = "" if prop in req else "?"
            fields.append(f"{prop}{opt}: {_ts_type(schema, defs)}")
        return "{ " + "; ".join(fields) + " }"

    logger.warning("Unknown JSON Schema node mapped to 'unknown': %s", node)
    return "unknown"


def _render_interface(name: str, schema: dict[str, Any], defs: dict[str, Any]) -> str:
    """Render a named TypeScript interface from a JSON Schema object definition."""
    lines = [f"export interface {name} {{"]
    properties = schema.get("properties", {})
    required_set = set(schema.get("required", []))

    for prop_name, prop_schema in properties.items():
        opt = "" if prop_name in required_set else "?"
        ts_t = _ts_type(prop_schema, defs)
        lines.append(f"  {prop_name}{opt}: {ts_t};")

    lines.append("}")
    return "\n".join(lines)


def generate(output_path: Path = OUTPUT_PATH) -> None:
    """Collect JSON schemas from CORE_MODELS, then write TypeScript interfaces."""
    all_defs: dict[str, Any] = {}
    def_origins: dict[str, str] = {}
    root_names: list[str] = []

    for model in CORE_MODELS:
        schema = model.model_json_schema()
        name = schema.get("title", model.__name__)
        root_names.append(name)

        # Extract nested $defs (WritebackRef, CheckpointState, etc.)
        for def_name, def_schema in schema.pop("$defs", {}).items():
            if def_name in all_defs:
                if all_defs[def_name] != def_schema:
                    logger.warning(
                        "$defs name collision for '%s': keeping definition from %s, "
                        "discarding differing definition from %s",
                        def_name,
                        def_origins.get(def_name, "unknown"),
                        name,
                    )
            else:
                all_defs[def_name] = def_schema
                def_origins[def_name] = name

        # Register the root model itself
        if name not in all_defs:
            all_defs[name] = schema

    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "// AUTO-GENERATED — DO NOT EDIT. Source: xueban/runtime/contracts.py",
        "// Run `python scripts/generate_ts_types.py` to regenerate after modifying contracts.py",
        "",
    ]

    generated: set[str] = set()

    def emit(name: str) -> None:
        if name in generated:
            return
        schema = all_defs.get(name)
        if schema is None:
            return
        if schema.get("type") == "object" or "properties" in schema:
            lines.append(_render_interface(name, schema, all_defs))
            lines.append("")
        generated.add(name)

    # Emit dependency types first (WritebackRef, CheckpointState, …), then root models
    for name in list(all_defs):
        if name not in root_names:
            emit(name)
    for name in root_names:
        emit(name)

    output_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        display = output_path.relative_to(PROJECT_ROOT)
    except ValueError:
        display = output_path
    print(f"Generated {display} ({len(generated)} interfaces)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    generate()

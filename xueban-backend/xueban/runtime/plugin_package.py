"""``.sfplugin`` plugin package format — minimal v1 (adoption #5).

A plugin package is a zip distributing Xueban extensions WITHOUT repo
access (gemini-extensions-inspired). v1 declares what it provides; the
validator checks structure and spec shapes:

    sfplugin.json      manifest (format/version/name/provides)
    provider_presets/*.yaml   zero-code CLI agent presets (existing format)
    loop_presets.yaml         loop body templates (SF_LOOP_PRESETS format)
    README.md                 usage + safety notes

`provides` declares intent so consumers can gate installation:

    {"provider_presets": ["codex-mini"], "loop_presets": ["review_cycle"],
     "executors": ["my_kind"], "sensors": ["my_sensor"]}

v1 validates only — applying (merging presets, registering loop templates)
is the follow-up once the format settles.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict, List

PLUGIN_FORMAT = "sfplugin"
PLUGIN_FORMAT_VERSION = "1.0"

_REQUIRED_MANIFEST_FIELDS = ("name", "version")


def validate_plugin_package(zip_bytes: bytes) -> Dict[str, Any]:
    """Validate a .sfplugin zip: {"valid", "errors", "warnings", "manifest"}."""
    errors: List[str] = []
    warnings: List[str] = []

    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        return {"valid": False, "errors": [f"not a zip: {exc}"], "warnings": [], "manifest": None}

    names = set(archive.namelist())

    if "sfplugin.json" not in names:
        return {"valid": False, "errors": ["missing sfplugin.json"], "warnings": [], "manifest": None}
    try:
        manifest = json.loads(archive.read("sfplugin.json"))
    except Exception as exc:  # noqa: BLE001 — any parse failure is data
        return {"valid": False, "errors": [f"sfplugin.json unreadable: {exc}"], "warnings": [], "manifest": None}
    if not isinstance(manifest, dict):
        return {"valid": False, "errors": ["sfplugin.json must be an object"], "warnings": [], "manifest": None}

    if manifest.get("format") not in (None, PLUGIN_FORMAT):
        errors.append(f"unsupported format {manifest.get('format')!r} (expected {PLUGIN_FORMAT!r})")
    for field in _REQUIRED_MANIFEST_FIELDS:
        if not isinstance(manifest.get(field), str) or not manifest.get(field):
            errors.append(f"manifest.{field} must be a non-empty string")

    provides = manifest.get("provides", {})
    if not isinstance(provides, dict):
        errors.append("manifest.provides must be an object")
        provides = {}

    # Declared provider presets must exist in the archive.
    declared_presets = provides.get("provider_presets", [])
    if not isinstance(declared_presets, list):
        errors.append("provides.provider_presets must be a list")
        declared_presets = []
    preset_files = {n for n in names if n.startswith("provider_presets/")}
    if declared_presets and not preset_files:
        warnings.append("provides.provider_presets declared but no provider_presets/ files present")

    # Declared loop presets must parse against the preset spec shape.
    declared_loops = provides.get("loop_presets", [])
    if not isinstance(declared_loops, list):
        errors.append("provides.loop_presets must be a list")
        declared_loops = []
    if declared_loops:
        if "loop_presets.yaml" not in names:
            errors.append("provides.loop_presets declared but loop_presets.yaml missing")
        else:
            import yaml

            try:
                doc = yaml.safe_load(archive.read("loop_presets.yaml")) or {}
            except Exception as exc:  # noqa: BLE001
                errors.append(f"loop_presets.yaml unreadable: {exc}")
                doc = {}
            presets = doc.get("presets") if isinstance(doc, dict) else None
            if not isinstance(presets, dict):
                errors.append("loop_presets.yaml must contain a top-level 'presets' mapping")
            else:
                from xueban.runtime.loop_engine import PRESET_PHASE_TYPES, _normalize_preset_spec

                for name in declared_loops:
                    if name not in presets:
                        errors.append(f"declared loop preset {name!r} not in loop_presets.yaml")
                for name, spec in presets.items():
                    try:
                        normalized = _normalize_preset_spec(str(name), spec if isinstance(spec, dict) else {})
                        for phase in normalized["phases"]:
                            if phase["type"] not in PRESET_PHASE_TYPES:
                                raise ValueError(f"phase {phase['id']!r} bad type {phase['type']!r}")
                    except ValueError as exc:
                        errors.append(f"loop preset {name!r}: {exc}")

    # Declared executors: kinds may be novel (that's the point) — just warn
    # that registration code is required at apply time.
    declared_executors = provides.get("executors", [])
    if isinstance(declared_executors, list) and declared_executors:
        warnings.append(
            "provides.executors declared; applying requires registering a BaseExecutor "
            "for each kind (register_executor_kind opens the schema)"
        )

    if "README.md" not in names:
        warnings.append("missing README.md (usage/safety notes recommended)")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "manifest": manifest,
    }


__all__ = ["PLUGIN_FORMAT", "PLUGIN_FORMAT_VERSION", "validate_plugin_package"]

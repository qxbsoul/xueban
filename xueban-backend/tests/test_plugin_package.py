""".sfplugin package validator tests (adoption #5, minimal v1)."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

from xueban.runtime.plugin_package import validate_plugin_package


def _zip(files: dict) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


_VALID_LOOP_PRESETS = """
presets:
  review_cycle:
    description: draft → review → fix
    phases:
      - {id: draft,  type: task}
      - {id: review, type: control.sensor}
      - {id: fix,    type: control.controller}
"""


def test_valid_plugin_package():
    pkg = _zip(
        {
            "sfplugin.json": json.dumps(
                {
                    "format": "sfplugin",
                    "format_version": "1.0",
                    "name": "review-kit",
                    "version": "0.1.0",
                    "provides": {"loop_presets": ["review_cycle"], "provider_presets": ["codex-mini"]},
                }
            ),
            "loop_presets.yaml": _VALID_LOOP_PRESETS,
            "provider_presets/codex-mini.yaml": "command: codex\n",
            "README.md": "demo kit",
        }
    )
    report = validate_plugin_package(pkg)
    assert report["valid"] is True, report["errors"]
    assert report["manifest"]["name"] == "review-kit"


def test_missing_manifest_rejected():
    report = validate_plugin_package(_zip({"random.txt": "x"}))
    assert report["valid"] is False
    assert any("sfplugin.json" in e for e in report["errors"])


def test_declared_loop_preset_missing_from_yaml():
    pkg = _zip(
        {
            "sfplugin.json": json.dumps(
                {"name": "bad", "version": "0.1", "provides": {"loop_presets": ["ghost"]}}
            ),
            "loop_presets.yaml": _VALID_LOOP_PRESETS,
        }
    )
    report = validate_plugin_package(pkg)
    assert report["valid"] is False
    assert any("ghost" in e for e in report["errors"])


def test_bad_loop_preset_phase_type_rejected():
    pkg = _zip(
        {
            "sfplugin.json": json.dumps(
                {"name": "bad-phase", "version": "0.1", "provides": {"loop_presets": ["x"]}}
            ),
            "loop_presets.yaml": (
                "presets:\n  x:\n    phases:\n      - {id: a, type: control.loop}\n"
            ),
        }
    )
    report = validate_plugin_package(pkg)
    assert report["valid"] is False
    assert any("type must be one of" in e for e in report["errors"])


def test_not_a_zip():
    report = validate_plugin_package(b"definitely not a zip")
    assert report["valid"] is False

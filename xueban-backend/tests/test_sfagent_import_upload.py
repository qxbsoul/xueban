"""M9 tests: .sfagent export/import roundtrip + import HTTP endpoint.

(0G bridge upload tests removed with the 0G teardown — ROADMAP 3.5, 2026-08-17;
offline export/import are the supported .sfagent paths.)
"""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from xueban.runtime.agent_bundle import (
    export_sfagent_zip,
    import_sfagent_zip,
    read_sfagent_zip,
)

_AGENT = {
    "id": "checker-beta",
    "name": "Checker Beta",
    "role": "Checker",
    "capabilities": ["diagnostics-sensor"],
    "skills": [{"name": "lint", "purpose": "count issues"}],
}


def test_bundle_roundtrip_import(tmp_path):
    zip_bytes = export_sfagent_zip(_AGENT)
    parsed = read_sfagent_zip(zip_bytes)
    assert parsed["manifest"]["agent_id"] == "checker-beta"

    record = import_sfagent_zip(zip_bytes, tmp_path / "agents")
    assert record["agent_id"] == "checker-beta"
    assert record["role"] == "Checker"
    stored = json.loads((tmp_path / "agents" / "checker-beta.json").read_text(encoding="utf-8"))
    assert stored["content_id"].startswith("sha256:")
    assert stored["skills"][0]["name"] == "lint"


def test_read_bundle_rejects_non_bundle_zip(tmp_path):
    import zipfile
    from io import BytesIO

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("random.txt", "not a bundle")
    import pytest

    with pytest.raises(ValueError, match="sfagent.json"):
        read_sfagent_zip(buf.getvalue())


def test_http_import_endpoint(tmp_path, monkeypatch):
    import xueban.api.agents as agents_mod

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", agents_dir)

    from xueban.server import app

    client = TestClient(app)
    zip_b64 = base64.b64encode(export_sfagent_zip(_AGENT)).decode("ascii")

    imported = client.post("/workflow/sfagent/import", json={"zip_b64": zip_b64})
    assert imported.status_code == 200, imported.text
    data = imported.json()["data"]
    assert data["agent_id"] == "checker-beta"
    assert (agents_dir / "checker-beta.json").exists()

"""Backend tests for Story 3.6.8 — POST /templates/custom, GET /templates, GET /templates/{id}.

No mocking (per .claude/rules/test-execution.md).
"""

from __future__ import annotations

import pathlib
import pytest
import yaml
from fastapi.testclient import TestClient

from xueban.server import app

client = TestClient(app)

# ---------------------------------------------------------------------------
# Minimal valid template YAML
# ---------------------------------------------------------------------------

MINIMAL_YAML = """\
template_id: test-import-alpha
version: "0.1"
name: Test Import Alpha
description: ""
user_role: Founder
default_ops_room_name: ""
brief_board_alias: BriefBoard
theme_color: "#A78BFA"
agent_roster: []
group_roster: []
parameters:
  goal:
    type: string
    required: true
agents:
  - id: agent_1
    ref: agent_1
flow:
  entrypoint: agent_1
  edges:
    - from: agent_1
      to: END
      type: final
policy_matrix:
  agents: {}
stages: []
defaults: {}
metadata: {}
"""


@pytest.fixture(autouse=True)
def cleanup_custom_templates():
    """Remove any custom template files created during tests."""
    custom_dir = pathlib.Path("templates/custom")
    created: list[pathlib.Path] = []

    yield created  # tests append paths they create

    for p in created:
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# T1: Import valid YAML → 200 + file on disk
# ---------------------------------------------------------------------------

def test_import_valid_yaml_creates_file(cleanup_custom_templates):
    resp = client.post("/templates/custom", json={"yaml_text": MINIMAL_YAML})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_id"] == "test-import-alpha"
    assert body["source"] == "custom"

    created_path = pathlib.Path("templates/custom/test-import-alpha.yaml")
    assert created_path.exists(), "Custom template file should be persisted"
    cleanup_custom_templates.append(created_path)


# ---------------------------------------------------------------------------
# T2: Import invalid YAML (missing template_id) → 422
# ---------------------------------------------------------------------------

def test_import_missing_required_field_returns_422():
    bad_yaml = """\
version: "0.1"
name: No ID Here
agents:
  - id: a
    ref: a
flow:
  entrypoint: a
  edges:
    - from: a
      to: END
      type: final
"""
    resp = client.post("/templates/custom", json={"yaml_text": bad_yaml})
    assert resp.status_code == 422
    body = resp.json()
    # Pydantic v2 errors list
    errors = body if isinstance(body, list) else body.get("detail", [])
    locs = [".".join(str(l) for l in e["loc"]) for e in errors if e.get("loc")]
    assert any("template_id" in loc for loc in locs), f"Expected template_id in error locs, got: {locs}"


# ---------------------------------------------------------------------------
# T3: Import template_id that conflicts with seed → 409
# ---------------------------------------------------------------------------

def test_import_seed_conflict_returns_409():
    seed_conflict_yaml = MINIMAL_YAML.replace("test-import-alpha", "solo-company")
    resp = client.post("/templates/custom", json={"yaml_text": seed_conflict_yaml})
    assert resp.status_code == 409
    body = resp.json()
    conflict = body.get("detail", body)
    assert conflict.get("existing_source") == "seed"


# ---------------------------------------------------------------------------
# T4: Import template_id that conflicts with existing custom → 409
# ---------------------------------------------------------------------------

def test_import_custom_conflict_returns_409(cleanup_custom_templates):
    # First import
    resp1 = client.post("/templates/custom", json={"yaml_text": MINIMAL_YAML})
    assert resp1.status_code == 200
    cleanup_custom_templates.append(pathlib.Path("templates/custom/test-import-alpha.yaml"))

    # Second import with same template_id
    resp2 = client.post("/templates/custom", json={"yaml_text": MINIMAL_YAML})
    assert resp2.status_code == 409
    body = resp2.json()
    conflict = body.get("detail", body)
    assert conflict.get("existing_source") == "custom"


# ---------------------------------------------------------------------------
# T5: overrides.template_id rename → file created under new name
# ---------------------------------------------------------------------------

def test_override_template_id_renames(cleanup_custom_templates):
    original_yaml = MINIMAL_YAML  # template_id = test-import-alpha
    resp = client.post(
        "/templates/custom",
        json={"yaml_text": original_yaml, "overrides": {"template_id": "test-renamed-beta"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_id"] == "test-renamed-beta"

    renamed_path = pathlib.Path("templates/custom/test-renamed-beta.yaml")
    assert renamed_path.exists(), "Renamed template file should exist"
    cleanup_custom_templates.append(renamed_path)

    # Original name should NOT exist
    assert not pathlib.Path("templates/custom/test-import-alpha.yaml").exists()


# ---------------------------------------------------------------------------
# T6: GET /templates returns seed + custom with correct source field
# ---------------------------------------------------------------------------

def test_list_templates_includes_seed_and_custom(cleanup_custom_templates):
    # Import a custom template first
    resp_import = client.post("/templates/custom", json={"yaml_text": MINIMAL_YAML})
    assert resp_import.status_code == 200
    cleanup_custom_templates.append(pathlib.Path("templates/custom/test-import-alpha.yaml"))

    resp = client.get("/templates")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert len(items) > 0

    sources = {item["source"] for item in items}
    assert "seed" in sources, "Should include seed templates"
    assert "custom" in sources, "Should include custom template just imported"

    custom_ids = [item["template_id"] for item in items if item["source"] == "custom"]
    assert "test-import-alpha" in custom_ids


# ---------------------------------------------------------------------------
# T7: GET /templates/{id} → 404 for unknown id
# ---------------------------------------------------------------------------

def test_get_template_not_found():
    resp = client.get("/templates/this-does-not-exist-xyz")
    assert resp.status_code == 404

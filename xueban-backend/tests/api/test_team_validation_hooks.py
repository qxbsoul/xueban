"""N1/002 — Team validation-hooks endpoint tests.

Mirrors the layout of `test_team_policy_workflow.py`. Covers:
  - GET  /api/teams/{id}/validation-hooks    → empty list (legacy / new team)
  - PUT  /api/teams/{id}/validation-hooks    → persists, round-trips via GET
  - PUT  overwrites prior list (whole-list replacement)
  - 404 on both verbs for non-existent team
  - 422 on invalid schema (bad kind, missing config block, duplicate ids)
  - PM defaults honoured (max_retries=0 / expose_error_details=false)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from xueban.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import json

    import xueban.api.agents as agents_mod
    import xueban.api.teams as teams_mod

    monkeypatch.setattr(teams_mod, "_TEAMS_DIR", tmp_path / "teams")
    # D2 write-time validation rejects teams referencing non-existent agents —
    # seed the ids these tests use into an isolated agent store.
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agents_mod, "_AGENTS_DIR", agents_dir)
    for aid in ("agent-a", "agent-b"):
        (agents_dir / f"{aid}.json").write_text(
            json.dumps({"agent_id": aid, "name": aid}), encoding="utf-8"
        )
    return TestClient(app)


@pytest.fixture
def team_id(client):
    r = client.post("/api/teams", json={
        "name": "Hook Test Team",
        "agent_ids": ["agent-a"],
    })
    assert r.status_code == 201
    return r.json()["data"]["team_id"]


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestValidationHooksRoundtrip:
    def test_get_empty_on_new_team(self, client, team_id):
        """AC1 — backwards compatible: team without the field returns []."""
        r = client.get(f"/api/teams/{team_id}/validation-hooks")
        assert r.status_code == 200
        assert r.json()["data"] == {"validation_hooks": []}

    def test_put_then_get_shell_hook(self, client, team_id):
        body = {
            "validation_hooks": [
                {
                    "id": "tsc-must-pass",
                    "kind": "shell",
                    "on_fail": "retry",
                    "max_retries": 1,
                    "timeout_ms": 60000,
                    "description": "TypeScript must compile",
                    "shell": {
                        "cmd": ["pnpm", "tsc", "--noEmit"],
                        "cwd": "${workspace}",
                        "env": {"NODE_OPTIONS": "--max-old-space-size=2048"},
                        "success_when": {"exit_code": 0},
                    },
                }
            ]
        }
        r = client.put(f"/api/teams/{team_id}/validation-hooks", json=body)
        assert r.status_code == 200, r.text
        # Round-trip through GET
        g = client.get(f"/api/teams/{team_id}/validation-hooks")
        assert g.status_code == 200
        hooks = g.json()["data"]["validation_hooks"]
        assert len(hooks) == 1
        assert hooks[0]["id"] == "tsc-must-pass"
        assert hooks[0]["kind"] == "shell"
        assert hooks[0]["on_fail"] == "retry"
        assert hooks[0]["max_retries"] == 1
        assert hooks[0]["shell"]["cmd"] == ["pnpm", "tsc", "--noEmit"]

    def test_put_webhook_hook(self, client, team_id):
        body = {
            "validation_hooks": [
                {
                    "id": "deploy-ping",
                    "kind": "webhook",
                    "on_fail": "blocker",
                    "webhook": {
                        "url": "https://ci/validate",
                        "method": "POST",
                        "headers": {"Authorization": "Bearer ${credential.ci_token}"},
                        "body_template": '{"team_id":"${team_id}"}',
                        "success_when": {
                            "status_code": 200,
                            "json_path": "$.passed",
                            "equals": True,
                        },
                    },
                }
            ]
        }
        r = client.put(f"/api/teams/{team_id}/validation-hooks", json=body)
        assert r.status_code == 200, r.text
        hooks = client.get(f"/api/teams/{team_id}/validation-hooks").json()["data"][
            "validation_hooks"
        ]
        assert hooks[0]["webhook"]["success_when"]["equals"] is True

    def test_put_builtin_hook(self, client, team_id):
        body = {
            "validation_hooks": [
                {
                    "id": "files-exist",
                    "kind": "builtin",
                    "on_fail": "warn",
                    "builtin": {
                        "name": "file-exists",
                        "args": {"paths": ["architecture.md", "prd.md"]},
                    },
                }
            ]
        }
        r = client.put(f"/api/teams/{team_id}/validation-hooks", json=body)
        assert r.status_code == 200, r.text

    def test_put_overwrites_prior_list(self, client, team_id):
        first = {
            "validation_hooks": [
                {
                    "id": "h1",
                    "kind": "builtin",
                    "builtin": {"name": "file-exists"},
                }
            ]
        }
        second = {
            "validation_hooks": [
                {
                    "id": "h2",
                    "kind": "builtin",
                    "builtin": {"name": "file-exists"},
                },
                {
                    "id": "h3",
                    "kind": "builtin",
                    "builtin": {"name": "file-exists"},
                },
            ]
        }
        client.put(f"/api/teams/{team_id}/validation-hooks", json=first)
        client.put(f"/api/teams/{team_id}/validation-hooks", json=second)
        hooks = client.get(f"/api/teams/{team_id}/validation-hooks").json()["data"][
            "validation_hooks"
        ]
        ids = sorted(h["id"] for h in hooks)
        assert ids == ["h2", "h3"]

    def test_put_empty_list(self, client, team_id):
        r = client.put(
            f"/api/teams/{team_id}/validation-hooks",
            json={"validation_hooks": []},
        )
        assert r.status_code == 200
        assert r.json()["data"] == {"validation_hooks": []}

    def test_pm_defaults_persisted(self, client, team_id):
        """PM Q12.2: max_retries default 0. PM Q12.3: expose_error_details default false."""
        body = {
            "validation_hooks": [
                {
                    "id": "minimal",
                    "kind": "builtin",
                    "builtin": {"name": "file-exists"},
                }
            ]
        }
        client.put(f"/api/teams/{team_id}/validation-hooks", json=body)
        hook = client.get(f"/api/teams/{team_id}/validation-hooks").json()["data"][
            "validation_hooks"
        ][0]
        assert hook["max_retries"] == 0  # PM Q12.2
        assert hook["expose_error_details"] is False  # PM Q12.3
        assert hook["on_fail"] == "blocker"
        assert hook["enabled"] is True
        assert hook["timeout_ms"] == 60_000


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestValidationHooksErrors:
    def test_get_404(self, client):
        r = client.get("/api/teams/team-nonexistent/validation-hooks")
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "TEAM_NOT_FOUND"

    def test_put_404(self, client):
        r = client.put(
            "/api/teams/team-ghost/validation-hooks",
            json={"validation_hooks": []},
        )
        assert r.status_code == 404

    def test_put_422_bad_kind(self, client, team_id):
        r = client.put(
            f"/api/teams/{team_id}/validation-hooks",
            json={
                "validation_hooks": [
                    {"id": "x", "kind": "bogus", "builtin": {"name": "n"}}
                ]
            },
        )
        assert r.status_code == 422

    def test_put_422_missing_config_block(self, client, team_id):
        """kind=shell but no shell: block → 422 (schema discriminator)."""
        r = client.put(
            f"/api/teams/{team_id}/validation-hooks",
            json={"validation_hooks": [{"id": "x", "kind": "shell"}]},
        )
        assert r.status_code == 422

    def test_put_422_duplicate_ids(self, client, team_id):
        r = client.put(
            f"/api/teams/{team_id}/validation-hooks",
            json={
                "validation_hooks": [
                    {
                        "id": "dup",
                        "kind": "builtin",
                        "builtin": {"name": "file-exists"},
                    },
                    {
                        "id": "dup",
                        "kind": "builtin",
                        "builtin": {"name": "file-exists"},
                    },
                ]
            },
        )
        assert r.status_code == 422

    def test_put_422_unknown_extra_field(self, client, team_id):
        r = client.put(
            f"/api/teams/{team_id}/validation-hooks",
            json={
                "validation_hooks": [
                    {
                        "id": "x",
                        "kind": "builtin",
                        "builtin": {"name": "file-exists"},
                        "rogue": "field",
                    }
                ]
            },
        )
        assert r.status_code == 422

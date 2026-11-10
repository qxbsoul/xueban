"""Story 12.5 — Agent Pack Registry API tests.

Covers:
  AC2: registry loads every indexed pack from templates/agent-packs/
  AC3: POST /install — creates blueprint, persists record
  AC4: version comparison + has_update status
  AC5: signature verification (no-sig allowed, bad-sig blocked)
  AC6: GET /installed
  AC7: GET /packs list + filter
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from xueban.server import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh client with isolated registry service and temp packs dir."""
    import xueban.services.registry_service as reg_mod

    # Reset singleton
    reg_mod.reset_registry_service()

    # Create minimal packs dir in tmp_path
    packs_root = tmp_path / "agent-packs"
    packs_root.mkdir()
    (packs_root / "installed").mkdir()

    # Write two packs
    for pack_id, version, kind in [
        ("test-coder", "1.0.0", "acp"),
        ("test-writer", "2.0.0", "api"),
    ]:
        pack_dir = packs_root / pack_id
        pack_dir.mkdir()
        manifest = {
            "id": pack_id,
            "version": version,
            "name": f"Test {pack_id.title()}",
            "description": f"Description for {pack_id}",
            "author": "Test",
            "soul": f"You are {pack_id}.",
            "kind": kind,
        }
        (pack_dir / "agent-manifest.yaml").write_text(yaml.dump(manifest), encoding="utf-8")

    index = {
        "packs": [
            {"id": "test-coder", "path": "test-coder/agent-manifest.yaml", "tags": ["coding"]},
            {"id": "test-writer", "path": "test-writer/agent-manifest.yaml", "tags": ["writing"]},
        ]
    }
    (packs_root / "registry-index.yaml").write_text(yaml.dump(index), encoding="utf-8")

    # Patch installed dir path
    monkeypatch.setattr(reg_mod, "_INSTALLED_DIR", packs_root / "installed")

    # Override get_registry_service to use our packs root
    svc = reg_mod.RegistryService()
    svc.load(packs_root)
    reg_mod._service = svc

    return TestClient(app)


# ---------------------------------------------------------------------------
# AC7 — List packs
# ---------------------------------------------------------------------------


class TestListPacks:
    def test_returns_all_packs(self, client):
        r = client.get("/api/agents/registry/packs")
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] == 2
        ids = {p["id"] for p in body["data"]}
        assert "test-coder" in ids
        assert "test-writer" in ids

    def test_pack_has_required_fields(self, client):
        r = client.get("/api/agents/registry/packs")
        pack = r.json()["data"][0]
        for field in ("id", "version", "name", "description", "author", "tags",
                      "install_status", "verified"):
            assert field in pack, f"missing field: {field}"

    def test_default_install_status_not_installed(self, client):
        r = client.get("/api/agents/registry/packs")
        for pack in r.json()["data"]:
            assert pack["install_status"] == "not_installed"

    def test_filter_by_q(self, client):
        r = client.get("/api/agents/registry/packs?q=coder")
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()["data"]]
        assert "test-coder" in ids
        assert "test-writer" not in ids

    def test_filter_by_tags(self, client):
        r = client.get("/api/agents/registry/packs?tags=writing")
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()["data"]]
        assert "test-writer" in ids
        assert "test-coder" not in ids


# ---------------------------------------------------------------------------
# AC3 — Install pack
# ---------------------------------------------------------------------------


class TestInstallPack:
    def test_install_returns_201(self, client):
        r = client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        assert r.status_code == 201
        body = r.json()
        assert body["data"]["pack_id"] == "test-coder"
        assert body["data"]["pack_version"] == "1.0.0"
        assert body["data"]["agent_id"].startswith("agent-")
        assert body["data"]["already_installed"] is False

    def test_install_blueprint_has_catalog_source(self, client):
        r = client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        meta = r.json()["data"]["blueprint"]["metadata"]
        assert meta["source"] == "catalog"
        assert meta["pack_id"] == "test-coder"

    def test_install_not_found(self, client):
        r = client.post("/api/agents/registry/packs/install", json={"pack_id": "does-not-exist"})
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "PACK_NOT_FOUND"

    def test_install_same_version_twice_already_installed(self, client):
        client.post("/api/agents/registry/packs/install", json={"pack_id": "test-writer"})
        r2 = client.post("/api/agents/registry/packs/install", json={"pack_id": "test-writer"})
        assert r2.status_code == 201
        assert r2.json()["data"]["already_installed"] is True

    def test_install_updates_install_status_in_list(self, client):
        client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        r = client.get("/api/agents/registry/packs")
        statuses = {p["id"]: p["install_status"] for p in r.json()["data"]}
        assert statuses["test-coder"] == "installed"
        assert statuses["test-writer"] == "not_installed"


# ---------------------------------------------------------------------------
# AC4 — Version management
# ---------------------------------------------------------------------------


class TestVersionManagement:
    def test_has_update_when_registry_newer(self, client, tmp_path, monkeypatch):
        import xueban.services.registry_service as reg_mod

        # Install pack at "old" version
        installed_path = reg_mod._INSTALLED_DIR / "default.json"
        installed_path.write_text(json.dumps({
            "workspace_id": "default",
            "installed": [
                {
                    "pack_id": "test-coder",
                    "pack_version": "0.9.0",  # older than registry "1.0.0"
                    "agent_id": "agent-old",
                    "installed_at": "2026-01-01T00:00:00Z",
                    "verified": False,
                }
            ],
        }), encoding="utf-8")

        r = client.get("/api/agents/registry/packs")
        statuses = {p["id"]: p["install_status"] for p in r.json()["data"]}
        assert statuses["test-coder"] == "has_update"

    def test_installed_when_same_version(self, client):
        import xueban.services.registry_service as reg_mod
        installed_path = reg_mod._INSTALLED_DIR / "default.json"
        installed_path.write_text(json.dumps({
            "workspace_id": "default",
            "installed": [
                {
                    "pack_id": "test-coder",
                    "pack_version": "1.0.0",
                    "agent_id": "agent-same",
                    "installed_at": "2026-01-01T00:00:00Z",
                    "verified": False,
                }
            ],
        }), encoding="utf-8")

        r = client.get("/api/agents/registry/packs")
        statuses = {p["id"]: p["install_status"] for p in r.json()["data"]}
        assert statuses["test-coder"] == "installed"


# ---------------------------------------------------------------------------
# AC5 — Signature verification
# ---------------------------------------------------------------------------


class TestSignatureVerification:
    def test_no_signature_allowed_unverified(self, client):
        # test-coder has no signature → should install OK, verified=False
        r = client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        assert r.status_code == 201
        assert r.json()["data"]["verified"] is False

    def test_bad_signature_returns_400(self, client, monkeypatch):
        import xueban.services.registry_service as reg_mod
        from xueban.contracts.agent_manifest import AgentPackManifest, ManifestSignature

        # Give the manifest a bad signature
        manifest = reg_mod._service._manifests["test-coder"]
        bad_manifest = manifest.model_copy(
            update={"signature": ManifestSignature(algorithm="HMAC-SHA256", value="badhex")}
        )
        reg_mod._service._manifests["test-coder"] = bad_manifest

        # Set a secret so verification is actually done
        monkeypatch.setenv("XUEBAN_PACK_SECRET", "test-secret-value")

        r = client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "MANIFEST_SIGNATURE_INVALID"


# ---------------------------------------------------------------------------
# AC6 — Installed list
# ---------------------------------------------------------------------------


class TestInstalledList:
    def test_empty_by_default(self, client):
        r = client.get("/api/agents/registry/packs/installed")
        assert r.status_code == 200
        assert r.json()["data"] == []
        assert r.json()["meta"]["total"] == 0

    def test_list_after_install(self, client):
        client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        r = client.get("/api/agents/registry/packs/installed")
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1
        assert data[0]["pack_id"] == "test-coder"
        assert "update_available" in data[0]
        assert "verified" in data[0]

    def test_name_enriched_in_installed_list(self, client):
        client.post("/api/agents/registry/packs/install", json={"pack_id": "test-writer"})
        r = client.get("/api/agents/registry/packs/installed")
        rec = r.json()["data"][0]
        assert "name" in rec
        assert rec["name"] == "Test Test-Writer"  # from fixture: f"Test {pack_id.title()}"


# ---------------------------------------------------------------------------
# AC2 — Real registry loads every indexed built-in pack
# ---------------------------------------------------------------------------


class TestRealRegistry:
    def test_real_packs_match_registry_index(self):
        """Built-in manifests and registry-index.yaml must stay in sync."""
        packs_root = Path(__file__).resolve().parents[1] / "templates" / "agent-packs"
        if not packs_root.exists():
            pytest.skip("templates/agent-packs not found")

        index = yaml.safe_load((packs_root / "registry-index.yaml").read_text(encoding="utf-8"))
        entries = index.get("packs") if isinstance(index, dict) else None
        assert isinstance(entries, list) and entries, "registry index must contain packs"
        indexed = {entry["path"] for entry in entries}
        manifests = {
            path.relative_to(packs_root).as_posix()
            for path in packs_root.glob("*/agent-manifest.yaml")
        }
        assert manifests == indexed, (
            f"Registry/manifest mismatch: unregistered={sorted(manifests - indexed)}, "
            f"missing={sorted(indexed - manifests)}"
        )

    def test_real_registry_index_exists(self):
        packs_root = Path(__file__).resolve().parents[1] / "templates" / "agent-packs"
        assert (packs_root / "registry-index.yaml").exists()


# ---------------------------------------------------------------------------
# Security / validation — workspace_id input guard (P11)
# ---------------------------------------------------------------------------


class TestWorkspaceIdValidation:
    def test_list_packs_bad_workspace_id_returns_400(self, client):
        r = client.get("/api/agents/registry/packs?workspace_id=../evil")
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "INVALID_WORKSPACE_ID"

    def test_installed_bad_workspace_id_returns_400(self, client):
        r = client.get("/api/agents/registry/packs/installed?workspace_id=../../etc/passwd")
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "INVALID_WORKSPACE_ID"

    def test_install_bad_workspace_id_returns_422(self, client):
        # InstallRequest.workspace_id has pattern=[a-zA-Z0-9_-]+ → Pydantic 422
        r = client.post(
            "/api/agents/registry/packs/install",
            json={"pack_id": "test-coder", "workspace_id": "../evil"},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# install_cmd awareness — MVP skip documented (P12)
# ---------------------------------------------------------------------------


class TestInstallCmdAwareness:
    def test_install_cmd_non_null_emits_warning(self, client, tmp_path, monkeypatch):
        """When a pack has a non-null install_cmd, response.meta.warnings must include a notice."""
        import xueban.services.registry_service as reg_mod
        from xueban.contracts.agent_manifest import AgentPackManifest

        manifest = reg_mod._service._manifests["test-coder"]
        patched = manifest.model_copy(update={"install_cmd": "pip install something"})
        reg_mod._service._manifests["test-coder"] = patched

        r = client.post("/api/agents/registry/packs/install", json={"pack_id": "test-coder"})
        assert r.status_code == 201
        warnings = r.json()["meta"]["warnings"]
        assert any("install_cmd" in w for w in warnings), (
            "Expected install_cmd warning in response.meta.warnings"
        )

        # Restore
        reg_mod._service._manifests["test-coder"] = manifest

"""Knowledge API endpoint tests — Story 9.1 AC6 + post-review hardening.

Covers:
  - POST /api/knowledge/packs returns pack with status=pending and `{data, meta}` envelope
  - GET /api/knowledge/packs lists packs, exposes meta.skipped (M3)
  - GET /api/knowledge/packs/{pack_id} returns 404 for unknown UUID-hex IDs
  - GET /api/knowledge/packs/{malformed} returns 400 KNOWLEDGE_PACK_INVALID_ID (C4/H2)
  - PATCH /api/knowledge/packs/{pack_id} updates name / description
  - PATCH retrieval_profile.chunk_size auto-resets sources to pending (H5)
  - DELETE /api/knowledge/packs/{pack_id} removes the record (and nested dirs, H7)
  - POST /api/knowledge/packs/{pack_id}/reindex resets sources to pending
  - Server-side corrupted records map to 500 KNOWLEDGE_PACK_INVALID (H3)
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import xueban.api.knowledge as knowledge_api
from xueban.api.knowledge import KnowledgeService
from xueban.server import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_knowledge(tmp_path: Path):
    svc = KnowledgeService(storage_dir=tmp_path / "knowledge")
    knowledge_api.set_service(svc)
    try:
        yield svc
    finally:
        knowledge_api.set_service(KnowledgeService())


def _create_payload(name: str = "kb"):
    return {
        "name": name,
        "description": "test pack",
        "sources": [{"source_type": "text", "source_ref": "hello world"}],
        "citation_required": False,
        "freshness_policy": "on_demand",
    }


def _new_uuid_hex() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


def test_create_pack_returns_envelope_with_pending_status():
    resp = client.post("/api/knowledge/packs", json=_create_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and "meta" in body
    assert body["meta"]["trace_id"]
    # L2: ISO 8601 UTC with `Z` suffix per .claude/rules/datetime.md
    assert body["meta"]["timestamp"].endswith("Z")
    pack = body["data"]
    assert pack["status"] == "pending"
    assert pack["name"] == "kb"
    assert pack["pack_id"]
    # C4: persisted pack_ids are 32-char hex
    assert len(pack["pack_id"]) == 32
    assert pack["sources"][0]["source_type"] == "text"


def test_create_pack_rejects_empty_sources():
    bad = _create_payload()
    bad["sources"] = []
    resp = client.post("/api/knowledge/packs", json=bad)
    assert resp.status_code == 422  # pydantic schema rejection (min_length=1)


# ---------------------------------------------------------------------------
# GET (list + detail)
# ---------------------------------------------------------------------------


def test_list_packs_empty():
    resp = client.get("/api/knowledge/packs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["packs"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["limit"] == 20
    assert body["meta"]["offset"] == 0
    # M3: meta.skipped surfaced even when zero
    assert body["meta"]["skipped"] == 0


def test_list_packs_returns_created_pack():
    client.post("/api/knowledge/packs", json=_create_payload("alpha"))
    client.post("/api/knowledge/packs", json=_create_payload("beta"))
    resp = client.get("/api/knowledge/packs?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["total"] == 2
    assert body["meta"]["skipped"] == 0
    names = {p["name"] for p in body["data"]["packs"]}
    assert names == {"alpha", "beta"}


def test_list_packs_surfaces_skipped_corrupted_records(_isolated_knowledge: KnowledgeService):
    """M3: corrupted pack.json files show up as `meta.skipped` and don't crash."""
    # Drop a valid pack and a corrupted one.
    client.post("/api/knowledge/packs", json=_create_payload("good"))
    bad_dir = _isolated_knowledge._storage_dir / _new_uuid_hex()
    bad_dir.mkdir(parents=True)
    (bad_dir / "pack.json").write_text("{ this is not json", encoding="utf-8")

    resp = client.get("/api/knowledge/packs")
    body = resp.json()
    assert resp.status_code == 200
    assert body["meta"]["total"] == 1  # only the good one
    assert body["meta"]["skipped"] == 1
    assert body["data"]["packs"][0]["name"] == "good"


def test_get_pack_not_found_returns_standard_error_envelope():
    """Valid UUID-hex shape but no record → 404 KNOWLEDGE_PACK_NOT_FOUND."""
    missing = _new_uuid_hex()
    resp = client.get(f"/api/knowledge/packs/{missing}")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "KNOWLEDGE_PACK_NOT_FOUND"
    assert body["error"]["message"]
    assert body["error"]["trace_id"]


def test_get_pack_invalid_id_returns_400():
    """C4/H2: malformed pack_id (non-UUID-hex) → 400 KNOWLEDGE_PACK_INVALID_ID, NOT 404.

    Note: HTTP-layer traversal patterns like `..` / `../etc/passwd` are normalized
    away by Starlette before our handler runs (they 404 at the routing layer).
    The values below are well-formed URL path segments that reach our code and
    must be rejected by the UUID-hex allowlist.
    """
    # Sample of attack-shaped IDs the old `_record_path` would have conflated with NotFound.
    for bad_id in [
        "missing-id",       # contains '-'
        "nope",             # too short
        "abc",              # way too short
        "ABCDEF" * 6,       # uppercase — UUID-hex is lowercase
        "g" * 32,           # not hex chars
        "A" * 31,           # 31 chars, not 32
        "0123456789abcdef" * 2 + "x",  # 33 chars
    ]:
        resp = client.get(f"/api/knowledge/packs/{bad_id}")
        assert resp.status_code == 400, f"expected 400 for {bad_id!r} got {resp.status_code}"
        assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_INVALID_ID"


def test_get_pack_round_trip():
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    resp = client.get(f"/api/knowledge/packs/{pack_id}")
    assert resp.status_code == 200
    pack = resp.json()["data"]
    assert pack["pack_id"] == pack_id
    assert pack["name"] == "kb"


def test_get_pack_corrupted_record_returns_500(_isolated_knowledge: KnowledgeService):
    """H3: a JSON-corrupted pack.json maps to 500 KNOWLEDGE_PACK_INVALID, not 400."""
    pack_id = _new_uuid_hex()
    pack_dir = _isolated_knowledge._storage_dir / pack_id
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.json").write_text("{ broken json", encoding="utf-8")

    resp = client.get(f"/api/knowledge/packs/{pack_id}")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_INVALID"


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


def test_patch_pack_updates_name_and_description():
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    resp = client.patch(
        f"/api/knowledge/packs/{pack_id}",
        json={"name": "renamed", "description": "new desc"},
    )
    assert resp.status_code == 200
    pack = resp.json()["data"]
    assert pack["name"] == "renamed"
    assert pack["description"] == "new desc"


def test_patch_pack_404_on_missing():
    resp = client.patch(f"/api/knowledge/packs/{_new_uuid_hex()}", json={"name": "x"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_NOT_FOUND"


def test_patch_retrieval_profile_chunk_size_triggers_reindex(
    _isolated_knowledge: KnowledgeService,
):
    """H5: changing chunk_size invalidates the persisted index → status flips to pending
    and sources are reset so the BG ingest can re-chunk against the new profile."""
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    # Wait for BG to settle (text source is fast; status will be ready or pending depending
    # on TestClient timing). Force a fresh state by manually saving as ready.
    pack = _isolated_knowledge.load(pack_id)
    from xueban.memory.knowledge_pack import update_pack
    _isolated_knowledge.save(
        update_pack(
            pack,
            status="ready",
            sources=[
                {**s.model_dump(), "ingest_status": "done", "chunk_count": 1}
                for s in pack.sources
            ],
        )
    )

    resp = client.patch(
        f"/api/knowledge/packs/{pack_id}",
        json={"retrieval_profile": {"mode": "semantic", "top_k": 5, "min_confidence": 0.5,
                                     "chunk_size": 256, "overlap": 32}},
    )
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["status"] == "pending"
    assert updated["retrieval_profile"]["chunk_size"] == 256
    assert all(s["ingest_status"] == "pending" for s in updated["sources"])


def test_patch_name_only_does_not_trigger_reindex(_isolated_knowledge: KnowledgeService):
    """H5: name/description changes are no-op for the index — no reindex queued."""
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    pack = _isolated_knowledge.load(pack_id)
    from xueban.memory.knowledge_pack import update_pack
    _isolated_knowledge.save(
        update_pack(
            pack,
            status="ready",
            sources=[
                {**s.model_dump(), "ingest_status": "done", "chunk_count": 1}
                for s in pack.sources
            ],
        )
    )

    resp = client.patch(f"/api/knowledge/packs/{pack_id}", json={"name": "renamed"})
    updated = resp.json()["data"]
    assert updated["status"] == "ready"  # unchanged
    assert updated["sources"][0]["ingest_status"] == "done"


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


def test_delete_pack_removes_record():
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    resp = client.delete(f"/api/knowledge/packs/{pack_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] is True

    # Subsequent GET on the (now-deleted) UUID-hex returns 404, NOT 400.
    follow = client.get(f"/api/knowledge/packs/{pack_id}")
    assert follow.status_code == 404


def test_delete_pack_with_nested_subdirs(_isolated_knowledge: KnowledgeService):
    """H7: shutil.rmtree handles nested cache subdirs cleanly."""
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    pack_dir = _isolated_knowledge.pack_dir(pack_id)
    nested = pack_dir / "cache" / "subdir"
    nested.mkdir(parents=True)
    (nested / "leftover.bin").write_text("payload", encoding="utf-8")

    resp = client.delete(f"/api/knowledge/packs/{pack_id}")
    assert resp.status_code == 200
    assert not pack_dir.exists()


# ---------------------------------------------------------------------------
# REINDEX
# ---------------------------------------------------------------------------


def test_reindex_resets_sources_to_pending():
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    resp = client.post(f"/api/knowledge/packs/{pack_id}/reindex")
    assert resp.status_code == 200
    pack = resp.json()["data"]
    assert pack["status"] == "pending"
    assert pack["sources"][0]["ingest_status"] == "pending"


def test_reindex_invalid_id_returns_400():
    resp = client.post("/api/knowledge/packs/not-a-uuid/reindex")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "KNOWLEDGE_PACK_INVALID_ID"


# ---------------------------------------------------------------------------
# H4: forward-compat persisted record with unknown field
# ---------------------------------------------------------------------------


def test_load_pack_with_unknown_persisted_field_succeeds(
    _isolated_knowledge: KnowledgeService,
):
    """H4: pack.json written by a future version (with extra fields) still loads."""
    created = client.post("/api/knowledge/packs", json=_create_payload()).json()["data"]
    pack_id = created["pack_id"]
    record = _isolated_knowledge.pack_dir(pack_id) / "pack.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["future_field_added_in_9_2"] = {"vector_store_url": "https://qdrant.example/9-1"}
    payload["retrieval_profile"]["future_top_p"] = 0.9
    record.write_text(json.dumps(payload), encoding="utf-8")

    resp = client.get(f"/api/knowledge/packs/{pack_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["pack_id"] == pack_id

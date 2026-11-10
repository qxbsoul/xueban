"""Citation API endpoint tests — Story 9.2 AC3/AC7.

Covers:
  - GET /citations/{run_id} happy path returns {data, meta} envelope with traces
  - GET /citations/{run_id} returns 404 CITATION_NOT_FOUND for unknown run_id
  - GET /citations/{run_id}?node_id=... filters traces to that node
  - GET /citations/{run_id}/export returns full CitationReport in envelope
  - GET /citations/{run_id}/export returns 404 for unknown run_id
  - citation_missing flag is reflected in GET /citations response
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import xueban.runtime.citation_service as citation_svc_module
from xueban.runtime.citation_service import (
    CitationService,
    CitationTrace,
    set_service,
)
from xueban.server import app


client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_citation_svc(tmp_path: Path):
    svc = CitationService(storage_dir=tmp_path / "citations")
    set_service(svc)
    try:
        yield svc
    finally:
        set_service(CitationService())


def _trace(**kwargs) -> CitationTrace:
    defaults = dict(
        pack_id="pack-01",
        source_id="src-01",
        chunk_id="chunk_0",
        excerpt="sample excerpt",
        confidence=0.80,
    )
    defaults.update(kwargs)
    return CitationTrace(**defaults)


# ---------------------------------------------------------------------------
# GET /citations/{run_id}
# ---------------------------------------------------------------------------


def test_list_citations_happy_path(_isolated_citation_svc):
    svc = _isolated_citation_svc
    svc.attach_trace("run-001", "node-a", [_trace()])

    resp = client.get("/citations/run-001")
    assert resp.status_code == 200

    body = resp.json()
    assert "data" in body
    assert "meta" in body
    assert body["data"]["run_id"] == "run-001"
    assert len(body["data"]["traces"]) == 1
    assert body["meta"]["total"] == 1


def test_list_citations_returns_envelope_shape(_isolated_citation_svc):
    svc = _isolated_citation_svc
    svc.attach_trace("run-002", "node-b", [_trace(), _trace(chunk_id="c1")])

    resp = client.get("/citations/run-002")
    body = resp.json()

    assert "trace_id" in body["meta"]
    assert "timestamp" in body["meta"]
    assert body["meta"]["total"] == 2


def test_list_citations_404_for_unknown_run(_isolated_citation_svc):
    resp = client.get("/citations/run-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    # FastAPI wraps HTTPException detail in {"detail": ...}
    detail = body.get("detail", body)
    assert "CITATION_NOT_FOUND" in str(detail)


def test_list_citations_node_id_filter(_isolated_citation_svc):
    svc = _isolated_citation_svc
    # Attach traces with explicit task_or_artifact_ref to control node routing
    t_a = _trace(chunk_id="ca")
    t_b = _trace(chunk_id="cb")
    svc.attach_trace("run-filter", "node-x", [t_a])
    svc.attach_trace("run-filter", "node-y", [t_b])

    resp = client.get("/citations/run-filter?node_id=node-x")
    assert resp.status_code == 200
    body = resp.json()
    # Only traces whose task_or_artifact_ref contains node:node-x
    traces = body["data"]["traces"]
    assert all("node:node-x" in t["task_or_artifact_ref"] for t in traces)
    assert len(traces) == 1


def test_list_citations_citation_missing_flag(_isolated_citation_svc):
    """citation_missing in response reflects trace list being empty.

    When a run has no traces (the query returns an empty list because node_id
    filter excludes all), citation_missing should be True.
    """
    svc = _isolated_citation_svc
    svc.attach_trace("run-cm", "node-p", [_trace()])

    # Filter for a non-existent node → empty list → citation_missing=True
    resp = client.get("/citations/run-cm?node_id=node-no-such")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["citation_missing"] is True


# ---------------------------------------------------------------------------
# GET /citations/{run_id}/export
# ---------------------------------------------------------------------------


def test_export_citations_happy_path(_isolated_citation_svc):
    svc = _isolated_citation_svc
    svc.attach_trace("run-exp", "node-e", [_trace()])

    resp = client.get("/citations/run-exp/export")
    assert resp.status_code == 200

    body = resp.json()
    assert "data" in body
    assert body["data"]["run_id"] == "run-exp"
    assert isinstance(body["data"]["traces"], list)
    assert len(body["data"]["traces"]) == 1
    assert "citation_missing" in body["data"]


def test_export_citations_404_for_unknown_run(_isolated_citation_svc):
    resp = client.get("/citations/run-export-missing/export")
    assert resp.status_code == 404
    body = resp.json()
    detail = body.get("detail", body)
    assert "CITATION_NOT_FOUND" in str(detail)


def test_export_citations_envelope_shape(_isolated_citation_svc):
    svc = _isolated_citation_svc
    svc.attach_trace("run-env", "node-n", [_trace()])

    resp = client.get("/citations/run-env/export")
    body = resp.json()
    assert "trace_id" in body["meta"]
    assert "timestamp" in body["meta"]

"""CitationService tests — Story 9.2 AC1/AC2/AC7.

Covers:
  - attach_trace: persists traces and returns normalised list
  - attach_trace: empty traces list is a no-op
  - attach_trace: fills in canonical task_or_artifact_ref when caller omits it
  - attach_trace: truncates excerpt to _EXCERPT_LIMIT chars
  - get_traces: returns all traces for a run
  - get_traces: filters by node_id via task_or_artifact_ref
  - get_traces: returns [] for unknown run_id (no crash)
  - get_traces: skips corrupted individual records (no crash)
  - export_traces: returns CitationReport-shaped dict with run_id + traces
  - export_traces: citation_missing=True when traces list is empty
  - summary_for_step: count and citation_missing computed correctly
  - has_traces: True after first attach, False for unknown run
  - concurrent attach_trace calls produce merged file (not overwrite)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

import pytest

from xueban.runtime.citation_service import (
    CitationNotFound,
    CitationService,
    CitationTrace,
)


@pytest.fixture()
def svc(tmp_path: Path) -> CitationService:
    return CitationService(storage_dir=tmp_path / "citations")


def _trace(**kwargs) -> CitationTrace:
    defaults = dict(
        pack_id="pack01",
        source_id="src01",
        chunk_id="chunk_0",
        excerpt="hello world",
        confidence=0.75,
    )
    defaults.update(kwargs)
    return CitationTrace(**defaults)


# ---------------------------------------------------------------------------
# attach_trace
# ---------------------------------------------------------------------------


def test_attach_trace_creates_file_and_returns_traces(svc, tmp_path):
    t = _trace()
    result = svc.attach_trace("run-abc", "node-1", [t])
    assert len(result) == 1
    assert result[0].trace_id == t.trace_id
    assert svc.has_traces("run-abc")


def test_attach_trace_empty_list_is_noop(svc):
    result = svc.attach_trace("run-abc", "node-1", [])
    assert result == []
    assert not svc.has_traces("run-abc")


def test_attach_trace_fills_canonical_ref_when_blank(svc):
    t = _trace(task_or_artifact_ref="")
    result = svc.attach_trace("run-xyz", "node-q", [t])
    assert "run:run-xyz" in result[0].task_or_artifact_ref
    assert "node:node-q" in result[0].task_or_artifact_ref


def test_attach_trace_preserves_caller_ref(svc):
    t = _trace(task_or_artifact_ref="artifact:my-report")
    result = svc.attach_trace("run-xyz", "node-q", [t])
    assert result[0].task_or_artifact_ref == "artifact:my-report"


def test_attach_trace_truncates_long_excerpt(svc):
    # Use model_construct to bypass the max_length=200 Pydantic constraint so
    # we can verify the service-layer truncation guard in attach_trace.
    long_excerpt = "x" * 500
    t = CitationTrace.model_construct(
        trace_id="tid-trunc",
        pack_id="pack01",
        source_id="src01",
        chunk_id="chunk_0",
        excerpt=long_excerpt,
        confidence=0.75,
        retrieved_at=None,
        task_or_artifact_ref="",
    )
    result = svc.attach_trace("run-trunc", "node-1", [t])
    assert len(result[0].excerpt) <= 200


def test_attach_trace_appends_on_second_call(svc):
    svc.attach_trace("run-abc", "node-1", [_trace(chunk_id="c0")])
    svc.attach_trace("run-abc", "node-2", [_trace(chunk_id="c1")])
    traces = svc.get_traces("run-abc")
    assert len(traces) == 2


def test_attach_trace_raises_on_empty_run_id(svc):
    with pytest.raises(ValueError, match="run_id"):
        svc.attach_trace("", "node-1", [_trace()])


# ---------------------------------------------------------------------------
# get_traces
# ---------------------------------------------------------------------------


def test_get_traces_happy_path(svc):
    svc.attach_trace("run-1", "node-a", [_trace(chunk_id="c0"), _trace(chunk_id="c1")])
    traces = svc.get_traces("run-1")
    assert len(traces) == 2


def test_get_traces_unknown_run_returns_empty(svc):
    traces = svc.get_traces("run-does-not-exist")
    assert traces == []


def test_get_traces_node_id_filter(svc):
    t_a = _trace(chunk_id="ca", task_or_artifact_ref="run:run-1/node:node-a")
    t_b = _trace(chunk_id="cb", task_or_artifact_ref="run:run-1/node:node-b")
    # Attach without auto-ref so we control the task_or_artifact_ref values.
    from xueban.runtime.citation_service import _EXCERPT_LIMIT

    with svc._lock:
        existing = svc._load_raw("run-1")
        existing.extend([t_a.model_dump(mode="json"), t_b.model_dump(mode="json")])
        svc._persist("run-1", existing)

    only_a = svc.get_traces("run-1", node_id="node-a")
    assert len(only_a) == 1
    assert only_a[0].chunk_id == "ca"


def test_get_traces_skips_corrupted_records(svc, tmp_path):
    run_id = "run-corrupted"
    target = svc._storage_dir / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Mix one valid trace with one corrupted dict
    good = _trace(chunk_id="c0").model_dump(mode="json")
    bad = {"not_a_trace": True}
    target.write_text(
        json.dumps({"run_id": run_id, "traces": [good, bad]}),
        encoding="utf-8",
    )
    traces = svc.get_traces(run_id)
    assert len(traces) == 1
    assert traces[0].chunk_id == "c0"


def test_get_traces_handles_empty_json_file(svc, tmp_path):
    run_id = "run-empty"
    target = svc._storage_dir / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
    assert svc.get_traces(run_id) == []


# ---------------------------------------------------------------------------
# export_traces
# ---------------------------------------------------------------------------


def test_export_traces_returns_report_shape(svc):
    svc.attach_trace("run-e", "node-1", [_trace()])
    report = svc.export_traces("run-e")
    assert report["run_id"] == "run-e"
    assert isinstance(report["traces"], list)
    assert len(report["traces"]) == 1
    assert "citation_missing" in report


def test_export_traces_citation_missing_when_empty(svc):
    # run with no traces → empty run_id.json doesn't exist, export returns empty list
    report = svc.export_traces("run-no-traces")
    assert report["run_id"] == "run-no-traces"
    assert report["traces"] == []
    assert report["citation_missing"] is True


# ---------------------------------------------------------------------------
# summary_for_step
# ---------------------------------------------------------------------------


def test_summary_for_step_count_and_not_missing(svc):
    traces = [_trace()]
    summary = svc.summary_for_step(traces, citation_required=True)
    assert summary.count == 1
    assert summary.citation_missing is False


def test_summary_for_step_missing_when_required_and_empty(svc):
    summary = svc.summary_for_step([], citation_required=True)
    assert summary.count == 0
    assert summary.citation_missing is True


def test_summary_for_step_not_missing_when_not_required(svc):
    summary = svc.summary_for_step([], citation_required=False)
    assert summary.citation_missing is False


# ---------------------------------------------------------------------------
# has_traces
# ---------------------------------------------------------------------------


def test_has_traces_false_for_unknown_run(svc):
    assert not svc.has_traces("run-unknown")


def test_has_traces_true_after_attach(svc):
    svc.attach_trace("run-ht", "node-1", [_trace()])
    assert svc.has_traces("run-ht")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_attach_merges_without_loss(svc):
    """Two threads appending to the same run should not overwrite each other."""
    errors: List[Exception] = []

    def worker(node_id: str) -> None:
        try:
            for i in range(5):
                svc.attach_trace("run-concurrent", node_id, [_trace(chunk_id=f"{node_id}-{i}")])
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("n1",))
    t2 = threading.Thread(target=worker, args=("n2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Concurrent attach raised: {errors}"
    traces = svc.get_traces("run-concurrent")
    # Both threads contributed 5 traces each
    assert len(traces) == 10

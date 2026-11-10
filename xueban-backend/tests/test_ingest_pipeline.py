"""Ingest pipeline tests — Story 9.1 AC6 + post-review hardening.

Covers parse → chunk → status update for the MVP path:
  - .txt file source ingests cleanly → done + chunk_count > 0
  - missing file → ingest_status=failed, error_message non-empty, pack.status=failed
  - chunk_text behaviour (overlap clamp, empty input)
  - KeywordIndex persists JSON to .xueban/knowledge/{pack_id}/index.json
  - C1 file sandbox: paths outside the sandbox are rejected
  - C2 SSRF: loopback / private / link-local hosts are rejected
  - L1 dataset source: explicit INGEST_PARSE_FAILED until a registry lands
  - C3 atomic write: KeywordIndex.persist uses tempfile + os.replace
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from xueban.memory.ingest_pipeline import (
    IngestError,
    KeywordIndex,
    _enforce_url_safety,
    chunk_text,
    parse_source,
    run_ingest,
)
from xueban.memory.knowledge_pack import KnowledgePack, KnowledgeSource


@pytest.fixture(autouse=True)
def _sandbox_to_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """C1: tests use tmp_path as their file source root via the sandbox env var."""
    monkeypatch.setenv(
        "XUEBAN_KNOWLEDGE_FILE_ROOTS", str(tmp_path)
    )
    yield


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


def test_chunk_text_empty_returns_empty():
    assert chunk_text("", chunk_size=100, overlap=10) == []


def test_chunk_text_basic_window():
    chunks = chunk_text("abcdefghij", chunk_size=4, overlap=1)
    # step = 3 → "abcd" "defg" "ghij" "j"
    assert chunks[0] == "abcd"
    assert chunks[1] == "defg"
    assert chunks[2] == "ghij"
    assert chunks[-1].endswith("j")


def test_chunk_text_overlap_clamp_avoids_infinite_loop():
    # overlap >= chunk_size should be clamped
    chunks = chunk_text("abcdefghij", chunk_size=4, overlap=10)
    # step is clamped to 1 → produces at most len(text) chunks
    assert len(chunks) <= len("abcdefghij")


# ---------------------------------------------------------------------------
# parse_source
# ---------------------------------------------------------------------------


def test_parse_text_source():
    src = KnowledgeSource(source_type="text", source_ref="hello world")
    assert parse_source(src) == "hello world"


def test_parse_txt_file(tmp_path: Path):
    f = tmp_path / "doc.txt"
    f.write_text("hello\nworld", encoding="utf-8")
    src = KnowledgeSource(source_type="file", source_ref=str(f))
    text = parse_source(src)
    assert "hello" in text and "world" in text


def test_parse_md_file(tmp_path: Path):
    f = tmp_path / "doc.md"
    f.write_text("# Title\nbody", encoding="utf-8")
    src = KnowledgeSource(source_type="file", source_ref=str(f))
    assert "Title" in parse_source(src)


def test_parse_missing_file_raises(tmp_path: Path):
    """Missing file inside the sandbox → INGEST_PARSE_FAILED."""
    src = KnowledgeSource(source_type="file", source_ref=str(tmp_path / "missing.txt"))
    with pytest.raises(IngestError) as exc:
        parse_source(src)
    assert exc.value.code == "INGEST_PARSE_FAILED"


# ---------------------------------------------------------------------------
# C1: file sandbox enforcement
# ---------------------------------------------------------------------------


def test_parse_file_outside_sandbox_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """C1: an absolute path outside XUEBAN_KNOWLEDGE_FILE_ROOTS is rejected."""
    other = tmp_path.parent / "outside.txt"
    other.write_text("secret", encoding="utf-8")
    try:
        # Override the autouse fixture's sandbox to exclude `other`
        monkeypatch.setenv("XUEBAN_KNOWLEDGE_FILE_ROOTS", str(tmp_path))
        src = KnowledgeSource(source_type="file", source_ref=str(other))
        with pytest.raises(IngestError) as exc:
            parse_source(src)
        assert exc.value.code == "INGEST_PARSE_FAILED"
        assert "sandbox" in exc.value.message.lower()
    finally:
        other.unlink(missing_ok=True)


def test_parse_file_traversal_via_dotdot_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """C1: relative paths with `..` get resolved before sandbox check."""
    # Sandbox is tmp_path/inner; a `../escape.txt` should resolve outside it.
    inner = tmp_path / "inner"
    inner.mkdir()
    monkeypatch.setenv("XUEBAN_KNOWLEDGE_FILE_ROOTS", str(inner))
    escape = inner / ".." / "escape.txt"
    escape.parent.parent.joinpath("escape.txt").write_text("x", encoding="utf-8")
    try:
        src = KnowledgeSource(source_type="file", source_ref=str(escape))
        with pytest.raises(IngestError):
            parse_source(src)
    finally:
        (tmp_path / "escape.txt").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# L1: dataset source rejected explicitly (no silent no-op)
# ---------------------------------------------------------------------------


def test_parse_dataset_source_raises():
    src = KnowledgeSource(source_type="dataset", source_ref="dataset:foo@v1")
    with pytest.raises(IngestError) as exc:
        parse_source(src)
    assert exc.value.code == "INGEST_PARSE_FAILED"
    assert "dataset" in exc.value.message.lower()


# ---------------------------------------------------------------------------
# C2: SSRF guard for URL sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        "http://10.0.0.1/",  # RFC1918
        "http://192.168.1.1/",
        "http://[::1]/",
        "ftp://example.com/file.txt",  # disallowed scheme
        "file:///etc/passwd",
    ],
)
def test_enforce_url_safety_blocks_dangerous_targets(url: str):
    with pytest.raises(IngestError) as exc:
        _enforce_url_safety(url)
    assert exc.value.code == "INGEST_PARSE_FAILED"


def test_enforce_url_safety_accepts_public_host():
    """Sanity check: a public hostname should pass scheme + IP-class checks.

    Note: this hits real DNS for `example.com`. Skipped if offline (gaierror
    surfaces as IngestError, and we accept either pass-or-skip semantics).
    """
    try:
        _enforce_url_safety("https://example.com/")
    except IngestError:
        pytest.skip("DNS unavailable in test env — skipping public-URL sanity check")


# ---------------------------------------------------------------------------
# run_ingest — happy + failure
# ---------------------------------------------------------------------------


def test_run_ingest_smoke_txt(tmp_path: Path):
    f = tmp_path / "kb.txt"
    f.write_text("alpha beta gamma " * 10, encoding="utf-8")
    pack = KnowledgePack(
        name="kb",
        sources=[KnowledgeSource(source_type="file", source_ref=str(f))],
    )
    index = KeywordIndex(root=tmp_path / "knowledge")
    finished = run_ingest(pack, index=index)

    assert finished.status == "ready"
    assert finished.sources[0].ingest_status == "done"
    assert finished.sources[0].chunk_count > 0
    assert finished.sources[0].checksum  # SHA-256 hex populated

    persisted = tmp_path / "knowledge" / pack.pack_id / "index.json"
    assert persisted.exists()
    # C3: persist uses atomic write — temp file should not linger.
    leftovers = list((tmp_path / "knowledge" / pack.pack_id).glob(".index.json.*.tmp"))
    assert leftovers == []


def test_run_ingest_failure_marks_source_failed(tmp_path: Path):
    pack = KnowledgePack(
        name="kb",
        sources=[KnowledgeSource(source_type="file", source_ref=str(tmp_path / "missing.txt"))],
    )
    index = KeywordIndex(root=tmp_path / "knowledge")
    finished = run_ingest(pack, index=index)

    assert finished.status == "failed"
    assert finished.sources[0].ingest_status == "failed"
    assert finished.sources[0].error_message  # non-empty


def test_run_ingest_partial_failure(tmp_path: Path):
    """One source succeeds, one fails — pack ends `failed`, success keeps its status."""
    f = tmp_path / "ok.txt"
    f.write_text("ok content", encoding="utf-8")
    pack = KnowledgePack(
        name="mixed",
        sources=[
            KnowledgeSource(source_type="file", source_ref=str(f)),
            KnowledgeSource(source_type="file", source_ref=str(tmp_path / "missing.txt")),
        ],
    )
    index = KeywordIndex(root=tmp_path / "knowledge")
    finished = run_ingest(pack, index=index)
    assert finished.status == "failed"
    assert finished.sources[0].ingest_status == "done"
    assert finished.sources[1].ingest_status == "failed"

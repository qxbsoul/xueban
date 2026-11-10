"""Tests for LightRAG adapter — Story 9.1 Phase 2.

Test strategy:
  1. Default (LIGHTRAG_ENABLED not set / false) → is_enabled() returns False,
     ingest still works via keyword stub, no LightRAG import attempted.
  2. LIGHTRAG_ENABLED=true but lightrag-hku not installed → ImportError is
     propagated clearly (not silently swallowed).
  3. LIGHTRAG_ENABLED=true with proper env vars → adapter functions are called
     (integration tests, skipped in CI unless lightrag-hku is installed).
  4. backend_label() returns correct string in each state.
  5. delete_pack_index() is a no-op when the directory does not exist.
  6. Full run_ingest stub path is unchanged when LIGHTRAG_ENABLED is false.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LIGHTRAG_PKG = "lightrag"
_ADAPTER_MOD = "xueban.runtime.lightrag_adapter"


def _lightrag_installed() -> bool:
    """Return True if lightrag-hku package is importable in this environment."""
    try:
        import lightrag  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Fixture: clean env for each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Ensure LIGHTRAG_ENABLED is unset before each test (default = stub path)."""
    monkeypatch.delenv("LIGHTRAG_ENABLED", raising=False)
    # Force adapter module to be re-evaluated for is_enabled() calls
    if _ADAPTER_MOD in sys.modules:
        importlib.reload(sys.modules[_ADAPTER_MOD])
    yield


# ---------------------------------------------------------------------------
# 1. Feature flag — default disabled
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from xueban.runtime import lightrag_adapter
        assert lightrag_adapter.is_enabled() is False

    def test_enabled_when_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        from xueban.runtime import lightrag_adapter
        assert lightrag_adapter.is_enabled() is True

    def test_enabled_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "TRUE")
        from xueban.runtime import lightrag_adapter
        assert lightrag_adapter.is_enabled() is True

    def test_not_enabled_for_other_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "yes", "on", "false", "0", ""):
            monkeypatch.setenv("LIGHTRAG_ENABLED", val)
            from xueban.runtime import lightrag_adapter
            assert lightrag_adapter.is_enabled() is False, f"Expected False for {val!r}"


# ---------------------------------------------------------------------------
# 2. backend_label()
# ---------------------------------------------------------------------------

class TestBackendLabel:
    def test_stub_when_disabled(self) -> None:
        from xueban.runtime import lightrag_adapter
        assert lightrag_adapter.backend_label() == "stub"

    def test_lightrag_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        from xueban.runtime import lightrag_adapter
        assert lightrag_adapter.backend_label() == "lightrag"


# ---------------------------------------------------------------------------
# 3. delete_pack_index() — file system behaviour
# ---------------------------------------------------------------------------

class TestDeletePackIndex:
    def test_noop_when_dir_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        from xueban.runtime import lightrag_adapter
        # Should not raise even though directory does not exist
        lightrag_adapter.delete_pack_index("a" * 32)

    def test_removes_pack_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        pack_id = "b" * 32
        pack_dir = tmp_path / "lightrag" / pack_id
        pack_dir.mkdir(parents=True)
        (pack_dir / "dummy.json").write_text("{}")
        from xueban.runtime import lightrag_adapter
        lightrag_adapter.delete_pack_index(pack_id)
        assert not pack_dir.exists()


# ---------------------------------------------------------------------------
# 4. ingest_text() — disabled path (stub, no lightrag import)
# ---------------------------------------------------------------------------

class TestIngestTextDisabled:
    def test_not_called_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When LIGHTRAG_ENABLED is false, run_ingest must NOT call ingest_text."""
        from xueban.runtime import lightrag_adapter

        called = []
        monkeypatch.setattr(lightrag_adapter, "ingest_text", lambda *a, **kw: called.append(True))

        # is_enabled() returns False → ingest_text should never be reached via run_ingest
        assert lightrag_adapter.is_enabled() is False

        from xueban.memory.knowledge_pack import KnowledgePack, KnowledgeSource
        from xueban.memory.retrieval_profiles import RetrievalProfile
        from xueban.memory.ingest_pipeline import run_ingest, KeywordIndex

        source = KnowledgeSource(source_type="text", source_ref="hello world")
        pack = KnowledgePack(
            name="test",
            sources=[source],
            retrieval_profile=RetrievalProfile(),
        )
        run_ingest(pack, index=KeywordIndex(root=None))  # type: ignore[arg-type]
        assert called == [], "ingest_text should NOT be called when LIGHTRAG_ENABLED is false"


# ---------------------------------------------------------------------------
# 5. ingest_text() + query() — enabled path (requires lightrag-hku installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _lightrag_installed(),
    reason="lightrag-hku not installed; skipping live LightRAG integration tests",
)
class TestLightRagLiveIntegration:
    """Integration smoke tests that require lightrag-hku to be installed.

    These tests run against a temp directory and a mock LLM/embedding function
    to avoid real API calls during CI.  They verify that the adapter can:
      - call ingest_text without error (using mocked LightRAG)
      - call query and get a string back (using mocked LightRAG)
    """

    def test_ingest_text_mock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        monkeypatch.setenv("LIGHTRAG_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LIGHTRAG_EMBED_API_KEY", "sk-test")

        from xueban.runtime import lightrag_adapter

        # Mock the LightRAG class so no real HTTP calls are made
        mock_rag = MagicMock()
        mock_rag.initialize_storages = MagicMock(return_value=_async_noop())
        mock_rag.ainsert = MagicMock(return_value=_async_noop())
        mock_rag.finalize_storages = MagicMock(return_value=_async_noop())

        with patch.object(lightrag_adapter, "_build_rag", return_value=mock_rag):
            lightrag_adapter.ingest_text("a" * 32, "Hello LightRAG world")

        mock_rag.ainsert.assert_called_once()

    def test_query_returns_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        monkeypatch.setenv("LIGHTRAG_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LIGHTRAG_EMBED_API_KEY", "sk-test")

        from xueban.runtime import lightrag_adapter

        async def _fake_query(*args, **kwargs):  # noqa: ANN002,ANN003
            return "Mocked answer"

        mock_rag = MagicMock()
        mock_rag.initialize_storages = MagicMock(return_value=_async_noop())
        mock_rag.aquery = MagicMock(return_value=_async_return("Mocked answer"))
        mock_rag.finalize_storages = MagicMock(return_value=_async_noop())

        with patch.object(lightrag_adapter, "_build_rag", return_value=mock_rag):
            result = lightrag_adapter.query("a" * 32, "What is this about?")

        assert isinstance(result, str)
        assert len(result) > 0

    def test_ingest_text_skips_empty_string(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        monkeypatch.setenv("LIGHTRAG_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LIGHTRAG_EMBED_API_KEY", "sk-test")

        from xueban.runtime import lightrag_adapter

        build_calls = []
        with patch.object(lightrag_adapter, "_build_rag", side_effect=lambda pid: build_calls.append(pid)):
            lightrag_adapter.ingest_text("a" * 32, "   ")  # whitespace-only

        assert build_calls == [], "_build_rag should not be called for empty/whitespace text"


# ---------------------------------------------------------------------------
# 6. Config error — missing API key when enabled
# ---------------------------------------------------------------------------

class TestConfigError:
    def test_missing_api_key_raises_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        monkeypatch.delenv("LIGHTRAG_LLM_API_KEY", raising=False)
        monkeypatch.delenv("LIGHTRAG_EMBED_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        from xueban.runtime import lightrag_adapter

        # Only run if lightrag is installed; otherwise _build_rag raises ImportError first
        if not _lightrag_installed():
            pytest.skip("lightrag-hku not installed")

        with pytest.raises(lightrag_adapter.LightRagConfigError, match="LIGHTRAG_LLM_API_KEY"):
            lightrag_adapter.ingest_text("a" * 32, "some text")


# ---------------------------------------------------------------------------
# 7. API meta includes rag_backend
# ---------------------------------------------------------------------------

class TestApiMetaRagBackend:
    def test_list_packs_includes_rag_backend(self, tmp_path: Path) -> None:
        """GET /api/knowledge/packs envelope meta must include rag_backend field."""
        import xueban.api.knowledge as knowledge_api
        from xueban.api.knowledge import KnowledgeService
        from fastapi.testclient import TestClient
        from xueban.server import app

        client = TestClient(app)
        svc = KnowledgeService(storage_dir=tmp_path / "knowledge")
        knowledge_api.set_service(svc)
        try:
            resp = client.get("/api/knowledge/packs")
            assert resp.status_code == 200
            body = resp.json()
            assert "rag_backend" in body["meta"], "meta must include rag_backend"
            assert body["meta"]["rag_backend"] in ("stub", "lightrag")
        finally:
            knowledge_api.set_service(KnowledgeService())

    def test_create_pack_includes_rag_backend(self, tmp_path: Path) -> None:
        """POST /api/knowledge/packs envelope meta must include rag_backend field."""
        import xueban.api.knowledge as knowledge_api
        from xueban.api.knowledge import KnowledgeService
        from fastapi.testclient import TestClient
        from xueban.server import app

        client = TestClient(app)
        svc = KnowledgeService(storage_dir=tmp_path / "knowledge")
        knowledge_api.set_service(svc)
        try:
            resp = client.post(
                "/api/knowledge/packs",
                json={
                    "name": "meta-test",
                    "sources": [{"source_type": "text", "source_ref": "hello"}],
                },
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "rag_backend" in body["meta"]
            assert body["meta"]["rag_backend"] == "stub"  # LIGHTRAG_ENABLED not set
        finally:
            knowledge_api.set_service(KnowledgeService())


# ---------------------------------------------------------------------------
# 8. query_hits() — multi-chunk retrieval (2026-08-22)
# ---------------------------------------------------------------------------


class TestQueryHitsParsing:
    """Pure parsing helpers: no lightrag import, no network."""

    def test_hits_from_structured_maps_chunks(self) -> None:
        from xueban.runtime import lightrag_adapter

        result = {
            "status": "success",
            "data": {
                "chunks": [
                    {"content": "chunk one", "file_path": "docs/a.md", "reference_id": "1"},
                    {"content": "chunk two", "reference_id": "2"},
                    {"content": "  ", "file_path": "skip-me.md"},
                    "not-a-dict",
                ],
                "references": [{"reference_id": "2", "file_path": "docs/b.md"}],
            },
        }
        hits = lightrag_adapter._hits_from_structured(result, top_k=5)
        assert [h["text"] for h in hits] == ["chunk one", "chunk two"]
        assert hits[0]["source"] == "docs/a.md"
        assert hits[1]["source"] == "docs/b.md"  # resolved via reference_id
        # rank-derived scores: 1.0 → 0.5 (no score in LightRAG chunk payload)
        assert hits[0]["score"] == 1.0
        assert hits[1]["score"] == 0.5

    def test_hits_from_structured_keeps_explicit_score(self) -> None:
        from xueban.runtime import lightrag_adapter

        result = {"data": {"chunks": [{"content": "c", "rerank_score": 0.42}]}}
        hits = lightrag_adapter._hits_from_structured(result, top_k=5)
        assert hits[0]["score"] == 0.42

    def test_hits_from_structured_bad_shape_returns_empty(self) -> None:
        from xueban.runtime import lightrag_adapter

        assert lightrag_adapter._hits_from_structured("not a dict", 5) == []
        assert lightrag_adapter._hits_from_structured({"data": {}}, 5) == []

    def test_hits_from_context_parses_json_lines(self) -> None:
        from xueban.runtime import lightrag_adapter

        context = (
            "-----Document Chunks-----\n"
            '{"content": "alpha chunk", "file_path": "a.md"}\n'
            '{"content": "beta chunk"}\n'
            '{"content": "alpha chunk", "file_path": "dup.md"}\n'  # duplicate → dedup
            "this is not json\n"
        )
        hits = lightrag_adapter._hits_from_context(context, top_k=5)
        assert [h["text"] for h in hits] == ["alpha chunk", "beta chunk"]
        assert hits[0]["source"] == "a.md"
        assert hits[1]["source"] == ""
        assert hits[1]["score"] == 0.5  # rank-derived

    def test_hits_from_context_non_string_returns_empty(self) -> None:
        from xueban.runtime import lightrag_adapter

        assert lightrag_adapter._hits_from_context(None, 5) == []


@pytest.mark.skipif(
    not _lightrag_installed(),
    reason="lightrag-hku not installed; skipping live LightRAG integration tests",
)
class TestQueryHitsIntegration:
    """query_hits() against a mocked LightRAG instance (no real API calls)."""

    def _set_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LIGHTRAG_ENABLED", "true")
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path / "lightrag"))
        monkeypatch.setenv("LIGHTRAG_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LIGHTRAG_EMBED_API_KEY", "sk-test")

    def test_query_hits_via_aquery_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._set_env(tmp_path, monkeypatch)
        from xueban.runtime import lightrag_adapter

        mock_rag = MagicMock()
        mock_rag.initialize_storages = MagicMock(return_value=_async_noop())
        mock_rag.finalize_storages = MagicMock(return_value=_async_noop())
        mock_rag.aquery_data = MagicMock(
            return_value=_async_return(
                {
                    "status": "success",
                    "data": {
                        "chunks": [
                            {"content": "氢能片段一", "file_path": "h2/a.md"},
                            {"content": "氢能片段二", "file_path": "h2/b.md"},
                        ],
                        "references": [],
                    },
                }
            )
        )

        with patch.object(lightrag_adapter, "_build_rag", return_value=mock_rag):
            hits = lightrag_adapter.query_hits("a" * 32, "氢能？", top_k=5)

        assert [h["text"] for h in hits] == ["氢能片段一", "氢能片段二"]
        assert hits[0] == {"text": "氢能片段一", "source": "h2/a.md", "score": 1.0}
        mock_rag.aquery_data.assert_called_once()

    def test_query_hits_legacy_context_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old lightrag without aquery_data → parse only_need_context string."""
        self._set_env(tmp_path, monkeypatch)
        from xueban.runtime import lightrag_adapter

        mock_rag = MagicMock(
            spec=["initialize_storages", "finalize_storages", "aquery"]
        )
        mock_rag.initialize_storages = MagicMock(return_value=_async_noop())
        mock_rag.finalize_storages = MagicMock(return_value=_async_noop())
        mock_rag.aquery = MagicMock(
            return_value=_async_return(
                '{"content": "legacy chunk", "file_path": "old.md"}\n'
            )
        )

        with patch.object(lightrag_adapter, "_build_rag", return_value=mock_rag):
            hits = lightrag_adapter.query_hits("a" * 32, "q", top_k=5)

        assert hits == [{"text": "legacy chunk", "source": "old.md", "score": 1.0}]

    def test_query_hits_raises_when_context_unparseable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy path with no parseable chunks → LightRagQueryError so the
        API endpoint falls back to the synthesized answer."""
        self._set_env(tmp_path, monkeypatch)
        from xueban.runtime import lightrag_adapter

        mock_rag = MagicMock(
            spec=["initialize_storages", "finalize_storages", "aquery"]
        )
        mock_rag.initialize_storages = MagicMock(return_value=_async_noop())
        mock_rag.finalize_storages = MagicMock(return_value=_async_noop())
        mock_rag.aquery = MagicMock(return_value=_async_return("no chunks here"))

        with patch.object(lightrag_adapter, "_build_rag", return_value=mock_rag):
            with pytest.raises(lightrag_adapter.LightRagQueryError):
                lightrag_adapter.query_hits("a" * 32, "q", top_k=5)


# ---------------------------------------------------------------------------
# Async helpers for mocking
# ---------------------------------------------------------------------------

async def _async_noop(*args, **kwargs) -> None:  # noqa: ANN002,ANN003
    return None


async def _async_return(value):  # noqa: ANN001,ANN201
    return value

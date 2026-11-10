"""Tests for scripts/generate_ts_types.py (Story 0.3)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import generate_ts_types  # noqa: E402


class TestGenerateTsTypes:
    def test_generate_produces_nonempty_file(self, tmp_path: Path) -> None:
        """generate() runs without error and writes a non-empty .ts file."""
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert len(content) > 0

    def test_output_has_auto_generated_header(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        assert "AUTO-GENERATED" in content
        assert "DO NOT EDIT" in content
        assert "xueban/runtime/contracts.py" in content

    def test_all_seven_core_interfaces_present(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        for name in [
            "TaskRecord",
            "RunRecord",
            "StepRecord",
            "ArtifactRef",
            "CheckpointRef",
            "MemoryEvent",
            "HandoffRef",
        ]:
            assert f"export interface {name}" in content, f"{name} interface missing"

    def test_fields_use_snake_case_not_camel(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        # snake_case fields that would be camelCase if incorrectly converted
        for field in ["task_id", "run_id", "step_id", "from_step_id", "event_id"]:
            assert field in content, f"snake_case field '{field}' missing from output"

    def test_supporting_types_emitted(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        assert "export interface WritebackRef" in content
        assert "export interface CheckpointState" in content

    def test_artifact_ref_references_writeback_ref(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        # ArtifactRef.writeback should reference WritebackRef by name, not inline
        assert "writeback: WritebackRef" in content

    def test_optional_fields_use_question_mark(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        # parent_task_id is Optional[str] = None in TaskRecord → should be optional
        assert "parent_task_id?" in content

    def test_dict_fields_become_record(self, tmp_path: Path) -> None:
        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        content = out.read_text(encoding="utf-8")
        assert "Record<string, unknown>" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        """Running generate twice produces identical output."""
        out1 = tmp_path / "workflow1.ts"
        out2 = tmp_path / "workflow2.ts"
        generate_ts_types.generate(out1)
        generate_ts_types.generate(out2)
        assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8")


class TestCommittedWorkflowTs:
    def test_committed_workflow_ts_matches_contracts(self, tmp_path: Path) -> None:
        """The checked-in workflow.ts must match what generate_ts_types produces now."""
        committed = ROOT / "src" / "core" / "types" / "workflow.ts"
        if not committed.exists():
            pytest.skip("workflow.ts not yet generated")
        fresh = tmp_path / "workflow.ts"
        generate_ts_types.generate(fresh)
        expected = fresh.read_text(encoding="utf-8")
        actual = committed.read_text(encoding="utf-8")
        assert actual == expected, (
            "Committed workflow.ts is out of sync with contracts.py — "
            "run `python scripts/generate_ts_types.py` and commit the result"
        )


class TestCheckContracts:
    def test_up_to_date_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_contracts returns 0 when workflow.ts matches contracts.py."""
        import check_contracts as cc

        out = tmp_path / "workflow.ts"
        generate_ts_types.generate(out)
        monkeypatch.setattr(cc, "WORKFLOW_TS", out)
        assert cc.main() == 0

    def test_drift_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_contracts returns 1 when workflow.ts has drifted."""
        import check_contracts as cc

        stale = tmp_path / "workflow.ts"
        stale.write_text(
            "// AUTO-GENERATED — DO NOT EDIT.\nexport interface TaskRecord { task_id: string; }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cc, "WORKFLOW_TS", stale)
        assert cc.main() == 1

    def test_missing_file_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_contracts returns 1 when workflow.ts does not exist."""
        import check_contracts as cc

        monkeypatch.setattr(cc, "WORKFLOW_TS", tmp_path / "nonexistent.ts")
        assert cc.main() == 1

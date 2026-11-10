"""tests/test_builder_publish.py — publish_blueprint 真实回填链路测试 (Story 8.6 AC8)

覆盖：
  - template 合法性验证（WorkflowTemplateSpec.model_validate）
  - workflow 合法性验证（WorkflowDefinition.model_validate）
  - metadata 字段 builder_origin / source_blueprint_id / workflow_id
  - REGRESSION_BLOCKED 时不写入文件系统
  - 文件写入路径符合预期
  - partial-write 回滚：workflow 写入失败时 template 被清理
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xueban.highlevel import WorkflowTemplateSpec
from xueban.runtime.contracts import WorkflowDefinition
from xueban.runtime.builder_service import (
    BuilderService,
    PublishBlueprintRequest,
    RegressionBlockedError,
    _BUILDER_VERSION,
)
from xueban.runtime.contracts_builder import AgentBlueprint, RoleProfile


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _minimal_blueprint(**kwargs) -> AgentBlueprint:
    """Return an AgentBlueprint with sensible defaults (overridable via kwargs)."""
    defaults = {
        "blueprint_id": "bp-pub-test01",
        "version": "1.0",
        "name": "Test Publish Agent",
        "goal": "Perform a comprehensive test of the publish pipeline",
        "audience": "Developer",
        "mode": "single",
        "role_profiles": [
            RoleProfile(
                name="tester",
                description="Runs tests",
                executor_kind="api",
                executor_provider="anthropic",
            )
        ],
    }
    defaults.update(kwargs)
    return AgentBlueprint(**defaults)


def _make_service() -> BuilderService:
    return BuilderService()


# ---------------------------------------------------------------------------
# Template validity
# ---------------------------------------------------------------------------

def test_publish_produces_valid_template(tmp_path):
    """publish_blueprint must write a template that passes WorkflowTemplateSpec.model_validate."""
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        result = svc.publish_blueprint(req)

    # Find written template file
    template_path = tmp_path / "templates" / "custom" / f"{result.template_id}.yaml"
    assert template_path.exists(), "Template YAML file was not written"

    import yaml
    raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    # Must not raise
    spec = WorkflowTemplateSpec.model_validate(raw)
    assert spec.template_id == result.template_id


# ---------------------------------------------------------------------------
# Workflow validity
# ---------------------------------------------------------------------------

def test_publish_produces_valid_workflow(tmp_path):
    """publish_blueprint must write a workflow that passes WorkflowDefinition.model_validate."""
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        result = svc.publish_blueprint(req)

    workflow_path = tmp_path / ".xueban" / "workflows" / f"{result.workflow_id}.json"
    assert workflow_path.exists(), "Workflow JSON file was not written"

    raw = json.loads(workflow_path.read_text(encoding="utf-8"))
    # Must not raise
    wf = WorkflowDefinition.model_validate(raw)
    assert wf.workflow_id == result.workflow_id


# ---------------------------------------------------------------------------
# Metadata fields
# ---------------------------------------------------------------------------

def test_publish_template_has_builder_metadata(tmp_path):
    """Template metadata must include builder_origin, source_blueprint_id, workflow_id."""
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        result = svc.publish_blueprint(req)

    import yaml
    template_path = tmp_path / "templates" / "custom" / f"{result.template_id}.yaml"
    raw = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    meta = raw.get("metadata", {})

    assert meta.get("builder_origin") == "builder"
    assert meta.get("source_blueprint_id") == bp.blueprint_id
    assert meta.get("workflow_id") == result.workflow_id
    # R2-Patch-4: use _BUILDER_VERSION constant instead of hardcoded "8.6" literal
    assert meta.get("builder_version") == _BUILDER_VERSION


def test_publish_workflow_has_source_template_id(tmp_path):
    """Workflow metadata.source_template_id must point back to the template."""
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        result = svc.publish_blueprint(req)

    workflow_path = tmp_path / ".xueban" / "workflows" / f"{result.workflow_id}.json"
    raw = json.loads(workflow_path.read_text(encoding="utf-8"))
    meta = raw.get("metadata", {})

    assert meta.get("source_template_id") == result.template_id
    assert meta.get("builder_origin") == "builder"


# ---------------------------------------------------------------------------
# Publish status and links
# ---------------------------------------------------------------------------

def test_publish_returns_published_status(tmp_path):
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        result = svc.publish_blueprint(req)

    assert result.publish_status == "published"
    assert result.template_id.startswith("bldr-")
    assert len(result.template_id) == 13  # "bldr-" + 8 hex chars
    assert result.workflow_id  # non-empty
    assert result.links.templates == "/templates"
    assert result.workflow_id in result.links.editor
    assert result.links.inbox == "/inbox"
    # Patch 21: verify files were actually written to disk
    template_path = tmp_path / "templates" / "custom" / f"{result.template_id}.yaml"
    workflow_path = tmp_path / ".xueban" / "workflows" / f"{result.workflow_id}.json"
    assert template_path.exists(), f"Template file not written: {template_path}"
    assert workflow_path.exists(), f"Workflow file not written: {workflow_path}"


# ---------------------------------------------------------------------------
# REGRESSION_BLOCKED — no files written
# ---------------------------------------------------------------------------

def test_regression_gate_not_called_during_publish(tmp_path):
    """publish_blueprint does NOT call regression gate — files are written regardless.

    Story 9-6 fix: gate was moved out of publish_blueprint to avoid blocking publish
    with empty current_metrics. Gate is now only executed via /regression/{id}/run.
    Mocking RegressionService.gate to return "blocked" must have no effect on publish.
    """
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    mock_gate_result = MagicMock(status="blocked", reason="regression baseline violated")
    mock_regression_svc = MagicMock()
    mock_regression_svc.gate.return_value = mock_gate_result

    mock_module = types.ModuleType("xueban.runtime.regression_service")
    mock_module.RegressionService = lambda: mock_regression_svc  # type: ignore[attr-defined]

    import sys
    with patch.dict(sys.modules, {"xueban.runtime.regression_service": mock_module}):
        with (
            patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
            patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
        ):
            # publish should succeed — gate is not called during publish
            result = svc.publish_blueprint(req)

    assert result is not None
    # Files must have been written (gate did not block publish)
    template_dir = tmp_path / "templates" / "custom"
    workflow_dir = tmp_path / ".xueban" / "workflows"
    assert template_dir.exists() and any(template_dir.iterdir())
    assert workflow_dir.exists() and any(workflow_dir.iterdir())


# ---------------------------------------------------------------------------
# Template ID format
# ---------------------------------------------------------------------------

def test_publish_template_id_matches_slug_pattern(tmp_path):
    """Template ID must match server's ^[a-z0-9-]{3,40}$ pattern."""
    import re
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", tmp_path / "templates/custom"),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", tmp_path / ".xueban/workflows"),
    ):
        result = svc.publish_blueprint(req)

    assert re.match(r"^[a-z0-9-]{3,40}$", result.template_id), (
        f"template_id {result.template_id!r} does not match slug pattern"
    )


# ---------------------------------------------------------------------------
# Patch 15: Partial-write rollback (template orphan cleanup)
# ---------------------------------------------------------------------------

def test_publish_cleans_up_template_on_workflow_write_failure(tmp_path):
    """CRITICAL Patch 1: if workflow write fails, template file must be cleaned up (no orphan)."""
    svc = _make_service()
    bp = _minimal_blueprint()
    req = PublishBlueprintRequest(blueprint=bp)

    template_dir = tmp_path / "templates" / "custom"
    workflow_dir = tmp_path / ".xueban" / "workflows"

    call_count = 0

    original_write_text = Path.write_text

    def _patched_write_text(self: Path, data: str, encoding: str = "utf-8", **kwargs: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call = template write — succeed normally
            return original_write_text(self, data, encoding=encoding, **kwargs)
        # Second call = workflow write — simulate disk full
        raise OSError("disk full (simulated)")

    with (
        patch("xueban.runtime.builder_service._CUSTOM_TEMPLATE_DIR", template_dir),
        patch("xueban.runtime.builder_service._WORKFLOW_DIR", workflow_dir),
        patch.object(Path, "write_text", _patched_write_text),
    ):
        with pytest.raises(OSError, match="disk full"):
            svc.publish_blueprint(req)

    # Template directory may exist, but must contain no orphaned files
    orphan_files = list(template_dir.glob("bldr-*.yaml")) if template_dir.exists() else []
    assert orphan_files == [], f"Orphan template files not cleaned up: {orphan_files}"

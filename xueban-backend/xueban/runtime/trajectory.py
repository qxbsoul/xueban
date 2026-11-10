"""Trajectory assembly helpers for Story 1.5.

Builds RunTrajectory (summary format) and TrajectoryBundle (0G-archival format)
from a completed RunResult without touching any existing store contracts.

Story 9.2 extension: optionally embeds CitationService traces into the bundle
metadata under `metadata.citations` so trajectory archives carry provenance.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import yaml

from xueban.runtime.contracts import (
    RunResult,
    RunTrajectory,
    TrajectoryBundle,
    WorkflowDefinition,
)

logger = logging.getLogger("xueban.trajectory")


def build_run_trajectory(result: RunResult) -> RunTrajectory:
    """Assemble a structured trajectory from a RunResult."""
    final_artifacts = [
        art for art in result.artifacts
        if art.metadata.get("final", False)
    ]

    sorted_steps = sorted(
        result.steps,
        key=lambda s: (s.index, s.started_at),
    )
    sorted_handoffs = sorted(
        result.handoffs,
        key=lambda h: h.created_at,
    )
    sorted_checkpoints = sorted(
        result.checkpoints,
        key=lambda c: c.created_at,
    )

    exported_at = result.run.ended_at or result.run.started_at

    return RunTrajectory(
        run=result.run,
        steps=sorted_steps,
        handoffs=sorted_handoffs,
        checkpoints=sorted_checkpoints,
        final_artifacts=final_artifacts,
        exported_at=exported_at,
    )


def build_trajectory_bundle(
    result: RunResult,
    workflow: Optional[WorkflowDefinition] = None,
    *,
    citation_service: Optional[Any] = None,
) -> TrajectoryBundle:
    """Assemble a full bundle suitable for 0G Storage archival.

    `citation_service` (optional) lets callers inject a custom CitationService
    instance; when omitted we resolve the global singleton lazily so test code
    can stub it out via `set_service`.
    """
    trajectory = build_run_trajectory(result)

    workflow_yaml: Optional[str] = None
    policy_matrix = None

    if workflow is not None:
        try:
            wf_dict = workflow.model_dump(mode="json", exclude_none=True)
            workflow_yaml = yaml.safe_dump(wf_dict, allow_unicode=True, sort_keys=False)
        except Exception:
            logger.error("Failed to serialize workflow to YAML for run=%s", result.run.run_id, exc_info=True)
            workflow_yaml = None
        policy_matrix = workflow.policy_matrix

    citations: List[Dict[str, Any]] = []
    try:
        from xueban.runtime.citation_service import (
            get_service as _get_citation_service,
        )

        svc = citation_service or _get_citation_service()
        traces = svc.get_traces(result.run.run_id)
        citations = [t.model_dump(mode="json") for t in traces]
    except Exception:  # pragma: no cover - never break export on citation errors
        logger.exception(
            "citation export failed for run_id=%s", result.run.run_id
        )
        citations = []

    return TrajectoryBundle(
        trajectory=trajectory,
        workflow_yaml=workflow_yaml,
        policy_matrix=policy_matrix,
        exported_at=trajectory.exported_at,
        # Story 9.2 AC6: top-level citations list (empty when no provenance).
        citations=citations,
        metadata={
            "run_id": result.run.run_id,
            "workflow_id": result.run.workflow_id,
            "status": result.run.status,
        },
    )

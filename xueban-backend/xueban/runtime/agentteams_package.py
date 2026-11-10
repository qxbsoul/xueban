"""AgentTeams offline deliverable (design §6, 复赛必交「可执行代码包」).

Two pieces, both fully verifiable WITHOUT a Kubernetes cluster or Matrix
homeserver:

1. ``translate_dag_to_team`` — deterministic DAG→Team-spec translation:
     - ``kind=agent`` nodes  → workers (id/role/skills from node config)
     - ``control.loop`` body → the team's iterative task (setpoint → success_criteria)
     - ``control.parallel`` branches → workers flagged parallel
     - workflow-level skills/runtime defaults honoured
2. ``export_team_package`` — writes the deliverable code package:
     team-cr.yaml · manifest.json · workflow.json · run.sh · README.md
   (``export_team_package_zip`` returns the same package in-memory for HTTP.)

The Team CR is rendered by the existing ``render_team_cr`` so the exported
artifact is byte-identical to what AgentTeamsExecutor would apply live — the
offline package IS the live spec, minus the cluster.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from xueban.runtime.agentteams_bridge import render_team_cr
from xueban.runtime.contracts import WorkflowDefinition

AGENT_KINDS = {"agent"}
CONTROL_TYPES = {"control.loop", "control.parallel", "control.fanout", "control.join"}


def _node_role(node: Any) -> str:
    cfg = node.config or {}
    return str(cfg.get("role") or cfg.get("name") or node.id)


def _node_skills(node: Any, default: List[str]) -> List[str]:
    cfg = node.config or {}
    skills = cfg.get("skills")
    if isinstance(skills, list) and skills:
        return [str(s) for s in skills]
    return list(default)


def translate_dag_to_team(
    workflow: WorkflowDefinition,
    *,
    runtime: str = "hermes",
    default_skills: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Translate a Xueban workflow DAG into an AgentTeams team_spec dict.

    Mapping (deterministic, order-preserving):
      - kind=agent nodes → workers [{id, role, runtime, skills, parallel}]
      - control.loop → team task: {goal_template, success_criteria (setpoint),
        iteration_budget, body workers referenced by subgraph}
      - control.parallel → body workers flagged parallel=true
      - control nodes never become workers themselves
    """
    defaults = workflow.defaults or {}
    team_runtime = str(defaults.get("agentteams", {}).get("runtime", runtime))
    skills_default = list(default_skills or defaults.get("skills") or [])
    provider = workflow.workflow_id

    workers: List[Dict[str, Any]] = []
    loop_tasks: List[Dict[str, Any]] = []

    def _body_worker_ids(node: Any) -> List[str]:
        """Worker ids inside a control node's body.

        control.loop puts body ids in config.subgraph; a config.preset loop
        expands to {node_id}-{phase} ids; control.parallel puts them in
        config.branches as a list of branch lists — flatten all.
        """
        cfg = node.config or {}
        preset = cfg.get("preset")
        if isinstance(preset, str) and preset:
            from xueban.runtime.loop_engine import preset_phase_node_ids

            return preset_phase_node_ids(node.id, preset)
        raw: List[Any] = list(cfg.get("subgraph") or [])
        for branch in cfg.get("branches") or []:
            if isinstance(branch, list):
                raw.extend(branch)
            else:
                raw.append(branch)
        valid = {n.id for n in workflow.nodes}
        return [sid for sid in raw if sid in valid]

    for node in workflow.nodes:
        if node.type in CONTROL_TYPES:
            if node.type == "control.loop":
                cfg = node.config or {}
                setpoint = cfg.get("setpoint", {})
                loop_tasks.append(
                    {
                        "id": node.id,
                        "kind": "loop",
                        "goal_template": cfg.get("goal_template") or workflow.name,
                        "success_criteria": {
                            "metric": setpoint.get("metric"),
                            "op": setpoint.get("op", ">="),
                            "value": setpoint.get("value"),
                        },
                        "iteration_budget": cfg.get("iteration_budget"),
                        "body": _body_worker_ids(node),
                    }
                )
            continue
        if node.kind in AGENT_KINDS:
            workers.append(
                {
                    "id": node.id,
                    "role": _node_role(node),
                    "runtime": team_runtime,
                    "skills": _node_skills(node, skills_default),
                    "parallel": False,
                }
            )

    # mark workers that only exist inside parallel branches
    for node in workflow.nodes:
        if node.type != "control.parallel":
            continue
        branch_ids = set(_body_worker_ids(node))
        for w in workers:
            if w["id"] in branch_ids:
                w["parallel"] = True

    return {
        "provider": provider,
        "name": workflow.name,
        "runtime": team_runtime,
        "workers": workers,
        "skills": sorted({s for w in workers for s in w["skills"]}) or skills_default,
        "tasks": loop_tasks,
        "source": {
            "workflow_id": workflow.workflow_id,
            "version": workflow.version,
            "node_count": len(workflow.nodes),
            "edge_count": len(workflow.edges),
        },
    }


def render_run_sh(team_spec: Dict[str, Any]) -> str:
    name = f"xueban-{team_spec.get('provider', 'default')}".lower()
    return f"""#!/usr/bin/env bash
# AgentTeams code package entrypoint — rendered by Xueban
# Prereqs: kubectl (with the agentteams CRDs installed) + a Matrix homeserver
# reachable from the team's workers (see README.md).
set -euo pipefail

TEAM_NAME={name}

# 1. Apply the Team CR (manager + workers + skills)
kubectl apply -f team-cr.yaml

# 2. Wait for the controller to reconcile workers
kubectl wait --for=condition=Ready team/$TEAM_NAME --timeout=300s

# 3. Dispatch the task (the team's Matrix room is created by the controller;
#    Xueban's bridge can dispatch the goal, or use any Matrix client):
#    python -m xueban.cli run workflow.json --input '{{"goal": "..."}}'
echo "Team $TEAM_NAME applied. Dispatch the goal via the Xueban bridge or any Matrix client."
"""


def render_readme(team_spec: Dict[str, Any]) -> str:
    workers = team_spec.get("workers", [])
    tasks = team_spec.get("tasks", [])
    worker_lines = "\n".join(
        f"| `{w['id']}` | {w.get('role', '')} | {w.get('runtime', '')} | "
        f"{', '.join(w.get('skills', [])) or '—'} |{' parallel' if w.get('parallel') else ''}"
        for w in workers
    ) or "| _none_ | | | |"
    task_lines = "\n".join(
        f"- `{t['id']}`: success when `{t['success_criteria'].get('metric')}` "
        f"{t['success_criteria'].get('op')} {t['success_criteria'].get('value')} "
        f"(budget {t.get('iteration_budget')} iterations, body={t.get('body')})"
        for t in tasks
    ) or "- none (single-pass team)"
    return f"""# AgentTeams Code Package — {team_spec.get('name')}

Exported from Xueban workflow `{team_spec['source']['workflow_id']}` v{team_spec['source']['version']}.

## Contents
| File | Purpose |
|---|---|
| `team-cr.yaml` | AgentTeams Team CR (manager + workers + skills) — identical to what Xueban applies live |
| `workflow.json` | Source Xueban workflow definition (replayable via `python -m xueban.cli run`) |
| `manifest.json` | Translation provenance (DAG→Team mapping) |
| `run.sh` | Cluster entrypoint: apply CR + wait for Ready + dispatch hint |

## Team composition
| Worker | Role | Runtime | Skills | Mode |
|---|---|---|---|---|
{worker_lines}

## Tasks (control loops)
{task_lines}

## Running
1. Install the AgentTeams CRDs and controller on your cluster.
2. `bash run.sh`
3. Dispatch the goal to the team's Matrix room (Xueban's `agentteams_bridge.MatrixClient`
   wraps login/room-create/message-send; see `xueban/runtime/agentteams_bridge.py`).
"""


def build_package_files(workflow: WorkflowDefinition, **translate_kwargs: Any) -> Dict[str, str]:
    team_spec = translate_dag_to_team(workflow, **translate_kwargs)
    return {
        "team-cr.yaml": render_team_cr(team_spec),
        "workflow.json": json.dumps(
            workflow.model_dump(mode="json"), ensure_ascii=False, indent=2
        ),
        "manifest.json": json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "translator": "xueban.runtime.agentteams_package.translate_dag_to_team",
                "team": team_spec,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "run.sh": render_run_sh(team_spec),
        "README.md": render_readme(team_spec),
    }


def export_team_package(workflow: WorkflowDefinition, out_dir: Path | str, **translate_kwargs: Any) -> Path:
    """Write the code package to ``out_dir`` (created if missing). Returns out_dir."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for filename, content in build_package_files(workflow, **translate_kwargs).items():
        (out / filename).write_text(content, encoding="utf-8", newline="\n")
    run_sh = out / "run.sh"
    try:
        run_sh.chmod(0o755)
    except OSError:
        # Windows filesystems may not support the exec bit; harmless for the artifact.
        pass
    return out


def export_team_package_zip(workflow: WorkflowDefinition, **translate_kwargs: Any) -> bytes:
    """Same package as an in-memory zip (HTTP download path)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, content in build_package_files(workflow, **translate_kwargs).items():
            zf.writestr(filename, content)
    return buffer.getvalue()


__all__ = [
    "translate_dag_to_team",
    "build_package_files",
    "export_team_package",
    "export_team_package_zip",
]

"""``.sfagent`` agent bundle export — GOAI M9 (agent upload/download share).

A bundle is a zip carrying the two competition-appendix manifests plus run
evidence, ready for upload (0G storage would mint the real CID; locally we
compute a deterministic sha256 content id over the canonical file set):

    sfagent.json    manifest: name/version/kind/capabilities + evidence refs + content id
    identity.yaml   附录A — Agent Identity 清单
    skills.yaml     附录B — Skill 清单
    trajectory.json run evidence (steps / loops / checkpoints summary)
    README.md       what this is, how to import

Deterministic: same inputs → identical zip (sorted keys, fixed date field),
so re-exports diff cleanly and the content id is stable.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from typing import Any, Dict, List, Optional

BUNDLE_FORMAT = "sfagent"
BUNDLE_VERSION = "1.0"


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _run_evidence(run_results: List[Any]) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    loops: List[Dict[str, Any]] = []
    checkpoints = 0
    artifacts = 0
    for result in run_results:
        for step in getattr(result, "steps", []):
            steps.append(
                {
                    "step_id": step.step_id,
                    "node_id": step.node_id,
                    "status": step.status,
                    "started_at": step.started_at.isoformat(),
                    "ended_at": step.ended_at.isoformat() if step.ended_at else None,
                }
            )
            output = step.output if isinstance(step.output, dict) else {}
            loop_record = output.get("loop_record")
            if loop_record:
                loops.append(
                    {
                        "loop_id": loop_record.get("loop_id"),
                        "status": loop_record.get("status"),
                        "iterations": loop_record.get("iteration_count"),
                        "body_mode": (loop_record.get("metadata") or {}).get("body_mode"),
                    }
                )
        checkpoints += len(getattr(result, "checkpoints", []) or [])
        artifacts += len(getattr(result, "artifacts", []) or [])
    return {
        "run_count": len(run_results),
        "step_count": len(steps),
        "checkpoint_count": checkpoints,
        "artifact_count": artifacts,
        "loops": loops,
        "steps": steps,
    }


def build_sfagent_files(
    agent: Dict[str, Any],
    *,
    run_results: Optional[List[Any]] = None,
    generated_at: str = "1970-01-01T00:00:00+00:00",
) -> Dict[str, str]:
    """Build the bundle's file set from an agent spec + optional run evidence.

    Agent spec (loose schema — anything an AgentGraph node or 附录A sheet has):
      id/name, role, description, kind (agent|tool|workflow), runtime,
      capabilities[], inputs[], outputs[], dependencies[], decision_boundary,
      skills[] ({name, purpose, input, output, invoke_when, dependencies,
      failure_handling, safety_boundary, reuse})
    """
    agent = dict(agent)
    agent_id = str(agent.get("id") or agent.get("name") or "agent")
    skills = [
        {
            "name": s.get("name", f"skill-{i}"),
            "purpose": s.get("purpose", ""),
            "input": s.get("input", ""),
            "output": s.get("output", ""),
            "invoke_when": s.get("invoke_when", ""),
            "dependencies": s.get("dependencies", []),
            "failure_handling": s.get("failure_handling", ""),
            "safety_boundary": s.get("safety_boundary", ""),
            "reuse": s.get("reuse", ""),
        }
        for i, s in enumerate(agent.get("skills") or [])
        if isinstance(s, dict)
    ]
    evidence = _run_evidence(list(run_results or []))

    identity = {
        "Name": agent.get("name") or agent_id,
        "Role": agent.get("role", ""),
        "Capabilities": agent.get("capabilities", []),
        "Inputs": agent.get("inputs", []),
        "Outputs": agent.get("outputs", []),
        "Dependencies": agent.get("dependencies", []),
        "Decision Boundary": agent.get("decision_boundary", ""),
        "Trace": evidence["steps"][:50],
    }

    files: Dict[str, str] = {
        "identity.yaml": _yaml_dump(identity),
        "skills.yaml": _yaml_dump({"skills": skills}),
        "trajectory.json": _canonical_json(
            {"agent_id": agent_id, "generated_at": generated_at, "evidence": evidence}
        ),
        "README.md": (
            f"# {identity['Name']} — .sfagent bundle\n\n"
            f"Format {BUNDLE_FORMAT} v{BUNDLE_VERSION}. Import via "
            "`python -m xueban.cli import-sfagent <zip>` (planned) or unzip and "
            "place identity/skills under .xueban/agents/.\n\n"
            f"Evidence: {evidence['run_count']} run(s), {evidence['step_count']} step(s), "
            f"{evidence['checkpoint_count']} checkpoint(s), {len(evidence['loops'])} loop(s).\n"
        ),
    }

    # content id: sha256 over the canonical file set (stable across re-exports)
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(files[name].encode("utf-8"))
    content_id = f"sha256:{digest.hexdigest()}"

    manifest = {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_VERSION,
        "agent_id": agent_id,
        "name": identity["Name"],
        "role": identity["Role"],
        "kind": agent.get("kind", "agent"),
        "runtime": agent.get("runtime", "xueban"),
        "description": agent.get("description", ""),
        "capabilities": agent.get("capabilities", []),
        "content_id": content_id,
        "generated_at": generated_at,
        "files": sorted(files),
        "evidence": {
            "run_count": evidence["run_count"],
            "step_count": evidence["step_count"],
            "checkpoint_count": evidence["checkpoint_count"],
            "artifact_count": evidence["artifact_count"],
            "loop_count": len(evidence["loops"]),
        },
    }
    files["sfagent.json"] = _canonical_json(manifest)
    return files


def export_sfagent_zip(
    agent: Dict[str, Any],
    *,
    run_results: Optional[List[Any]] = None,
    generated_at: str = "1970-01-01T00:00:00+00:00",
) -> bytes:
    files = build_sfagent_files(agent, run_results=run_results, generated_at=generated_at)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return buffer.getvalue()


def read_sfagent_zip(zip_bytes: bytes) -> Dict[str, Any]:
    """Parse a .sfagent bundle: {"manifest", "identity", "skills", "trajectory"}.

    Raises ValueError on malformed zips (missing sfagent.json etc.).
    """
    import yaml

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            if "sfagent.json" not in names:
                raise ValueError("bundle is missing sfagent.json")
            manifest = json.loads(zf.read("sfagent.json"))
            identity = yaml.safe_load(zf.read("identity.yaml")) if "identity.yaml" in names else {}
            skills = yaml.safe_load(zf.read("skills.yaml")) if "skills.yaml" in names else {}
            trajectory = (
                json.loads(zf.read("trajectory.json")) if "trajectory.json" in names else {}
            )
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a valid .sfagent zip: {exc}") from exc
    return {
        "manifest": manifest,
        "identity": identity or {},
        "skills": skills or {},
        "trajectory": trajectory or {},
    }


def import_sfagent_zip(zip_bytes: bytes, agents_dir) -> Dict[str, Any]:
    """Import a bundle into the agent store (`.xueban/agents`).

    Writes `<agent_id>.json` in the same shape the agents API reads
    ({"agent_id", "name", ...}); returns the stored record.
    """
    parsed = read_sfagent_zip(zip_bytes)
    manifest = parsed["manifest"]
    agent_id = str(manifest.get("agent_id") or manifest.get("name") or "agent")
    record = {
        "agent_id": agent_id,
        "name": parsed["identity"].get("Name") or manifest.get("name") or agent_id,
        "role": parsed["identity"].get("Role") or manifest.get("role") or "",
        "capabilities": parsed["identity"].get("Capabilities") or manifest.get("capabilities") or [],
        "kind": manifest.get("kind", "agent"),
        "runtime": manifest.get("runtime", "xueban"),
        "content_id": manifest.get("content_id"),
        "imported_from": "sfagent",
        "skills": parsed.get("skills", {}).get("skills") or [],
    }
    agents_dir.mkdir(parents=True, exist_ok=True)
    target = agents_dir / f"{agent_id}.json"
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**record, "path": str(target)}


def _yaml_dump(data: Any) -> str:
    """Tiny deterministic YAML writer (avoids a pyyaml dependency cycle in
    export paths; pyyaml is available but sorting/aliases differ by version)."""
    import yaml

    return yaml.safe_dump(data, allow_unicode=True, sort_keys=True, default_flow_style=False)


__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_VERSION",
    "build_sfagent_files",
    "export_sfagent_zip",
    "import_sfagent_zip",
    "read_sfagent_zip",
]

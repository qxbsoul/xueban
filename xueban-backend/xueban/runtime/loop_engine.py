"""Loop Engine v2 — DAG-native loop execution (design §8, loop-engine-design v0.3).

The loop body is now a **subgraph of the host DAG**, not a linear list:

- ``config.subgraph`` declares the body node set; execution follows the
  workflow's own edges between those nodes (conditional edges, back edges).
  With no edges among body nodes the legacy list-order chain applies, so
  existing workflows keep byte-identical behavior.
- Sensor (C) and Controller (A) are first-class graph node types
  (``control.sensor`` / ``control.controller``) that users can place inside
  the body DAG. When present they replace the engine's built-in post-body
  phases; when absent the legacy loop-level ``config.sensor`` /
  ``config.controller`` phases run unchanged (BYOK verifier included).
- ``config.preset`` expands a named body template — ``pdca`` is now one
  preset among several (``ooda``, ``generate_evaluate``), not the engine's
  hardwired shape.

Everything else from the v1 engine is preserved: LoopFrame fractal stack
(max_depth / depth_factor), loops-watching-loops, River Memory dredge/settle,
SEED-lite warm start, mid-loop resume (loop_progress), iteration-level reject,
IterationRecord evidence, CoE claims, NODE_RETRIED events.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from xueban.runtime.contracts import (
    EdgeDefinition,
    IterationRecord,
    LoopFrame,
    LoopRecord,
    NodeDefinition,
)

# ---------------------------------------------------------------------------
# Loop presets — an OPEN registry of body templates.
#
# The engine itself is preset-agnostic: a loop body is always a DAG subgraph.
# A preset is sugar that expands a named template into body nodes + chain
# edges. PDCA/OODA/generate_evaluate ship as bundled *examples* (marked
# ``example: true``) to demonstrate the format — they are special cases of
# the general DAG mechanism, not engine-native concepts.
#
# Three registration layers, later wins:
#   1. bundled examples (below)
#   2. YAML files via env ``SF_LOOP_PRESETS`` (os-pathsep separated) — the
#      zero-code authoring path, same philosophy as provider_presets.yaml
#   3. ``register_loop_preset()`` from Python
#
# YAML shape:
#   presets:
#     my_review_loop:
#       description: draft → review → fix until the reviewer converges
#       phases:
#         - {id: draft,  type: task,              role: Drafter}
#         - {id: review, type: control.sensor,    role: Reviewer}
#         - {id: fix,    type: control.controller, role: Fixer}
# ---------------------------------------------------------------------------

SENSOR_TYPE = "control.sensor"
CONTROLLER_TYPE = "control.controller"

# Internal body-input keys the engine injects before each walk. Prefixed with
# "_" so they can never collide with user variables.
_HISTORY_KEY = "_metric_history"
_RIVER_KEY = "_river_recall"
_SETPOINT_KEY = "setpoint"

# Phase node types allowed in a preset (task = plain body node).
PRESET_PHASE_TYPES = {"task", SENSOR_TYPE, CONTROLLER_TYPE}

_BUNDLED_EXAMPLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "pdca": {
        "description": "EXAMPLE — Plan → Do → Check(sensor) → Act(controller) cycle.",
        "example": True,
        "phases": [
            {"id": "plan", "type": "task", "role": "Planner"},
            {"id": "do", "type": "task", "role": "Executor"},
            {"id": "check", "type": SENSOR_TYPE, "role": "Checker"},
            {"id": "act", "type": CONTROLLER_TYPE, "role": "Controller"},
        ],
    },
    "ooda": {
        "description": "EXAMPLE — Observe → Orient → Decide(sensor) → Act(controller).",
        "example": True,
        "phases": [
            {"id": "observe", "type": "task", "role": "Observer"},
            {"id": "orient", "type": "task", "role": "Synthesizer"},
            {"id": "decide", "type": SENSOR_TYPE, "role": "Decider"},
            {"id": "act", "type": CONTROLLER_TYPE, "role": "Controller"},
        ],
    },
    "generate_evaluate": {
        "description": "EXAMPLE — Generate → Evaluate(sensor) → Revise(controller).",
        "example": True,
        "phases": [
            {"id": "generate", "type": "task", "role": "Generator"},
            {"id": "evaluate", "type": SENSOR_TYPE, "role": "Evaluator"},
            {"id": "revise", "type": CONTROLLER_TYPE, "role": "Reviser"},
        ],
    },
}

_USER_PRESETS: Dict[str, Dict[str, Any]] = {}
_yaml_presets_loaded = False


def _normalize_preset_spec(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a preset spec dict (from YAML or code) into canonical shape."""
    phases = spec.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ValueError(f"loop preset {name!r} must define a non-empty 'phases' list")
    normalized_phases = []
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError(f"loop preset {name!r}: each phase must be a mapping")
        phase_id = phase.get("id")
        phase_type = phase.get("type", "task")
        if not isinstance(phase_id, str) or not phase_id:
            raise ValueError(f"loop preset {name!r}: each phase needs a non-empty 'id'")
        if phase_type not in PRESET_PHASE_TYPES:
            raise ValueError(
                f"loop preset {name!r}: phase {phase_id!r} type must be one of "
                f"{sorted(PRESET_PHASE_TYPES)} (got {phase_type!r})"
            )
        normalized_phases.append(
            {"id": phase_id, "type": phase_type, "role": str(phase.get("role") or phase_id)}
        )
    return {
        "description": str(spec.get("description", "")),
        "example": bool(spec.get("example", False)),
        "phases": normalized_phases,
    }


def _load_yaml_presets() -> None:
    """Merge preset YAMLs listed in env SF_LOOP_PRESETS (pathsep separated)."""
    global _yaml_presets_loaded
    if _yaml_presets_loaded:
        return
    _yaml_presets_loaded = True
    import os

    raw = os.getenv("SF_LOOP_PRESETS")
    if not raw:
        return
    import yaml

    for path_str in raw.split(os.pathsep):
        path_str = path_str.strip()
        if not path_str:
            continue
        try:
            with open(path_str, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except OSError as exc:
            import logging

            logging.getLogger(__name__).warning("SF_LOOP_PRESETS entry unreadable (%s): %s", path_str, exc)
            continue
        presets = doc.get("presets") if isinstance(doc, dict) else None
        if not isinstance(presets, dict):
            continue
        for name, spec in presets.items():
            _USER_PRESETS[str(name)] = _normalize_preset_spec(str(name), spec)


def register_loop_preset(name: str, spec: Dict[str, Any], *, override: bool = True) -> None:
    """Register (or replace) a loop body preset — the open extension point.

    ``spec`` follows the YAML shape ({"description", "example", "phases"}).
    Set ``override=False`` to refuse replacing an existing registration.
    """
    if not override and (name in _USER_PRESETS or name in _BUNDLED_EXAMPLE_PRESETS):
        raise ValueError(f"loop preset {name!r} already registered")
    _USER_PRESETS[name] = _normalize_preset_spec(name, spec)


def get_loop_preset(name: str) -> Dict[str, Any]:
    _load_yaml_presets()
    if name in _USER_PRESETS:
        return _USER_PRESETS[name]
    if name in _BUNDLED_EXAMPLE_PRESETS:
        return _BUNDLED_EXAMPLE_PRESETS[name]
    raise ValueError(
        f"unknown loop preset {name!r} (registered: {sorted(set(_BUNDLED_EXAMPLE_PRESETS) | set(_USER_PRESETS))}); "
        "define one via register_loop_preset() or a SF_LOOP_PRESETS YAML file"
    )


def list_loop_presets() -> Dict[str, Dict[str, Any]]:
    _load_yaml_presets()
    merged = dict(_BUNDLED_EXAMPLE_PRESETS)
    merged.update(_USER_PRESETS)
    return merged


def preset_phase_node_ids(node_id: str, preset: str) -> List[str]:
    spec = get_loop_preset(preset)
    return [f"{node_id}-{phase['id']}" for phase in spec["phases"]]


def expand_preset_body(node: NodeDefinition) -> Tuple[List[NodeDefinition], List[EdgeDefinition]]:
    """Materialize a preset's body nodes and chain edges for a control.loop node.

    The loop-level ``config.sensor`` / ``config.controller`` / ``config.setpoint``
    are mapped onto the preset's sensor/controller phase nodes, so a BYOK
    verifier configured on the loop still applies when the preset expands.
    ``config.phases.<id>`` overrides any phase node's config.
    """
    preset = str(node.config.get("preset"))
    spec = get_loop_preset(preset)
    cfg = node.config
    setpoint = cfg.get("setpoint") if isinstance(cfg.get("setpoint"), dict) else {}
    sensor_cfg = cfg.get("sensor") if isinstance(cfg.get("sensor"), dict) else {}
    controller_cfg = cfg.get("controller") if isinstance(cfg.get("controller"), dict) else {}
    phase_overrides = cfg.get("phases") if isinstance(cfg.get("phases"), dict) else {}

    nodes: List[NodeDefinition] = []
    for phase in spec["phases"]:
        node_type = phase["type"]
        base_config: Dict[str, Any] = {"role": phase["role"]}
        if node_type == SENSOR_TYPE:
            base_config.update(
                {
                    "type": sensor_cfg.get("type", "threshold"),
                    "metric": setpoint.get("metric"),
                    "op": setpoint.get("op", ">="),
                    "value": setpoint.get("value"),
                }
            )
            verifier = sensor_cfg.get("verifier")
            if isinstance(verifier, dict):
                base_config["verifier"] = verifier
        elif node_type == CONTROLLER_TYPE:
            base_config.update(
                {k: v for k, v in controller_cfg.items() if k in {"type", "field", "rate", "init_value", "init", "freeze"}}
            )
        override = phase_overrides.get(phase["id"])
        if isinstance(override, dict):
            base_config.update(override)
        nodes.append(
            NodeDefinition(
                id=f"{node.id}-{phase['id']}",
                type=node_type,
                config=base_config,
            )
        )

    edges = [
        EdgeDefinition.model_validate({"from": nodes[i].id, "to": nodes[i + 1].id})
        for i in range(len(nodes) - 1)
    ]
    return nodes, edges


# ---------------------------------------------------------------------------
# Body plan — normalize preset / DAG-subgraph / legacy-linear into one shape.
# ---------------------------------------------------------------------------


class LoopBodyPlan:
    """Resolved loop body: a node set plus edge-following walk rules.

    mode:
      - "preset": generated from config.preset (ephemeral nodes, chain edges)
      - "dag":    subgraph ids + induced workflow edges (entry via config.entry
                  or the unique minimal-indegree body node)
      - "linear": subgraph ids with no induced edges → legacy list-order chain
    """

    def __init__(
        self,
        *,
        mode: str,
        nodes: List[NodeDefinition],
        edges: List[EdgeDefinition],
        entry: str,
        declared_ids: List[str],
    ) -> None:
        self.mode = mode
        self.nodes = nodes
        self.edges = edges
        self.entry = entry
        self.declared_ids = declared_ids
        self.nodes_by_id: Dict[str, NodeDefinition] = {n.id: n for n in nodes}
        self.body_set = {n.id for n in nodes}

    def compile_edges(self, compile_condition) -> Dict[str, List[Dict[str, Any]]]:
        edges_by_from: Dict[str, List[Dict[str, Any]]] = {}
        for edge in self.edges:
            edges_by_from.setdefault(edge.from_id, []).append(
                {"to_id": edge.to_id, "condition": compile_condition(edge.condition)}
            )
        return edges_by_from


def resolve_body_plan(
    node: NodeDefinition,
    nodes_by_id: Dict[str, NodeDefinition],
    workflow_edges: List[EdgeDefinition],
) -> LoopBodyPlan:
    cfg = node.config
    preset = cfg.get("preset")

    if isinstance(preset, str) and preset:
        body_nodes, body_edges = expand_preset_body(node)
        return LoopBodyPlan(
            mode="preset",
            nodes=body_nodes,
            edges=body_edges,
            entry=body_nodes[0].id,
            declared_ids=[n.id for n in body_nodes],
        )

    subgraph_ids = [nid for nid in cfg.get("subgraph", []) if isinstance(nid, str)]
    body_set = set(subgraph_ids)
    induced = [
        edge
        for edge in workflow_edges
        if edge.from_id in body_set and (edge.to_id in body_set or edge.to_id == "END")
    ]

    if not induced:
        # Legacy linear mode: list order is the chain.
        chain = [
            EdgeDefinition.model_validate({"from": subgraph_ids[i], "to": subgraph_ids[i + 1]})
            for i in range(len(subgraph_ids) - 1)
        ]
        return LoopBodyPlan(
            mode="linear",
            nodes=[nodes_by_id[nid] for nid in subgraph_ids],
            edges=chain,
            entry=subgraph_ids[0] if subgraph_ids else "",
            declared_ids=subgraph_ids,
        )

    declared_entry = cfg.get("entry")
    if isinstance(declared_entry, str) and declared_entry in body_set:
        entry = declared_entry
    else:
        incoming = {edge.to_id for edge in induced if edge.to_id != "END"}
        candidates = [nid for nid in subgraph_ids if nid not in incoming]
        entry = (candidates or subgraph_ids)[0]
    return LoopBodyPlan(
        mode="dag",
        nodes=[nodes_by_id[nid] for nid in subgraph_ids],
        edges=induced,
        entry=entry,
        declared_ids=subgraph_ids,
    )


# ---------------------------------------------------------------------------
# Sensor / Controller node execution (delegates back into RuntimeService).
# ---------------------------------------------------------------------------

_NODE_RESERVED_KEYS = {
    "role", "prompt", "message_template", "executor", "artifact",
    "gap_detection", "citation_required", "skills",
}


def _sensor_setpoint(node: NodeDefinition) -> Dict[str, Any]:
    cfg = node.config or {}
    explicit = cfg.get("setpoint")
    if isinstance(explicit, dict):
        return explicit
    return {
        "metric": cfg.get("metric"),
        "op": cfg.get("op", ">="),
        "value": cfg.get("value"),
    }


async def execute_sensor_node(svc, node: NodeDefinition, step_input: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a control.sensor graph node: observe, classify, judge.

    The node's own config IS the sensor config (type/metric/op/value/verifier…).
    ``_metric_history`` injected by the loop engine feeds trajectory sensors.
    Sensor types:
      - threshold / trajectory / verifier — control-theoretic verdicts
      - diagnostics (adoption #1, opencode-inspired): counts an LSP-style
        ``diagnostics`` list on the input (issue dicts with a ``severity``);
        value = issue count, converges when count <= target (default 0) —
        "agent reads inline diagnostics rather than recompiling".
    """
    cfg = node.config or {}
    setpoint = _sensor_setpoint(node)
    sensor_cfg = {k: v for k, v in cfg.items() if k not in _NODE_RESERVED_KEYS | {"setpoint"}}
    measurement = dict(step_input)
    raw_history = step_input.get(_HISTORY_KEY)
    history = [float(v) for v in raw_history if isinstance(v, (int, float))] if isinstance(raw_history, list) else []

    if sensor_cfg.get("type") == "diagnostics":
        raw_diags = measurement.get("diagnostics")
        issues = [d for d in raw_diags if isinstance(d, dict)] if isinstance(raw_diags, list) else []
        if sensor_cfg.get("include") == "errors":
            issues = [d for d in issues if str(d.get("severity", "error")).lower() == "error"]
        issue_count = len(issues)
        target = setpoint.get("value")
        max_issues = float(target) if isinstance(target, (int, float)) and not isinstance(target, bool) else 0.0
        result = {
            "sensor_type": "diagnostics",
            "value": float(issue_count),
            "converged": issue_count <= max_issues,
            "classification": "clean" if issue_count <= max_issues else "issues",
            "issue_count": issue_count,
            "severities": sorted({str(d.get("severity", "error")) for d in issues}),
        }
    else:
        result = svc._sense(sensor_cfg, setpoint, measurement, history)
        if sensor_cfg.get("type") == "verifier":
            result = await svc._sense_with_verifier(sensor_cfg, setpoint, measurement, history, result)

    output: Dict[str, Any] = {
        "message": cfg.get(
            "message_template",
            f"[{cfg.get('role', 'Sensor')}] {node.id} → value={result.get('value')} "
            f"converged={result.get('converged')}",
        ),
        "node_id": node.id,
        "node_type": SENSOR_TYPE,
        "handled_by": cfg.get("role", "Sensor"),
        "state": {},
        "converged": bool(result.get("converged")),
        "value": result.get("value"),
        "metric": setpoint.get("metric"),
        "sensor_type": result.get("sensor_type"),
        "classification": result.get("classification"),
        "sensor": result,
    }
    prompt = cfg.get("prompt")
    if isinstance(prompt, str) and prompt:
        output["prompt"] = prompt
    return output


def execute_controller_node(svc, node: NodeDefinition, step_input: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a control.controller graph node: actuate next-iteration variables.

    Reads the running ``controlled`` dict from the step input (injected by the
    loop engine) and the last sensor verdict's flat keys, then applies the
    same feedback policy the engine-level controller phase uses.
    """
    cfg = node.config or {}
    controller_cfg = {k: v for k, v in cfg.items() if k not in _NODE_RESERVED_KEYS}
    setpoint = step_input.get(_SETPOINT_KEY) if isinstance(step_input.get(_SETPOINT_KEY), dict) else {}
    controlled_in = step_input.get("controlled")
    if not isinstance(controlled_in, dict):
        controlled_in = controller_cfg.get("init") if isinstance(controller_cfg.get("init"), dict) else {}
    sensor_result = {"converged": step_input.get("converged"), "value": step_input.get("value")}
    freeze = controller_cfg.get("freeze") if isinstance(controller_cfg.get("freeze"), list) else []

    controlled = svc._control(controller_cfg, dict(controlled_in), sensor_result, setpoint, freeze)

    return {
        "message": cfg.get(
            "message_template",
            f"[{cfg.get('role', 'Controller')}] {node.id} → controlled={controlled}",
        ),
        "node_id": node.id,
        "node_type": CONTROLLER_TYPE,
        "handled_by": cfg.get("role", "Controller"),
        "state": {},
        "controlled": controlled,
        _SETPOINT_KEY: setpoint,
    }


# ---------------------------------------------------------------------------
# The loop executor (v2). RuntimeService._execute_loop_node delegates here.
# ---------------------------------------------------------------------------


async def execute_loop_node(
    svc,
    *,
    request,
    run_id: str,
    task,
    step_id: str,
    node: NodeDefinition,
    step_input: Dict[str, Any],
    state: Dict[str, Any],
    nodes_by_id: Dict[str, NodeDefinition],
    edges_by_from: Optional[Dict[str, List[Any]]] = None,
) -> Dict[str, Any]:
    """Execute a control.loop node over its DAG body until convergence/budget.

    The loop occupies a single main-walk step; body-node executions are captured
    as IterationRecords (not StepRecords). LoopFrame push/pop tracks the fractal
    call-stack in ``state["loop_frames"]``.
    """
    from xueban.runtime.events import NODE_RETRIED

    config = node.config
    loop_kind = config.get("kind", "until_converge")
    iteration_budget = int(config.get("iteration_budget", 1))
    setpoint = config.get("setpoint") if isinstance(config.get("setpoint"), dict) else {}
    role = config.get("role", node.type)
    prompt = config.get("prompt", "")
    max_depth = int(config.get("max_depth", 4))
    depth_factor = float(config.get("depth_factor", 0.6))
    watch = config.get("watch") if isinstance(config.get("watch"), list) else []

    workflow_edges = request.workflow.edges
    plan = resolve_body_plan(node, nodes_by_id, workflow_edges)
    plan_edges_by_from = plan.compile_edges(svc._compile_condition)

    active_frames: List[LoopFrame] = state.setdefault("loop_frames", [])
    current_depth = (active_frames[-1].depth + 1) if active_frames else 0
    parent_loop_id = active_frames[-1].loop_id if active_frames else None

    # Fractal depth bound (design §1.4 / §5.5): beyond max_depth a loop degrades
    # to a single iteration; otherwise the budget shrinks by depth_factor per
    # nesting level (Murray's-law analogue).
    if current_depth >= max_depth:
        effective_budget = 1
    else:
        effective_budget = max(1, int(iteration_budget * (depth_factor ** current_depth)))

    loop_record = LoopRecord(
        run_id=run_id,
        parent_loop_id=parent_loop_id,
        kind=loop_kind,
        setpoint=setpoint,
        depth=current_depth,
        sensor_binding=config.get("sensor_binding"),
        actuator_binding=config.get("actuator_binding"),
        metadata={"body_mode": plan.mode, "preset": config.get("preset")},
    )
    frame = LoopFrame(
        loop_id=loop_record.loop_id,
        index=0,
        setpoint=setpoint,
        depth=current_depth,
    )
    active_frames.append(frame)

    # LE-1 mid-loop resume: if this loop was interrupted, continue from the saved iteration.
    progress_map = state.setdefault("loop_progress", {})
    saved = progress_map.get(node.id)
    start_iteration = 0
    _resume_controlled: Optional[Dict[str, Any]] = None
    if isinstance(saved, dict):
        start_iteration = max(0, min(int(saved.get("next_iteration", 0)), effective_budget))
        for ir in saved.get("iterations", []):
            rec = IterationRecord.model_validate(ir) if isinstance(ir, dict) else ir
            loop_record.iterations.append(rec)
        loop_record.iteration_count = len(loop_record.iterations)
        if isinstance(saved.get("controlled"), dict):
            _resume_controlled = saved["controlled"]

    signals_map = state.setdefault("loop_signals", {})
    loop_local_visited: List[str] = []
    last_output: Dict[str, Any] = dict(step_input)
    converged = False
    halted_by_watch = False
    halted_by_request = False
    retried_count = 0

    sensor_cfg = config.get("sensor") if isinstance(config.get("sensor"), dict) else {}
    controller_cfg = config.get("controller") if isinstance(config.get("controller"), dict) else {}
    freeze_fields = config.get("freeze") if isinstance(config.get("freeze"), list) else []
    attribution_cfg = config.get("attribution") if isinstance(config.get("attribution"), dict) else {}
    coe_enabled = bool(attribution_cfg.get("coe"))
    controlled: Dict[str, Any] = {}
    if isinstance(controller_cfg.get("init"), dict):
        controlled.update(controller_cfg["init"])
    if _resume_controlled:
        controlled.update(_resume_controlled)
    metric_history: List[float] = []

    # River Memory (design §5.7): recall settled patterns on entry, settle a
    # reusable pattern on convergence.
    memory_cfg = config.get("memory") if isinstance(config.get("memory"), dict) else {}
    river_enabled = bool(memory_cfg.get("river"))
    recalled: List[Dict[str, Any]] = []
    if river_enabled:
        recalled = [
            c.model_dump(mode="json")
            for c in svc._river.dredge(
                tags=memory_cfg.get("tags"), limit=int(memory_cfg.get("recall", 3))
            )
        ]

    # SEED-lite (design §7.4): a memory_nudge controller warm-starts from a
    # prior settled pattern.
    if controller_cfg.get("type") == "memory_nudge" and recalled:
        field = controller_cfg.get("field") or setpoint.get("metric")
        try:
            factor = float(controller_cfg.get("warm_start_factor", 0.8))
        except (TypeError, ValueError):
            factor = 0.8
        for chunk in recalled:
            prior = (chunk.get("content") or {}).get("controlled") or {}
            if field in prior:
                try:
                    controlled[field] = round(float(prior[field]) * factor)
                except (TypeError, ValueError):
                    pass
                break

    async def _walk_body() -> Tuple[Dict[str, Any], Dict[str, Any], List[str], List[Dict[str, Any]], Dict[str, Any], int]:
        """One body walk: follow plan edges (conditions honored) from the entry.

        Returns (iteration_outputs, last_output, visited_order, sensor_results,
        controller_controlled, walk_steps). Back edges within the body re-walk
        their target inside the same iteration, bounded by a per-iteration walk
        budget of len(body)*3 (mirrors the main walk's step bound).
        """
        outputs: Dict[str, Any] = {}
        visited: List[str] = []
        sensor_results: List[Dict[str, Any]] = []
        controller_controlled: Dict[str, Any] = {}
        current: Optional[str] = plan.entry
        walk_last: Dict[str, Any] = dict(last_output)
        walk_budget = max(len(plan.nodes) * 3, 1)
        walk_steps = 0

        while current and current != "END" and walk_steps < walk_budget:
            body_node = plan.nodes_by_id.get(current)
            if body_node is None:
                break
            body_input: Dict[str, Any] = {**walk_last, **outputs, **controlled}
            if recalled:
                body_input[_RIVER_KEY] = recalled
            if metric_history:
                body_input[_HISTORY_KEY] = list(metric_history)
            body_input[_SETPOINT_KEY] = setpoint
            # Explicit control.controller nodes read/actuate the running
            # controlled dict through this key (engine-reserved name).
            body_input["controlled"] = controlled
            # Adoption #2: loop lineage for downstream provenance consumers
            # (ACP _meta.xueban, event log) — which loop, which iteration.
            body_input["_loop_provenance"] = {
                "loop_id": loop_record.loop_id,
                "iteration": iteration_index,
                "depth": current_depth,
                "parent_loop_id": parent_loop_id,
            }

            if body_node.type == "control.loop":
                body_output = await execute_loop_node(
                    svc=svc,
                    request=request,
                    run_id=run_id,
                    task=task,
                    step_id=step_id,
                    node=body_node,
                    step_input=body_input,
                    state=state,
                    nodes_by_id=nodes_by_id,
                    edges_by_from=edges_by_from,
                )
            else:
                body_output = await svc._execute_node(body_node, body_input, state, request.context)

            outputs[current] = body_output
            walk_last = body_output
            visited.append(current)
            if body_node.type == SENSOR_TYPE:
                sensor_results.append(body_output)
            elif body_node.type == CONTROLLER_TYPE:
                merged = body_output.get("controlled")
                if isinstance(merged, dict):
                    controller_controlled.update(merged)

            next_id: Optional[str] = None
            for edge in plan_edges_by_from.get(current, []):
                if svc._match_compiled_condition(edge["condition"], body_output, state):
                    to_id = edge["to_id"]
                    next_id = None if to_id == "END" or to_id not in plan.body_set else to_id
                    break
            current = next_id
            walk_steps += 1

        return outputs, walk_last, visited, sensor_results, controller_controlled, walk_steps

    try:
        for iteration_index in range(start_iteration, effective_budget):
            # LE-2 supervision (loops watching loops): halt if a watched loop
            # already emitted a matching signal earlier in the run.
            if svc._loop_watch_triggered(watch, signals_map):
                halted_by_watch = True
                break

            # E2 live halt: POST /runs/{run_id}/loops/{loop_id}/halt sets a
            # signal consumed here, at the iteration boundary.
            if svc._consume_loop_halt(run_id, loop_record.loop_id):
                halted_by_request = True
                break

            frame.index = iteration_index
            (
                iteration_outputs,
                walk_last_output,
                body_visited,
                sensor_results,
                controller_controlled,
                walk_steps,
            ) = await _walk_body()

            if body_visited:
                last_output = walk_last_output
                loop_local_visited.extend(body_visited)

            if controller_controlled:
                controlled = {**controlled, **controller_controlled}

            # C (Sensor): explicit control.sensor nodes win; otherwise the
            # legacy engine phase observes the merged body response.
            if sensor_results:
                last_sensor_node = sensor_results[-1]
                sensor_result = last_sensor_node.get("sensor") or {}
                sensor_value = last_sensor_node.get("value")
                if isinstance(sensor_value, (int, float)) and not isinstance(sensor_value, bool):
                    metric_history.append(float(sensor_value))
            else:
                measurement = {**last_output, **controlled}
                metric_value = svc._metric_value(measurement, setpoint)
                if metric_value is not None:
                    metric_history.append(metric_value)
                sensor_type = sensor_cfg.get("type", "threshold") if isinstance(sensor_cfg, dict) else "threshold"
                if sensor_type == "verifier":
                    sensor_result = await svc._sense_with_verifier(
                        sensor_cfg, setpoint, measurement, metric_history,
                        svc._sense(sensor_cfg, setpoint, measurement, metric_history)
                    )
                else:
                    sensor_result = svc._sense(sensor_cfg, setpoint, measurement, metric_history)

            metrics = svc._extract_loop_metrics({**last_output, **controlled}, setpoint)
            iteration_record = IterationRecord(
                loop_id=loop_record.loop_id,
                run_id=run_id,
                index=iteration_index,
                evidence={
                    "body_mode": plan.mode,
                    "body_node_ids": list(body_visited),
                    "walk_steps": walk_steps,
                    "last_output": svc._json_safe(last_output),
                    "controlled": svc._json_safe(controlled),
                    "sensor": sensor_result,
                    **({
                        "claims": svc._build_coe_claims(
                            {**last_output, **controlled}, sensor_result, body_visited
                        )
                    } if coe_enabled else {}),
                },
                metrics=metrics,
                verifier_score=sensor_result.get("value"),
                trace_id=run_id,
            )
            loop_record.iterations.append(iteration_record)
            loop_record.iteration_count += 1
            svc._emit_run_event(
                run_id,
                "loop.iteration",
                node_id=node.id,
                iteration_index=iteration_index,
                payload={
                    "loop_id": loop_record.loop_id,
                    "body_mode": plan.mode,
                    "converged": bool(sensor_result.get("converged")),
                    "verifier_score": sensor_result.get("value"),
                },
            )
            # Adoption #4 (Magentic-style task ledger): one durable fact per
            # iteration — what the loop is driving at and whether it's done.
            # The replay projection turns this stream into the ledger view.
            svc._emit_run_event(
                run_id,
                "task.ledger",
                node_id=node.id,
                iteration_index=iteration_index,
                payload={
                    "loop_id": loop_record.loop_id,
                    "goal": setpoint.get("metric"),
                    "target": setpoint.get("value"),
                    "done": bool(sensor_result.get("converged")),
                    "current": sensor_result.get("value"),
                    "actuated": svc._json_safe(controlled),
                },
            )

            # Persist progress so an interrupted run can resume from this iteration.
            progress_map[node.id] = {
                "next_iteration": iteration_index + 1,
                "iterations": [ir.model_dump(mode="json") for ir in loop_record.iterations],
                "controlled": svc._json_safe(controlled),
            }

            if loop_kind == "until_converge" and sensor_result.get("converged"):
                converged = True
                break

            # A (Controller): explicit control.controller nodes already actuated
            # during the walk; otherwise the legacy engine phase closes the loop.
            if not controller_controlled:
                controlled = svc._control(
                    controller_cfg, controlled, sensor_result, setpoint, freeze_fields
                )

            is_last = iteration_index >= effective_budget - 1
            if not is_last:
                retried_count += 1
                if svc._event_bus is not None:
                    svc._event_bus.publish_node_event(
                        run_id,
                        NODE_RETRIED,
                        node.id,
                        {
                            "loop_id": loop_record.loop_id,
                            "iteration": iteration_index,
                            "step_id": step_id,
                        },
                    )
    finally:
        if active_frames and active_frames[-1].loop_id == loop_record.loop_id:
            active_frames.pop()

    if loop_kind != "until_converge" and not halted_by_watch and not halted_by_request:
        converged = True

    loop_record.status = (
        "halted" if (halted_by_watch or halted_by_request) else ("converged" if converged else "halted")
    )
    svc._emit_run_event(
        run_id,
        "loop.completed",
        node_id=node.id,
        payload={
            "loop_id": loop_record.loop_id,
            "status": loop_record.status,
            "iterations": loop_record.iteration_count,
            "body_mode": plan.mode,
            "halted_by_request": halted_by_request,
        },
    )

    # River Memory: sediment a reusable pattern when the loop converges.
    settled = False
    if river_enabled and converged and not halted_by_watch:
        svc._river.settle(
            {
                "loop_id": loop_record.loop_id,
                "setpoint": setpoint,
                "controlled": svc._json_safe(controlled),
                "iterations": loop_record.iteration_count,
            },
            run_id=run_id,
            loop_id=loop_record.loop_id,
            tags=list(memory_cfg.get("tags") or []) + [loop_kind],
        )
        settled = True

    # Publish this loop's terminal signal for any supervisor watching it.
    signals_map[node.id] = loop_record.status
    # Loop completed: clear resumable progress so a fresh re-run starts from iteration 0.
    progress_map.pop(node.id, None)

    output: Dict[str, Any] = {
        "message": config.get(
            "message_template",
            f"[{role}] loop {loop_record.loop_id} ran {loop_record.iteration_count} "
            f"iteration(s); status={loop_record.status}",
        ),
        "node_id": node.id,
        "node_type": node.type,
        "handled_by": role,
        "state": {
            "last_loop_id": loop_record.loop_id,
            "loop_status": loop_record.status,
            "loop_iterations": loop_record.iteration_count,
        },
        "loop_id": loop_record.loop_id,
        "loop_kind": loop_kind,
        "loop_body_mode": plan.mode,
        "loop_preset": config.get("preset"),
        "loop_depth": current_depth,
        "loop_iterations": loop_record.iteration_count,
        "loop_status": loop_record.status,
        "loop_converged": converged,
        "loop_retried_count": retried_count,
        "loop_halted_by_watch": halted_by_watch,
        "loop_halted_by_request": halted_by_request,
        "loop_memory_recalled": len(recalled),
        "loop_memory_settled": settled,
        "loop_local_visited": loop_local_visited,
        "loop_record": loop_record.model_dump(mode="json"),
    }
    if prompt:
        output["prompt"] = prompt
    return output


__all__ = [
    "CONTROLLER_TYPE",
    "LoopBodyPlan",
    "PRESET_PHASE_TYPES",
    "SENSOR_TYPE",
    "execute_controller_node",
    "execute_loop_node",
    "execute_sensor_node",
    "expand_preset_body",
    "get_loop_preset",
    "list_loop_presets",
    "preset_phase_node_ids",
    "register_loop_preset",
    "resolve_body_plan",
]

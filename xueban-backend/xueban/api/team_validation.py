"""Team write-time validation — D1+D2.

Pure-function validators used by `xueban.api.teams` when a client writes a
team's workflow / membership. Kept dependency-free (no FastAPI, no filesystem)
so they are trivially unit-testable.

Semantics mirror the Node reference `server/src/lib/team-yaml.ts:validateDag()`:

  * Edge endpoints (``from`` / ``to``) must be members of the team.
  * Cycle detection runs over **sequential** edges only — ``conditional`` and
    ``parallel`` edges are excluded (a conditional edge is the legitimate
    retry-back / branch mechanism, so it may legally point "backwards").
    An edge with no ``kind`` is treated as ``sequential`` (same default the
    Node loader applies).

D1 additionally rejects self-loops (``from == to``); the Node loader never
emits self-loops from its yaml shape, but a raw JSON PUT could, so we guard.

D2 (`validate_agent_ids`) checks that every referenced agent id resolves in
the Python agent store. The store lookup is injected as a callable so the
validator stays pure and the caller controls (and tests control) where agents
are read from.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List


# Edge kinds that participate in cycle detection. A missing/unknown kind is
# treated as sequential, matching the Node default.
_CYCLE_KINDS = {"sequential"}


def _edge_endpoints(edge: Any) -> tuple[Any, Any] | None:
    """Return (from, to) for a well-formed edge dict, else None."""
    if not isinstance(edge, dict):
        return None
    src = edge.get("from")
    dst = edge.get("to")
    if not isinstance(src, str) or not isinstance(dst, str):
        return None
    return src, dst


def validate_dag(members: List[str], edges: List[Any]) -> List[str]:
    """Validate DAG structure. Returns a list of human-readable error strings
    (empty == valid). Never raises on malformed input — malformed edges are
    reported as errors so the caller can 4xx rather than 500.

    Checks (order is stable for predictable error messages):
      1. each edge is a well-formed {from, to} pair
      2. no self-loop (from == to)
      3. both endpoints are team members
      4. no cycle via sequential edges (conditional/parallel excluded)
    """
    errs: List[str] = []
    member_set = set(members)

    # 1+2+3: per-edge structural checks
    clean_edges: List[Dict[str, Any]] = []
    for raw in edges:
        pair = _edge_endpoints(raw)
        if pair is None:
            errs.append(f"malformed edge entry (needs string 'from'/'to'): {raw!r}")
            continue
        src, dst = pair
        if src == dst:
            errs.append(f'self-loop edge "{src}" → "{dst}" is not allowed')
            # still record it below so it doesn't silently vanish, but a
            # self-loop is also a 1-node cycle; skip adding to cycle graph.
            continue
        if src not in member_set:
            errs.append(f'edge "{src}" → "{dst}": "{src}" is not a team member')
        if dst not in member_set:
            errs.append(f'edge "{src}" → "{dst}": "{dst}" is not a team member')
        kind = raw.get("kind")
        clean_edges.append({"from": src, "to": dst, "kind": kind})

    # 4: cycle detection over sequential edges only
    seq_edges = [
        e for e in clean_edges if (e["kind"] in _CYCLE_KINDS or e["kind"] is None)
    ]
    cycle = _find_cycle(members, seq_edges)
    if cycle:
        errs.append("cycle detected via sequential edges: " + " → ".join(cycle))

    return errs


def _find_cycle(nodes: List[str], edges: List[Dict[str, Any]]) -> List[str] | None:
    """DFS cycle finder. Returns the cycle path (closing node repeated) or None.

    Mirrors `team-yaml.ts:findCycle`. Edges whose endpoints aren't in `nodes`
    are still added (the endpoint-membership error is reported separately), so
    a cycle can be found even on partially-invalid graphs — matching Node.
    """
    graph: Dict[str, List[str]] = {n: [] for n in nodes}
    for e in edges:
        graph.setdefault(e["from"], []).append(e["to"])

    visited: set[str] = set()
    stack: set[str] = set()
    path: List[str] = []

    def dfs(n: str) -> List[str] | None:
        if n in stack:
            start = path.index(n)
            return path[start:] + [n]
        if n in visited:
            return None
        visited.add(n)
        stack.add(n)
        path.append(n)
        for nxt in graph.get(n, []):
            r = dfs(nxt)
            if r:
                return r
        stack.discard(n)
        path.pop()
        return None

    # iterate over all graph keys (nodes + any edge sources) for completeness
    for n in list(graph.keys()):
        if n not in visited:
            r = dfs(n)
            if r:
                return r
    return None


def _map_mode(mode: Any) -> str:
    """Map a workflow edge `data.mode` to an edge kind, mirroring Node
    `team-source.ts:mapMode`: 'conditional'/'parallel' pass through, everything
    else ('direct'/None/unknown) → 'sequential'.
    """
    if mode == "conditional":
        return "conditional"
    if mode == "parallel":
        return "parallel"
    return "sequential"


def validate_workflow(workflow: Dict[str, Any]) -> List[str]:
    """Validate a team workflow graph in the *stored* shape:

        workflow = {
          "nodes": [{"id": str, "data": {"agentId": str}}, ...],
          "edges": [{"source": str, "target": str, "data": {"mode": str}}, ...],
        }

    Edges reference **node ids** (source/target), and `data.mode` selects the
    edge kind. This is the shape `team-source.ts` reads at run time, so we
    validate against node ids (not agent ids) — exactly matching the Node
    run-shape mapping.

    Returns error strings (empty == valid). Does NOT raise.
    """
    if not isinstance(workflow, dict):
        return ["workflow must be an object with 'nodes' and 'edges'"]

    raw_nodes = workflow.get("nodes") or []
    raw_edges = workflow.get("edges") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return ["workflow.nodes and workflow.edges must be arrays"]

    node_ids: List[str] = []
    for n in raw_nodes:
        if isinstance(n, dict) and isinstance(n.get("id"), str):
            node_ids.append(n["id"])

    # Translate stored {source,target,data.mode} edges into the generic
    # {from,to,kind} shape `validate_dag` understands.
    generic_edges: List[Any] = []
    for e in raw_edges:
        if not isinstance(e, dict):
            generic_edges.append(e)  # let validate_dag report it as malformed
            continue
        src = e.get("source")
        dst = e.get("target")
        mode = (e.get("data") or {}).get("mode") if isinstance(e.get("data"), dict) else None
        generic_edges.append({"from": src, "to": dst, "kind": _map_mode(mode)})

    return validate_dag(members=node_ids, edges=generic_edges)


def workflow_agent_ids(workflow: Dict[str, Any]) -> List[str]:
    """Extract the non-empty `data.agentId` values referenced by workflow nodes.

    Empty agentId (e.g. a coordinator placeholder node) is intentionally
    skipped — those are not agent references and must not be rejected.
    """
    out: List[str] = []
    if not isinstance(workflow, dict):
        return out
    for n in workflow.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        data = n.get("data")
        if isinstance(data, dict):
            aid = data.get("agentId")
            if isinstance(aid, str) and aid.strip():
                out.append(aid)
    return out


def validate_agent_ids(
    agent_ids: List[str],
    exists: Callable[[str], bool],
) -> List[str]:
    """D2: every agent id must resolve in the agent store.

    `exists` is injected so this stays pure & testable; the API layer passes a
    closure over the real agent dir. Returns error strings for each dangling
    reference (empty == all resolvable).
    """
    errs: List[str] = []
    for aid in agent_ids:
        if not exists(aid):
            errs.append(
                f'agent "{aid}" does not exist in the agent store '
                f"(referenced by team but not found)"
            )
    return errs


def agent_exists_in_store(agent_id: str) -> bool:
    """Default agent-existence probe against the Python agent store.

    Reads through the live `xueban.api.agents._AGENTS_DIR` module attribute
    so that test monkeypatching of `_AGENTS_DIR` is honoured (the value is
    looked up at call time, not import time).
    """
    # Imported lazily and read via attribute access so monkeypatch on the
    # module attribute takes effect (don't bind the path at import time).
    from xueban.api import agents as _agents_mod

    agents_dir = _agents_mod._AGENTS_DIR
    try:
        return (agents_dir / f"{agent_id}.json").exists()
    except OSError:
        return False

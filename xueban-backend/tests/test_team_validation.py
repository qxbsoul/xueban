"""D1+D2 — Team write-time validation unit tests.

Pure-function tests for `xueban.api.team_validation`:
  D1: DAG structural validation (cycle, dangling endpoints, self-loop),
      semantics mirror Node `server/src/lib/team-yaml.ts:validateDag()`.
  D2: agent_id existence check against the Python agent store.

These are *pure* (no FastAPI / TestClient) so they don't touch any real
`.xueban/` directory. The agent-existence helper is parameterised on a
lookup callable so we never read the real agents dir here.
"""

from __future__ import annotations

import pytest

from xueban.api import team_validation as tv


# ---------------------------------------------------------------------------
# D1 — DAG structure
# ---------------------------------------------------------------------------


class TestNoCycle:
    def test_simple_chain_ok(self):
        errs = tv.validate_dag(
            members=["a", "b", "c"],
            edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
        )
        assert errs == []

    def test_two_node_cycle_rejected(self):
        errs = tv.validate_dag(
            members=["a", "b"],
            edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
        )
        assert any("cycle" in e.lower() for e in errs)

    def test_three_node_cycle_rejected(self):
        errs = tv.validate_dag(
            members=["a", "b", "c"],
            edges=[
                {"from": "a", "to": "b"},
                {"from": "b", "to": "c"},
                {"from": "c", "to": "a"},
            ],
        )
        assert any("cycle" in e.lower() for e in errs)

    def test_conditional_back_edge_allowed(self):
        """Conditional edges are excluded from cycle detection (mirrors Node)."""
        errs = tv.validate_dag(
            members=["a", "b"],
            edges=[
                {"from": "a", "to": "b", "kind": "sequential"},
                {"from": "b", "to": "a", "kind": "conditional"},
            ],
        )
        # the conditional back-edge must NOT be reported as a cycle
        assert not any("cycle" in e.lower() for e in errs)

    def test_parallel_back_edge_does_not_trip_cycle(self):
        """Only sequential (or kind-less) edges participate in cycle check."""
        errs = tv.validate_dag(
            members=["a", "b"],
            edges=[
                {"from": "a", "to": "b", "kind": "sequential"},
                {"from": "b", "to": "a", "kind": "parallel"},
            ],
        )
        assert not any("cycle" in e.lower() for e in errs)


class TestDanglingEndpoints:
    def test_from_not_member_rejected(self):
        errs = tv.validate_dag(
            members=["a", "b"],
            edges=[{"from": "ghost", "to": "b"}],
        )
        assert any("ghost" in e for e in errs)

    def test_to_not_member_rejected(self):
        errs = tv.validate_dag(
            members=["a", "b"],
            edges=[{"from": "a", "to": "ghost"}],
        )
        assert any("ghost" in e for e in errs)


class TestSelfLoop:
    def test_self_loop_rejected(self):
        errs = tv.validate_dag(
            members=["a", "b"],
            edges=[{"from": "a", "to": "a"}],
        )
        assert any("self" in e.lower() for e in errs)


class TestEmptyAndMalformed:
    def test_no_edges_ok(self):
        assert tv.validate_dag(members=["a"], edges=[]) == []

    def test_malformed_edge_missing_to(self):
        errs = tv.validate_dag(members=["a", "b"], edges=[{"from": "a"}])
        assert errs  # something is reported, not a crash

    def test_malformed_edge_not_a_dict(self):
        errs = tv.validate_dag(members=["a", "b"], edges=["not-a-dict"])
        assert errs


# ---------------------------------------------------------------------------
# D1 — stored workflow shape ({source,target,data.mode} over node ids)
# ---------------------------------------------------------------------------


def _node(node_id: str, agent_id: str = "") -> dict:
    return {"id": node_id, "data": {"agentId": agent_id}}


def _wf_edge(src: str, dst: str, mode: str = "direct") -> dict:
    return {"source": src, "target": dst, "data": {"mode": mode}}


class TestValidateWorkflow:
    def test_valid_chain(self):
        wf = {
            "nodes": [_node("a"), _node("b"), _node("c")],
            "edges": [_wf_edge("a", "b"), _wf_edge("b", "c")],
        }
        assert tv.validate_workflow(wf) == []

    def test_direct_mode_cycle_rejected(self):
        """'direct' maps to sequential — a direct back-edge is a cycle."""
        wf = {
            "nodes": [_node("a"), _node("b")],
            "edges": [_wf_edge("a", "b"), _wf_edge("b", "a")],
        }
        errs = tv.validate_workflow(wf)
        assert any("cycle" in e.lower() for e in errs)

    def test_conditional_back_edge_allowed(self):
        wf = {
            "nodes": [_node("a"), _node("b")],
            "edges": [
                _wf_edge("a", "b", "direct"),
                _wf_edge("b", "a", "conditional"),
            ],
        }
        assert not any("cycle" in e.lower() for e in tv.validate_workflow(wf))

    def test_edge_to_unknown_node_rejected(self):
        wf = {
            "nodes": [_node("a")],
            "edges": [_wf_edge("a", "ghost")],
        }
        errs = tv.validate_workflow(wf)
        assert any("ghost" in e for e in errs)

    def test_self_loop_rejected(self):
        wf = {
            "nodes": [_node("a"), _node("b")],
            "edges": [_wf_edge("a", "a")],
        }
        assert any("self" in e.lower() for e in tv.validate_workflow(wf))

    def test_empty_workflow_ok(self):
        assert tv.validate_workflow({"nodes": [], "edges": []}) == []

    def test_non_dict_workflow(self):
        assert tv.validate_workflow("nope")  # reports error, no crash


class TestWorkflowAgentIds:
    def test_extracts_non_empty_agent_ids(self):
        wf = {
            "nodes": [
                _node("coordinator", ""),  # empty → skipped
                _node("planner", "agent-1"),
                _node("writer", "agent-2"),
            ],
            "edges": [],
        }
        assert tv.workflow_agent_ids(wf) == ["agent-1", "agent-2"]

    def test_empty_workflow(self):
        assert tv.workflow_agent_ids({"nodes": [], "edges": []}) == []


# ---------------------------------------------------------------------------
# D2 — agent_id existence
# ---------------------------------------------------------------------------


class TestAgentExistence:
    def test_all_exist_ok(self):
        existing = {"agent-1", "agent-2"}
        errs = tv.validate_agent_ids(
            ["agent-1", "agent-2"], exists=lambda a: a in existing
        )
        assert errs == []

    def test_missing_agent_reported(self):
        existing = {"agent-1"}
        errs = tv.validate_agent_ids(
            ["agent-1", "agent-missing"], exists=lambda a: a in existing
        )
        assert any("agent-missing" in e for e in errs)

    def test_multiple_missing_all_reported(self):
        errs = tv.validate_agent_ids(
            ["x", "y"], exists=lambda a: False
        )
        assert any("x" in e for e in errs)
        assert any("y" in e for e in errs)

    def test_empty_list_ok(self):
        assert tv.validate_agent_ids([], exists=lambda a: True) == []

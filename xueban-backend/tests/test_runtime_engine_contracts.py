import asyncio
from pathlib import Path

from xueban.runtime import ChildRunRequest, MarkdownWritebackAdapter, ResumeRequest, RuntimeRequest, RuntimeService, WorkflowDefinition
from tests.test_runtime_markdown_cli import _install_fake_path_fs

MANUAL_REVIEW_OVERRIDE = {"activation_overrides": {"nodes": ["reviewer"]}}


WORKFLOW_PAYLOAD = {
    "workflow_id": "engine-contracts",
    "version": "0.1",
    "name": "Engine Contracts",
    "entrypoint": "planner",
    "nodes": [
        {
            "id": "planner",
            "kind": "agent",
            "type": "agent.execute",
            "config": {
                "role": "planner",
                "message_template": "[planner] planned the work.",
                "local_activation": {
                    "mode": "local",
                    "tags": ["planning", "core"],
                    "delegate_candidates": ["reviewer"],
                    "feedback_channels": ["artifact", "checkpoint", "handoff"],
                    "review_gates": ["requires_review"],
                },
                "assignment": {"handoff_goal": "Give reviewer enough structure to assess the plan."},
                "emit": {
                    "artifact": {
                        "kind": "report",
                        "name": "plan.md",
                        "content": "# Plan\n\nStructured plan output.",
                    }
                },
            },
        },
        {
            "id": "reviewer",
            "kind": "agent",
            "type": "agent.execute",
            "config": {
                "role": "reviewer",
                "message_template": "[reviewer] reviewed the plan.",
                "local_activation": {
                    "mode": "manual",
                    "tags": ["review"],
                    "feedback_channels": ["step_result"],
                },
            },
        },
    ],
    "edges": [
        {"from": "planner", "to": "reviewer", "type": "default"},
        {"from": "reviewer", "to": "END", "type": "final"},
    ],
}

CHILD_WORKFLOW_PAYLOAD = {
    "workflow_id": "child-engine-contracts",
    "version": "0.1",
    "name": "Child Engine Contracts",
    "entrypoint": "child_planner",
    "nodes": [
        {
            "id": "child_planner",
            "kind": "agent",
            "type": "agent.execute",
            "config": {
                "role": "child-planner",
                "message_template": "[child-planner] completed delegated work.",
                "context_echo": ["repo", "owner"],
            },
        }
    ],
    "edges": [{"from": "child_planner", "to": "END", "type": "final"}],
}

DELEGATED_NODE_WORKFLOW_PAYLOAD = {
    "workflow_id": "delegated-node-parent",
    "version": "0.1",
    "name": "Delegated Node Parent",
    "entrypoint": "delegator",
    "nodes": [
        {
            "id": "delegator",
            "kind": "agent",
            "type": "agent.execute",
            "config": {
                "role": "delegator",
                "message_template": "[delegator] child run finished.",
                "assignment": {"handoff_goal": "Return delegated analysis to the reviewer."},
                "delegated": {
                    "workflow": CHILD_WORKFLOW_PAYLOAD,
                    "context_mode": "inherit",
                    "task_title": "Delegated Child Task",
                    "context": {"owner": "delegated-node"},
                },
            },
        },
        {
            "id": "reviewer",
            "kind": "agent",
            "type": "agent.execute",
            "config": {
                "role": "reviewer",
                "message_template": "[reviewer] consumed delegated result.",
            },
        },
    ],
    "edges": [
        {"from": "delegator", "to": "reviewer", "type": "default"},
        {"from": "reviewer", "to": "END", "type": "final"},
    ],
}


def test_run_result_contains_task_handoff_and_memory_events():
    workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Stabilize the engine contracts"},
                metadata={"task_title": "Contract stabilization", **MANUAL_REVIEW_OVERRIDE},
            )
        )
    )

    assert result.run.status == "succeeded"
    assert len(result.tasks) == 1
    assert result.tasks[0].title == "Contract stabilization"
    assert result.tasks[0].status == "succeeded"

    assert len(result.handoffs) == 1
    assert result.handoffs[0].from_node_id == "planner"
    assert result.handoffs[0].to_node_id == "reviewer"
    assert result.handoffs[0].goal == "Give reviewer enough structure to assess the plan."
    assert result.handoffs[0].artifact_ids == [result.artifacts[0].artifact_id]
    assert len(result.activation_candidates) == 3
    assert result.activation_candidates[0].candidate_type == "node"
    assert result.activation_candidates[0].selected is True
    assert any(
        candidate.candidate_type == "delegate_target" and candidate.candidate_ref == "reviewer"
        for candidate in result.activation_candidates
    )
    assert len(result.activations) == 2
    assert result.activations[0].mode == "local"
    assert result.activations[0].delegate_candidates == ["reviewer"]
    assert result.activations[0].metadata["candidate_ids"]
    assert result.steps[0].metadata["activation_id"] == result.activations[0].activation_id
    assert result.steps[0].metadata["activation_candidate_ids"]
    assert result.steps[0].metadata["selected_candidate_ids"]
    assert result.steps[0].metadata["activation_mode"] == "local"
    assert result.steps[0].metadata["feedback_ids"]
    assert len(result.feedback) == 3
    assert result.feedback[0].source_type == "step"
    assert result.feedback[0].signals["candidate_count"] == 2
    assert result.feedback[0].signals["selected_candidate_count"] == 1
    assert result.feedback[0].signals["artifact_count"] == 1
    assert result.feedback[0].signals["checkpoint_id"] == result.checkpoints[0].checkpoint_id
    assert result.feedback[-1].source_type == "run"
    assert result.feedback[-1].signals["activation_candidate_count"] == 3
    assert result.feedback[-1].signals["activation_count"] == 2

    categories = {event.category for event in result.memory_events}
    assert {
        "task",
        "step_result",
        "artifact",
        "checkpoint",
        "handoff",
        "activation",
        "feedback_signal",
        "run_summary",
    } <= categories


def test_file_run_store_round_trips_engine_contract_objects(monkeypatch):
    workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)
    files, _ = _install_fake_path_fs(monkeypatch)
    root = Path(".test-runtime-engine-contracts")
    adapter = MarkdownWritebackAdapter(root)
    service = RuntimeService(writeback_adapter=adapter, checkpoint_store=adapter.checkpoint_store)

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Persist engine contracts"},
                metadata={"task_title": "Persistence check", **MANUAL_REVIEW_OVERRIDE},
            )
        )
    )

    reloaded = adapter.run_store.get(result.run.run_id)
    assert reloaded is not None
    assert len(reloaded.tasks) == 1
    assert reloaded.tasks[0].title == "Persistence check"
    assert len(reloaded.handoffs) == 1
    assert reloaded.handoffs[0].to_node_id == "reviewer"
    assert len(reloaded.activation_candidates) == 3
    assert len(reloaded.activations) == 2
    assert len(reloaded.feedback) == 3
    assert any(event.category == "run_summary" for event in reloaded.memory_events)

    run_file = root / "runs" / f"{result.run.run_id}.json"
    assert str(run_file).replace("\\", "/") in files
    assert '"memory_events"' in files[str(run_file).replace("\\", "/")]
    assert '"activation_candidates"' in files[str(run_file).replace("\\", "/")]
    assert '"activations"' in files[str(run_file).replace("\\", "/")]
    assert '"feedback"' in files[str(run_file).replace("\\", "/")]


def test_repeated_runs_reuse_compiled_routing_with_stateful_conditions():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "compiled-routing-cache",
            "version": "0.1",
            "name": "Compiled Routing Cache",
            "entrypoint": "planner",
            "nodes": [
                {
                    "id": "planner",
                    "kind": "agent",
                    "type": "planning.analyze",
                    "config": {
                        "role": "planner",
                        "message_template": "[planner] prepared the route.",
                        "emit": {"score": 8},
                        "set_state": {"environment": "prod"},
                    },
                },
                {
                    "id": "approved",
                    "kind": "agent",
                    "type": "review.summarize",
                    "config": {
                        "role": "reviewer",
                        "message_template": "[reviewer] approved the route.",
                    },
                },
                {
                    "id": "fallback",
                    "kind": "agent",
                    "type": "review.summarize",
                    "config": {
                        "role": "reviewer",
                        "message_template": "[reviewer] fallback route.",
                    },
                },
            ],
            "edges": [
                {
                    "from": "planner",
                    "to": "approved",
                    "type": "conditional",
                    "condition": "result.score >= 8 and state.environment == 'prod'",
                },
                {"from": "planner", "to": "fallback", "type": "default"},
                {"from": "approved", "to": "END", "type": "final"},
                {"from": "fallback", "to": "END", "type": "final"},
            ],
        }
    )
    service = RuntimeService()

    first = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "compiled route 1"},
                metadata={"task_title": "Compiled Route 1"},
            )
        )
    )
    second = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "compiled route 2"},
                metadata={"task_title": "Compiled Route 2"},
            )
        )
    )

    assert [step.node_id for step in first.steps] == ["planner", "approved"]
    assert [step.node_id for step in second.steps] == ["planner", "approved"]
    assert first.final_output["message"] == "[reviewer] approved the route."
    assert second.final_output["message"] == "[reviewer] approved the route."


def test_checkpoint_payload_omits_reconstructable_and_transient_state():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "checkpoint-slimming",
            "version": "0.1",
            "name": "Checkpoint Slimming",
            "entrypoint": "fanout",
            "nodes": [
                {
                    "id": "fanout",
                    "kind": "node",
                    "type": "control.parallel",
                    "config": {
                        "role": "dispatcher",
                        "branches": ["branch_a", "branch_b"],
                        "barrier": "merge",
                    },
                },
                {
                    "id": "branch_a",
                    "kind": "agent",
                    "type": "agent.execute",
                    "config": {"role": "a", "message_template": "a"},
                },
                {
                    "id": "branch_b",
                    "kind": "agent",
                    "type": "agent.execute",
                    "config": {"role": "b", "message_template": "b"},
                },
                {
                    "id": "merge",
                    "kind": "node",
                    "type": "control.barrier",
                    "config": {"role": "merge", "source_parallel": "fanout", "message_template": "merge"},
                },
            ],
            "edges": [{"from": "merge", "to": "END", "type": "final"}],
        }
    )
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "checkpoint slimming"},
                context={"source_system": "pytest"},
            )
        )
    )

    shared_state = result.checkpoints[0].state.state["shared_state"]
    assert "input" not in shared_state
    assert "context" not in shared_state
    assert "workflow_id" not in shared_state
    assert "root_input" not in shared_state
    assert "artifacts_by_step" not in shared_state
    assert "branch_set" not in shared_state["parallel"]["fanout"]


def test_runtime_service_exports_typed_projection_graphs():
    workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Project the runtime graph"},
                metadata={"task_title": "Projection Test", **MANUAL_REVIEW_OVERRIDE},
            )
        )
    )

    task_tree = service.export_task_tree(result.run.run_id)
    assert task_tree is not None
    assert task_tree.projection_kind == "task_tree"
    assert task_tree.scope.run_id == result.run.run_id
    assert any(node.entity_type == "run" for node in task_tree.nodes)
    assert any(node.entity_type == "task" for node in task_tree.nodes)
    assert any(node.entity_type == "step" for node in task_tree.nodes)
    assert any(edge.edge_type == "belongs_to_task" for edge in task_tree.edges)
    assert "graph projection" in task_tree.metadata["projection_note"]
    assert task_tree.summary["activation_candidate_count"] == len(result.activation_candidates)
    assert task_tree.summary["activation_count"] == len(result.activations)
    assert task_tree.summary["feedback_count"] == len(result.feedback)

    artifact_lineage = service.export_artifact_lineage(run_id=result.run.run_id)
    assert artifact_lineage is not None
    assert artifact_lineage.projection_kind == "artifact_lineage_graph"
    assert artifact_lineage.summary["artifact_count"] == 1
    assert any(node.entity_type == "artifact" for node in artifact_lineage.nodes)
    assert any(edge.edge_type == "produces_artifact" for edge in artifact_lineage.edges)

    memory_graph = service.export_memory_relation_graph(result.run.run_id)
    assert memory_graph is not None
    assert memory_graph.projection_kind == "memory_relation_graph"
    assert any(node.entity_type == "memory_event" for node in memory_graph.nodes)
    assert any(node.entity_type == "activation_candidate" for node in memory_graph.nodes)
    assert any(node.entity_type == "activation" for node in memory_graph.nodes)
    assert any(node.entity_type == "feedback_signal" for node in memory_graph.nodes)
    assert any(edge.edge_type == "emits_memory_event" for edge in memory_graph.edges)
    assert any(edge.edge_type == "candidate_for_activation" for edge in memory_graph.edges)
    assert any(edge.edge_type == "activates" for edge in memory_graph.edges)
    assert any(edge.edge_type == "records_feedback" for edge in memory_graph.edges)
    assert memory_graph.summary["activation_candidate_count"] == len(result.activation_candidates)
    assert memory_graph.summary["activation_count"] == len(result.activations)
    assert memory_graph.summary["feedback_count"] == len(result.feedback)

    checkpoint_lineage = service.export_checkpoint_lineage(result.run.run_id)
    assert checkpoint_lineage is not None
    assert checkpoint_lineage.projection_kind == "checkpoint_lineage_graph"
    assert checkpoint_lineage.summary["checkpoint_count"] == len(result.checkpoints)
    assert any(node.entity_type == "checkpoint" for node in checkpoint_lineage.nodes)
    assert any(edge.edge_type == "creates_checkpoint" for edge in checkpoint_lineage.edges)

    training_dataset = service.export_activation_training_dataset(result.run.run_id)
    assert training_dataset is not None
    assert training_dataset.dataset_kind == "activation_training_dataset"
    assert training_dataset.summary["sample_count"] == len(result.steps)
    assert len(training_dataset.samples) == len(result.steps)
    assert training_dataset.samples[0].node_id == "planner"
    assert training_dataset.samples[0].candidate_count == 2
    assert training_dataset.samples[0].candidates[0]["scoring_breakdown"]
    assert training_dataset.samples[0].feedback_ids


def test_manual_activation_can_suppress_node_execution_until_overridden():
    workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Leave reviewer inactive"},
                metadata={"task_title": "Manual Gate"},
            )
        )
    )

    assert [step.node_id for step in result.steps] == ["planner", "reviewer"]
    assert result.steps[1].status == "skipped"
    assert result.steps[1].output["skipped"] is True
    assert result.steps[1].output["activation_decision"] == "suppressed"
    assert result.activations[1].decision == "suppressed"
    reviewer_node_candidate = next(
        candidate
        for candidate in result.activation_candidates
        if candidate.step_id == result.steps[1].step_id and candidate.candidate_type == "node"
    )
    assert reviewer_node_candidate.selected is False
    assert reviewer_node_candidate.suppressed_reason == "manual_activation_required"
    assert result.feedback[1].status == "suppressed"
    assert result.feedback[1].signals["activation_decision"] == "suppressed"
    assert len(result.handoffs) == 1


def test_activation_policy_applies_threshold_top_k_and_budget_to_candidates():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "activation-policy",
            "version": "0.1",
            "name": "Activation Policy",
            "entrypoint": "planner",
            "metadata": {
                "template_activation": {
                    "selection_threshold": 0.75,
                    "top_k": 2,
                    "budget": 1,
                    "candidate_type_weights": {"delegate_target": 0.5, "subgoal": 2.0},
                    "signal_weights": {"goal": 0.05, "context": 0.0},
                }
            },
            "nodes": [
                {
                    "id": "planner",
                    "kind": "agent",
                    "type": "agent.execute",
                    "config": {
                        "role": "planner",
                        "message_template": "[planner] applied candidate policy.",
                        "local_activation": {
                            "mode": "local",
                            "delegate_candidates": ["delegate-a", "delegate-b", "delegate-c"],
                            "subgoal_triggers": ["subgoal-a"],
                        },
                    },
                }
            ],
            "edges": [{"from": "planner", "to": "END", "type": "final"}],
        }
    )
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Apply candidate policy"},
            )
        )
    )

    planner_candidates = [candidate for candidate in result.activation_candidates if candidate.node_id == "planner"]
    selected_non_node = [
        candidate for candidate in planner_candidates if candidate.candidate_type != "node" and candidate.selected
    ]
    assert len(planner_candidates) == 5
    assert len(selected_non_node) == 1
    assert selected_non_node[0].candidate_ref == "subgoal-a"
    assert any(
        candidate.candidate_ref == "delegate-a" and candidate.suppressed_reason == "below_selection_threshold"
        for candidate in planner_candidates
    )
    assert any(
        candidate.candidate_ref == "delegate-b" and candidate.suppressed_reason == "below_selection_threshold"
        for candidate in planner_candidates
    )
    assert any(
        candidate.candidate_ref == "delegate-c" and candidate.suppressed_reason == "below_selection_threshold"
        for candidate in planner_candidates
    )
    assert result.feedback[0].signals["candidate_count"] == 5
    assert result.feedback[0].signals["selected_candidate_count"] == 2
    assert result.feedback[0].signals["selected_non_node_candidate_count"] == 1
    subgoal_candidate = next(candidate for candidate in planner_candidates if candidate.candidate_ref == "subgoal-a")
    assert subgoal_candidate.metadata["scoring_breakdown"]["type_weight"] == 2.0
    assert subgoal_candidate.metadata["scoring_breakdown"]["weighted_signals"]["goal"] == 0.05


def test_checkpoint_lineage_marks_resumed_runs_with_intervention_edges():
    workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)
    service = RuntimeService()

    first = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Create resumable lineage"},
                metadata={"task_title": "Resume Source"},
            )
        )
    )
    resumed = asyncio.run(
        service.resume(
            first.run.run_id,
            ResumeRequest(checkpoint_id=first.checkpoints[0].checkpoint_id),
        )
    )

    checkpoint_lineage = service.export_checkpoint_lineage(resumed.run.run_id)
    assert checkpoint_lineage is not None
    resume_edges = [edge for edge in checkpoint_lineage.edges if edge.edge_type == "resume_from"]
    derived_edges = [edge for edge in checkpoint_lineage.edges if edge.edge_type == "derived_from_checkpoint"]

    assert len(resume_edges) == 1
    assert resume_edges[0].intervention is True
    assert resume_edges[0].to_id == first.checkpoints[0].checkpoint_id
    assert len(derived_edges) == 1
    assert derived_edges[0].from_id == first.checkpoints[0].checkpoint_id
    assert derived_edges[0].to_id == resumed.run.run_id


def test_spawn_child_run_records_lineage_and_inherits_context():
    parent_workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)
    child_workflow = WorkflowDefinition.model_validate(CHILD_WORKFLOW_PAYLOAD)
    service = RuntimeService()

    parent = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=parent_workflow,
                input={"goal": "Parent run"},
                context={"repo": "Xueban", "owner": "core"},
                metadata={"task_title": "Parent Task"},
            )
        )
    )

    child = asyncio.run(
        service.spawn_child_run(
            parent.run.run_id,
            ChildRunRequest(
                workflow=child_workflow,
                input={"goal": "Child run"},
                context={"owner": "delegated"},
                parent_step_id=parent.steps[0].step_id,
                task_title="Child Task",
                handoff_goal="Deliver delegated findings back to the parent.",
            ),
        )
    )

    assert child.run.parent_run_id == parent.run.run_id
    assert child.run.root_run_id == parent.run.run_id
    assert child.tasks[0].parent_task_id == parent.tasks[0].task_id
    assert child.tasks[0].root_task_id == parent.tasks[0].root_task_id
    assert child.run.metadata["delegated_run"] is True
    assert child.run.metadata["parent_step_id"] == parent.steps[0].step_id
    assert child.run.metadata["delegated_from_node_id"] == parent.steps[0].node_id
    assert child.tasks[0].metadata["handoff_goal"] == "Deliver delegated findings back to the parent."
    assert child.steps[0].output["context"]["repo"] == "Xueban"
    assert child.steps[0].output["context"]["owner"] == "delegated"
    assert child.activations[0].mode == "always"
    assert child.feedback[-1].source_type == "run"

    task_tree = service.export_task_tree(parent.run.run_id)
    assert task_tree is not None
    assert task_tree.summary["run_count"] == 2
    assert any(node.id == child.run.run_id and node.entity_type == "run" for node in task_tree.nodes)
    assert any(node.id == child.tasks[0].task_id and node.entity_type == "task" for node in task_tree.nodes)
    assert any(
        edge.edge_type == "delegation" and edge.from_id == parent.run.run_id and edge.to_id == child.run.run_id
        for edge in task_tree.edges
    )
    assert any(
        edge.edge_type == "delegation" and edge.from_id == parent.tasks[0].task_id and edge.to_id == child.tasks[0].task_id
        for edge in task_tree.edges
    )


def test_delegated_node_automatically_spawns_child_run_and_updates_task_tree():
    workflow = WorkflowDefinition.model_validate(DELEGATED_NODE_WORKFLOW_PAYLOAD)
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "Run delegated node"},
                context={"repo": "Xueban", "owner": "core"},
                metadata={"task_title": "Delegated Parent Task"},
            )
        )
    )

    assert [step.node_id for step in result.steps] == ["delegator", "reviewer"]
    delegated_step = result.steps[0]
    assert delegated_step.output["delegated_run"] is True
    child_run_id = delegated_step.output["child_run_id"]
    child_result = service.get_run(child_run_id)
    assert child_result is not None
    assert child_result.run.parent_run_id == result.run.run_id
    assert child_result.tasks[0].parent_task_id == result.tasks[0].task_id
    assert child_result.steps[0].output["context"]["repo"] == "Xueban"
    assert child_result.steps[0].output["context"]["owner"] == "delegated-node"

    task_tree = service.export_task_tree(result.run.run_id)
    assert task_tree is not None
    assert task_tree.summary["run_count"] == 2
    assert any(node.id == child_run_id and node.entity_type == "run" for node in task_tree.nodes)
    assert any(
        edge.edge_type == "delegation" and edge.from_id == result.run.run_id and edge.to_id == child_run_id
        for edge in task_tree.edges
    )


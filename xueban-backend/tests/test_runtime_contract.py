import asyncio
from copy import deepcopy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from xueban.runtime import (
    ChatMessageRequest,
    ChatSessionCreateRequest,
    ResumeRequest,
    RuntimeRequest,
    RuntimeService,
    WorkflowDefinition,
    get_official_example,
    load_official_workflow,
)
from xueban.server import app

ROOT = Path(__file__).resolve().parents[1]


def make_local_test_dir(prefix: str) -> Path:
    path = Path.home() / ".codex" / "memories" / "xueban-test-output" / f"{prefix}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


WORKFLOW_PAYLOAD = {
    "workflow_id": "docs-gap-review",
    "version": "0.1",
    "name": "Docs Gap Review",
    "entrypoint": "planner",
    "defaults": {
        "writeback": {
            "artifact": {"target": "docs", "mode": "inline"},
            "checkpoint": {"target": "memory", "mode": "reference"},
        }
    },
    "nodes": [
        {
            "id": "planner",
            "kind": "agent",
            "type": "planning.analyze",
            "config": {
                "role": "planner",
                "message_template": "[planner] reviewed docs gap",
                "emit": {"gap_count": 2},
                "set_state": {"gap_count": 2},
            },
        },
        {
            "id": "reviewer",
            "kind": "agent",
            "type": "review.summarize",
            "config": {
                "role": "reviewer",
                "message_template": "[reviewer] created notes",
                "copy_input": ["goal"],
                "artifact": {
                    "kind": "report",
                    "name": "review-notes.md",
                    "content": "# notes",
                },
            },
        },
    ],
    "edges": [
        {"from": "planner", "to": "reviewer", "type": "conditional", "condition": "result.gap_count > 0"},
        {"from": "reviewer", "to": "END", "type": "final"},
    ],
}

PARALLEL_WORKFLOW_PAYLOAD = {
    "workflow_id": "parallel-synthesis",
    "version": "0.1",
    "name": "Parallel Synthesis",
    "entrypoint": "fanout",
    "defaults": {
        "writeback": {
            "artifact": {"target": "docs", "mode": "reference"},
            "checkpoint": {"target": "graph", "mode": "reference"},
        }
    },
    "nodes": [
        {
            "id": "fanout",
            "kind": "node",
            "type": "control.parallel",
            "config": {
                "role": "dispatcher",
                "message_template": "[dispatcher] scheduled research branches.",
                "branches": ["research_a", "research_b"],
                "barrier": "merge",
            },
        },
        {
            "id": "research_a",
            "kind": "agent",
            "type": "research.collect",
            "config": {
                "role": "researcher-a",
                "message_template": "[researcher-a] collected product signals.",
                "emit": {"topic": "product", "confidence": 7},
            },
        },
        {
            "id": "research_b",
            "kind": "agent",
            "type": "research.collect",
            "config": {
                "role": "researcher-b",
                "message_template": "[researcher-b] collected engineering signals.",
                "emit": {"topic": "engineering", "confidence": 8},
            },
        },
        {
            "id": "merge",
            "kind": "node",
            "type": "control.barrier",
            "config": {
                "role": "synthesizer",
                "source_parallel": "fanout",
                "message_template": "[synthesizer] joined branch outputs and prepared synthesis context.",
            },
        },
        {
            "id": "writer",
            "kind": "agent",
            "type": "review.summarize",
            "config": {
                "role": "writer",
                "message_template": "[writer] produced the synthesis summary.",
                "artifact": {
                    "kind": "report",
                    "name": "parallel-synthesis-summary.md",
                    "content": "# summary",
                },
            },
        },
    ],
    "edges": [
        {"from": "merge", "to": "writer", "type": "conditional", "condition": "result.branch_count >= 2"},
        {"from": "writer", "to": "END", "type": "final"},
    ],
}

NODE_OVERRIDE_WORKFLOW_PAYLOAD = {
    "workflow_id": "node-override-check",
    "version": "0.1",
    "name": "Node Override Check",
    "entrypoint": "draft",
    "defaults": {
        "writeback": {
            "artifact": {"target": "memory", "mode": "reference"},
            "checkpoint": {"target": "memory", "mode": "reference"},
        }
    },
    "nodes": [
        {
            "id": "draft",
            "kind": "agent",
            "type": "content.write",
            "config": {
                "role": "writer",
                "message_template": "[writer] drafted the content.",
                "artifact": {
                    "kind": "document",
                    "name": "node-override.md",
                    "content": "# node override",
                    "writeback": {"target": "docs", "mode": "inline"},
                },
            },
        }
    ],
    "edges": [{"from": "draft", "to": "END", "type": "final"}],
}


def test_runtime_service_returns_contract_shape():
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD),
        input={"goal": "Analyze docs gap and produce notes"},
        metadata={"source_system": "pytest"},
    )

    result = asyncio.run(service.run(request))

    assert result.run.status == "succeeded"
    assert result.run.workflow_id == "docs-gap-review"
    assert len(result.steps) == 2
    assert result.steps[0].output["gap_count"] == 2
    assert result.final_output["message"] == "[reviewer] created notes"
    assert result.final_output["copied_input"]["goal"] == "Analyze docs gap and produce notes"
    assert result.artifacts[0].name == "review-notes.md"
    assert result.artifacts[0].writeback.channel == "artifact"
    assert result.artifacts[0].writeback.host_action == "persist_artifact_ref"
    assert result.artifacts[0].writeback.target == "docs"
    assert result.artifacts[0].writeback.mode == "inline"
    assert result.artifacts[0].writeback.content_field == "metadata.content"
    assert result.checkpoints[0].writeback.channel == "checkpoint"
    assert result.checkpoints[0].writeback.host_action == "persist_checkpoint_ref"
    assert result.checkpoints[0].writeback.target == "memory"
    assert result.checkpoints[0].writeback.mode == "reference"
    assert len(result.checkpoints) == 2


def test_runtime_service_validates_entrypoint():
    invalid_payload = {
        **WORKFLOW_PAYLOAD,
        "entrypoint": "missing-node",
    }

    with pytest.raises(Exception, match="entrypoint must reference an existing node"):
        WorkflowDefinition.model_validate(invalid_payload)


def test_workflow_validation_rejects_invalid_writeback_defaults():
    invalid_checkpoint_mode = deepcopy(WORKFLOW_PAYLOAD)
    invalid_checkpoint_mode["defaults"]["writeback"]["checkpoint"]["mode"] = "inline"

    with pytest.raises(Exception, match="workflow defaults writeback.checkpoint.mode"):
        WorkflowDefinition.model_validate(invalid_checkpoint_mode)

    invalid_artifact_target = deepcopy(WORKFLOW_PAYLOAD)
    invalid_artifact_target["defaults"]["writeback"]["artifact"]["target"] = "invalid"

    with pytest.raises(Exception, match="workflow defaults writeback.artifact.target"):
        WorkflowDefinition.model_validate(invalid_artifact_target)


def test_runtime_request_validation_rejects_invalid_writeback_metadata():
    with pytest.raises(Exception, match="runtime request metadata writeback.checkpoint.mode"):
        RuntimeRequest.model_validate(
            {
                "workflow": WORKFLOW_PAYLOAD,
                "input": {"goal": "bad request"},
                "metadata": {"writeback": {"checkpoint": {"mode": "inline"}}},
            }
        )

    with pytest.raises(Exception, match="runtime request metadata writeback.artifact.target"):
        RuntimeRequest.model_validate(
            {
                "workflow": WORKFLOW_PAYLOAD,
                "input": {"goal": "bad request"},
                "metadata": {"writeback": {"artifact": {"target": "invalid"}}},
            }
        )


def test_runtime_request_writeback_override_takes_precedence():
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD),
        input={"goal": "Override writeback targets"},
        metadata={
            "source_system": "pytest",
            "writeback": {
                "artifact": {"target": "graph", "mode": "reference"},
                "checkpoint": {"target": "host", "mode": "reference"},
            },
        },
    )

    result = asyncio.run(service.run(request))

    assert result.artifacts[0].writeback.target == "graph"
    assert result.artifacts[0].writeback.mode == "reference"
    assert result.artifacts[0].writeback.content_field is None
    assert result.checkpoints[0].writeback.target == "host"
    assert result.checkpoints[0].writeback.mode == "reference"


def test_node_level_artifact_writeback_override_takes_highest_precedence():
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=WorkflowDefinition.model_validate(NODE_OVERRIDE_WORKFLOW_PAYLOAD),
        input={"goal": "Check node override"},
        metadata={
            "source_system": "pytest",
            "writeback": {
                "artifact": {"target": "graph", "mode": "reference"},
                "checkpoint": {"target": "host", "mode": "reference"},
            },
        },
    )

    result = asyncio.run(service.run(request))

    assert result.artifacts[0].writeback.target == "docs"
    assert result.artifacts[0].writeback.mode == "inline"
    assert result.artifacts[0].writeback.content_field == "metadata.content"
    assert result.checkpoints[0].writeback.target == "host"


def test_inline_artifact_without_content_fails_runtime():
    service = RuntimeService()
    invalid_payload = deepcopy(WORKFLOW_PAYLOAD)
    invalid_payload["nodes"][1]["config"]["artifact"].pop("content")

    with pytest.raises(ValueError, match="requires content when mode=inline"):
        asyncio.run(
            service.run(
                RuntimeRequest(
                    workflow=WorkflowDefinition.model_validate(invalid_payload),
                    input={"goal": "Analyze docs gap and produce notes"},
                    metadata={"source_system": "pytest"},
                )
            )
        )


def test_fastapi_contract_endpoints():
    client = TestClient(app)

    validate_response = client.post("/workflow/validate", json=WORKFLOW_PAYLOAD)
    assert validate_response.status_code == 200
    assert validate_response.json()["valid"] is True

    run_response = client.post(
        "/workflow/run",
        json={
            "workflow": WORKFLOW_PAYLOAD,
            "input": {"goal": "Analyze docs gap and produce notes"},
            "context": {"source_system": "api-test"},
            "metadata": {"source_system": "api"},
        },
    )
    assert run_response.status_code == 200
    run_result = run_response.json()
    assert run_result["run"]["status"] == "succeeded"
    assert len(run_result["steps"]) == 2
    assert run_result["artifacts"][0]["writeback"]["target"] == "docs"
    assert run_result["artifacts"][0]["writeback"]["mode"] == "inline"
    assert run_result["checkpoints"][0]["writeback"]["target"] == "memory"
    assert run_result["checkpoints"][0]["writeback"]["mode"] == "reference"

    run_id = run_result["run"]["run_id"]
    get_response = client.get(f"/runs/{run_id}")
    assert get_response.status_code == 200
    assert get_response.json()["run"]["run_id"] == run_id

    checkpoint_id = run_result["checkpoints"][0]["checkpoint_id"]
    checkpoint_response = client.get(f"/checkpoints/{checkpoint_id}")
    assert checkpoint_response.status_code == 200
    assert checkpoint_response.json()["checkpoint_id"] == checkpoint_id
    assert checkpoint_response.json()["writeback"]["channel"] == "checkpoint"
    assert checkpoint_response.json()["writeback"]["host_action"] == "persist_checkpoint_ref"
    assert checkpoint_response.json()["writeback"]["target"] == "memory"


def test_fastapi_validation_rejects_invalid_writeback_configs():
    client = TestClient(app)

    invalid_workflow = deepcopy(WORKFLOW_PAYLOAD)
    invalid_workflow["defaults"]["writeback"]["checkpoint"]["mode"] = "inline"
    validate_response = client.post("/workflow/validate", json=invalid_workflow)
    assert validate_response.status_code == 422

    run_response = client.post(
        "/workflow/run",
        json={
            "workflow": WORKFLOW_PAYLOAD,
            "input": {"goal": "bad request"},
            "metadata": {"writeback": {"checkpoint": {"mode": "inline"}}},
        },
    )
    assert run_response.status_code == 422


def test_fastapi_run_returns_400_for_inline_artifact_without_content():
    client = TestClient(app)
    invalid_workflow = deepcopy(WORKFLOW_PAYLOAD)
    invalid_workflow["nodes"][1]["config"]["artifact"].pop("content")

    run_response = client.post(
        "/workflow/run",
        json={
            "workflow": invalid_workflow,
            "input": {"goal": "Analyze docs gap and produce notes"},
            "metadata": {"source_system": "api"},
        },
    )

    assert run_response.status_code == 400
    detail = run_response.json()["detail"]
    # Server now returns structured {"error": {"code": ..., "message": ...}} envelope
    error_msg = detail if isinstance(detail, str) else detail.get("error", {}).get("message", "")
    assert "requires content when mode=inline" in error_msg


def test_runtime_service_can_resume_from_checkpoint():
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD),
        input={"goal": "Analyze docs gap and produce notes"},
        metadata={"source_system": "pytest"},
    )

    result = asyncio.run(service.run(request))
    checkpoint_id = result.checkpoints[0].checkpoint_id

    resumed = asyncio.run(
        service.resume(
            result.run.run_id,
            ResumeRequest(checkpoint_id=checkpoint_id, metadata={"source_system": "resume-test"}),
        )
    )

    assert resumed.run.status == "succeeded"
    assert resumed.run.metadata["resumed_from_checkpoint_id"] == checkpoint_id
    assert resumed.steps[0].node_id == "reviewer"
    assert resumed.final_output["message"] == "[reviewer] created notes"
    assert resumed.artifacts[0].writeback.target == "docs"
    assert resumed.artifacts[0].writeback.mode == "inline"


def test_runtime_service_supports_parallel_barrier():
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=WorkflowDefinition.model_validate(PARALLEL_WORKFLOW_PAYLOAD),
        input={"goal": "Synthesize two research branches"},
        metadata={"source_system": "pytest"},
    )

    result = asyncio.run(service.run(request))

    assert result.run.status == "succeeded"
    assert [step.node_id for step in result.steps] == ["fanout", "research_a", "research_b", "merge", "writer"]
    assert result.steps[3].output["branch_count"] == 2
    assert sorted(result.steps[3].output["branch_outputs"].keys()) == ["research_a", "research_b"]
    assert result.final_output["message"] == "[writer] produced the synthesis summary."
    assert result.checkpoints[0].writeback.target == "graph"


def test_parallel_workflow_can_resume_after_first_branch():
    service = RuntimeService()
    request = RuntimeRequest(
        workflow=WorkflowDefinition.model_validate(PARALLEL_WORKFLOW_PAYLOAD),
        input={"goal": "Resume parallel synthesis"},
        metadata={"source_system": "pytest"},
    )

    result = asyncio.run(service.run(request))
    branch_checkpoint = next(checkpoint for checkpoint in result.checkpoints if checkpoint.state.current_node_id == "research_a")

    resumed = asyncio.run(
        service.resume(
            result.run.run_id,
            ResumeRequest(checkpoint_id=branch_checkpoint.checkpoint_id, metadata={"source_system": "resume-test"}),
        )
    )

    assert resumed.run.status == "succeeded"
    assert resumed.run.metadata["resumed_from_checkpoint_id"] == branch_checkpoint.checkpoint_id
    assert [step.node_id for step in resumed.steps] == ["research_b", "merge", "writer"]
    assert resumed.steps[1].output["branch_count"] == 2
    assert resumed.checkpoints[0].writeback.target == "graph"


def test_fastapi_adapter_boundary_supports_run_checkpoint_and_resume():
    client = TestClient(app)
    example = get_official_example("research-review-loop")
    workflow = load_official_workflow(example)

    run_response = client.post(
        "/workflow/run",
        json={
            "workflow": workflow.model_dump(mode="json"),
            "input": example.input,
            "metadata": {"source_system": "adapter-boundary-test", **example.metadata},
        },
    )

    assert run_response.status_code == 200
    payload = run_response.json()
    assert sorted(payload.keys()) == [
        "activation_candidates",
        "activations",
        "artifacts",
        "checkpoints",
        "errors",
        "feedback",
        "final_output",
        "handoffs",
        "memory_events",
        "run",
        "steps",
        "tasks",
        "trace",
    ]
    assert payload["run"]["workflow_id"] == example.id
    assert payload["run"]["status"] == "succeeded"
    assert isinstance(payload["steps"], list) and payload["steps"]
    assert isinstance(payload["tasks"], list) and payload["tasks"]
    assert isinstance(payload["artifacts"], list)
    assert isinstance(payload["checkpoints"], list)
    assert isinstance(payload["handoffs"], list)
    assert isinstance(payload["activations"], list)
    assert isinstance(payload["activation_candidates"], list)
    assert isinstance(payload["feedback"], list)
    assert isinstance(payload["memory_events"], list)
    assert isinstance(payload["trace"], list)
    assert isinstance(payload["final_output"], dict)
    assert payload["checkpoints"][0]["writeback"]["target"] == "memory"
    assert payload["checkpoints"][0]["writeback"]["mode"] == "reference"

    run_id = payload["run"]["run_id"]
    get_run_response = client.get(f"/runs/{run_id}")
    assert get_run_response.status_code == 200
    assert get_run_response.json()["run"]["run_id"] == run_id

    checkpoint = next(
        item for item in payload["checkpoints"] if item["state"]["current_node_id"] == example.resume_from_checkpoint_node_id
    )
    checkpoint_response = client.get(f"/checkpoints/{checkpoint['checkpoint_id']}")
    assert checkpoint_response.status_code == 200
    checkpoint_payload = checkpoint_response.json()
    assert checkpoint_payload["run_id"] == run_id
    assert checkpoint_payload["state"]["current_node_id"] == example.resume_from_checkpoint_node_id
    assert checkpoint_payload["state"]["next_node_id"] == example.expected_resumed_nodes[0]
    assert checkpoint_payload["writeback"]["target"] == "memory"

    resume_response = client.post(
        f"/runs/{run_id}/resume",
        json={
            "checkpoint_id": checkpoint["checkpoint_id"],
            "metadata": {"source_system": "adapter-boundary-test", "resume_case": example.id},
        },
    )
    assert resume_response.status_code == 200
    resumed_payload = resume_response.json()
    assert resumed_payload["run"]["metadata"]["resumed_from_checkpoint_id"] == checkpoint["checkpoint_id"]
    assert resumed_payload["run"]["metadata"]["resumed_from_run_id"] == run_id
    assert [step["node_id"] for step in resumed_payload["steps"]] == example.expected_resumed_nodes
    assert resumed_payload["final_output"]["node_id"] == example.expected_resumed_terminal_node
    assert checkpoint_payload["writeback"]["resume_supported"] is True
    assert checkpoint_payload["writeback"]["next_node_id"] == example.expected_resumed_nodes[0]
    assert resumed_payload["artifacts"][0]["writeback"]["channel"] == "artifact"
    assert resumed_payload["artifacts"][0]["writeback"]["host_action"] == "persist_artifact_ref"
    assert resumed_payload["artifacts"][0]["writeback"]["target"] == "docs"
    assert resumed_payload["artifacts"][0]["writeback"]["mode"] == "reference"


def test_parallel_example_exposes_adapter_boundary_branch_outputs():
    example = get_official_example("parallel-synthesis")
    workflow = load_official_workflow(example)
    service = RuntimeService()

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=workflow,
                input=example.input,
                metadata={"source_system": "adapter-boundary-test", **example.metadata},
            )
        )
    )

    barrier_step = next(step for step in result.steps if step.node_id == "merge")
    assert barrier_step.output["parallel_group"] == "fanout"
    assert barrier_step.output["branch_count"] == example.expected_parallel_branch_count
    assert sorted(barrier_step.output["branch_outputs"].keys()) == ["research_a", "research_b"]
    assert barrier_step.output["branches_completed"] == ["research_a", "research_b"]
    assert result.checkpoints[0].writeback.channel == "checkpoint"
    assert result.checkpoints[0].writeback.target == "graph"
    assert result.artifacts[0].writeback.target == "docs"
    assert result.artifacts[0].writeback.mode == "reference"


def test_runtime_service_exports_workflow_graph_and_chat_session():
    service = RuntimeService()
    workflow = WorkflowDefinition.model_validate(WORKFLOW_PAYLOAD)

    graph = service.export_workflow_graph(workflow)
    assert graph.workflow_id == workflow.workflow_id
    assert graph.entrypoint == workflow.entrypoint
    assert len(graph.nodes) == len(workflow.nodes)
    assert graph.nodes[0].entrypoint is True

    session = service.create_chat_session(
        ChatSessionCreateRequest.model_validate(
            {
            "title": "pytest chat",
            "executor": {
                "kind": "cli",
                "provider": "generic",
                "command": sys.executable,
                "args": [
                    "-c",
                    (
                        "import json,sys;"
                        "payload=json.load(sys.stdin);"
                        "print(json.dumps({'message':'echo:' + payload['step_input']['message']}))"
                    ),
                ],
                "stdin": "json",
                "parse": "json",
            },
            "system_prompt": "你是一个简洁助手。",
            "metadata": {"source_system": "pytest"},
            }
        )
    )
    assert session.session.title == "pytest chat"
    assert session.messages[0].role == "system"

    turn = asyncio.run(
        service.send_chat_message(
            session.session.session_id,
            ChatMessageRequest.model_validate({"content": "hello"}),
        )
    )
    assert turn.response_text == "echo:hello"
    reloaded_session = service.get_chat_session(session.session.session_id)
    assert reloaded_session is not None
    assert [item.role for item in reloaded_session.messages] == ["system", "user", "assistant"]


def test_fastapi_exposes_runs_graph_and_chat_endpoints():
    client = TestClient(app)
    child_workflow_payload = {
        "workflow_id": "api-child-workflow",
        "version": "0.1",
        "name": "API Child Workflow",
        "entrypoint": "child",
        "nodes": [
            {
                "id": "child",
                "kind": "agent",
                "type": "agent.execute",
                "config": {
                    "role": "child",
                    "message_template": "[child] delegated execution complete",
                    "context_echo": ["repo"],
                },
            }
        ],
        "edges": [{"from": "child", "to": "END", "type": "final"}],
    }

    graph_response = client.post("/workflow/graph", json=WORKFLOW_PAYLOAD)
    assert graph_response.status_code == 200
    graph_payload = graph_response.json()
    assert graph_payload["workflow_id"] == WORKFLOW_PAYLOAD["workflow_id"]
    assert graph_payload["entrypoint"] == WORKFLOW_PAYLOAD["entrypoint"]

    run_response = client.post(
        "/workflow/run",
        json={
            "workflow": WORKFLOW_PAYLOAD,
            "input": {"goal": "Expose run list and graph"},
            "metadata": {"source_system": "api-panel-test"},
        },
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    run_id = run_payload["run"]["run_id"]

    runs_response = client.get("/runs")
    assert runs_response.status_code == 200
    assert any(item["run_id"] == run_id for item in runs_response.json())

    run_graph_response = client.get(f"/runs/{run_id}/graph")
    assert run_graph_response.status_code == 200
    run_graph_payload = run_graph_response.json()
    assert run_graph_payload["run_id"] == run_id
    assert any(node["id"] == "planner" and node["status"] == "succeeded" for node in run_graph_payload["nodes"])
    assert run_graph_payload["projection_kind"] == "run_graph"
    assert "activation_candidate_count" in run_graph_payload["summary"]
    assert "activation_count" in run_graph_payload["summary"]
    assert "feedback_count" in run_graph_payload["summary"]

    child_run_response = client.post(
        f"/runs/{run_id}/children",
        json={
            "workflow": child_workflow_payload,
            "input": {"goal": "Spawn child from API"},
            "context": {"repo": "Xueban"},
            "parent_step_id": run_payload["steps"][0]["step_id"],
            "task_title": "API Child Task",
            "handoff_goal": "Return delegated API findings.",
        },
    )
    assert child_run_response.status_code == 200
    child_run_payload = child_run_response.json()
    assert child_run_payload["run"]["parent_run_id"] == run_id
    assert child_run_payload["tasks"][0]["parent_task_id"] == run_payload["tasks"][0]["task_id"]

    task_tree_response = client.get(f"/runs/{run_id}/task-tree")
    assert task_tree_response.status_code == 200
    task_tree_payload = task_tree_response.json()
    assert task_tree_payload["projection_kind"] == "task_tree"
    assert any(node["entity_type"] == "task" for node in task_tree_payload["nodes"])
    assert any(edge["edge_type"] == "belongs_to_task" for edge in task_tree_payload["edges"])
    assert task_tree_payload["summary"]["run_count"] == 2
    assert "activation_candidate_count" in task_tree_payload["summary"]
    assert "activation_count" in task_tree_payload["summary"]
    assert "feedback_count" in task_tree_payload["summary"]
    assert any(
        edge["edge_type"] == "delegation"
        and edge["from_id"] == run_id
        and edge["to_id"] == child_run_payload["run"]["run_id"]
        for edge in task_tree_payload["edges"]
    )
    assert "graph projection" in task_tree_payload["metadata"]["projection_note"]

    artifact_lineage_response = client.get(f"/runs/{run_id}/artifact-lineage")
    assert artifact_lineage_response.status_code == 200
    artifact_lineage_payload = artifact_lineage_response.json()
    assert artifact_lineage_payload["projection_kind"] == "artifact_lineage_graph"
    assert any(node["entity_type"] == "artifact" for node in artifact_lineage_payload["nodes"])

    memory_graph_response = client.get(f"/runs/{run_id}/memory-graph")
    assert memory_graph_response.status_code == 200
    memory_graph_payload = memory_graph_response.json()
    assert memory_graph_payload["projection_kind"] == "memory_relation_graph"
    assert any(node["entity_type"] == "memory_event" for node in memory_graph_payload["nodes"])
    assert any(node["entity_type"] == "activation_candidate" for node in memory_graph_payload["nodes"])
    assert any(node["entity_type"] == "activation" for node in memory_graph_payload["nodes"])
    assert any(node["entity_type"] == "feedback_signal" for node in memory_graph_payload["nodes"])
    assert any(edge["edge_type"] == "candidate_for_activation" for edge in memory_graph_payload["edges"])
    assert any(edge["edge_type"] == "activates" for edge in memory_graph_payload["edges"])
    assert any(edge["edge_type"] == "records_feedback" for edge in memory_graph_payload["edges"])

    checkpoint_lineage_response = client.get(f"/runs/{run_id}/checkpoint-lineage")
    assert checkpoint_lineage_response.status_code == 200
    checkpoint_lineage_payload = checkpoint_lineage_response.json()
    assert checkpoint_lineage_payload["projection_kind"] == "checkpoint_lineage_graph"
    assert any(node["entity_type"] == "checkpoint" for node in checkpoint_lineage_payload["nodes"])

    training_dataset_response = client.get(f"/runs/{run_id}/training-dataset")
    assert training_dataset_response.status_code == 200
    training_dataset_payload = training_dataset_response.json()
    assert training_dataset_payload["dataset_kind"] == "activation_training_dataset"
    assert training_dataset_payload["summary"]["sample_count"] >= 1
    assert training_dataset_payload["samples"][0]["candidates"]
    assert "scoring_breakdown" in training_dataset_payload["samples"][0]["candidates"][0]

    create_session_response = client.post(
        "/chat/sessions",
        json={
            "title": "api chat",
            "executor": {
                "kind": "cli",
                "provider": "generic",
                "command": sys.executable,
                "args": [
                    "-c",
                    (
                        "import json,sys;"
                        "payload=json.load(sys.stdin);"
                        "print(json.dumps({'message':'chat:' + payload['step_input']['message']}))"
                    ),
                ],
                "stdin": "json",
                "parse": "json",
            },
            "system_prompt": "你是测试助手。",
        },
    )
    assert create_session_response.status_code == 200
    session_payload = create_session_response.json()
    session_id = session_payload["session"]["session_id"]

    send_message_response = client.post(
        f"/chat/sessions/{session_id}/messages",
        json={"content": "ping"},
    )
    assert send_message_response.status_code == 200
    turn_payload = send_message_response.json()
    assert turn_payload["response_text"] == "chat:ping"

    get_session_response = client.get(f"/chat/sessions/{session_id}")
    assert get_session_response.status_code == 200
    messages = get_session_response.json()["messages"]
    assert [item["role"] for item in messages] == ["system", "user", "assistant"]


def test_cli_graph_command_exports_workflow_graph():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "graph",
            "-w",
            "examples/runtime-contract/docs-gap-review.yaml",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["workflow_id"] == "docs-gap-review"
    assert payload["entrypoint"] == "planner"


def test_cli_chat_command_supports_single_turn_generic_executor():
    runtime_root = make_local_test_dir("chat-single-turn")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "chat",
            "--kind",
            "cli",
            "--provider",
            "generic",
            "--command",
            sys.executable,
            "--args-json",
            json.dumps(
                [
                    "-c",
                    (
                        "import json,sys;"
                        "payload=json.load(sys.stdin);"
                        "print(json.dumps({'message':'chat-test:' + payload['step_input']['message']}))"
                    ),
                ]
            ),
            "--stdin-mode",
            "json",
            "--parse",
            "json",
            "--message",
            "hello",
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["response_text"] == "chat-test:hello"
    assert payload["assistant_message"]["content"] == "chat-test:hello"
    shutil.rmtree(runtime_root, ignore_errors=True)


def test_cli_runs_checkpoints_and_resume_commands_support_persisted_runtime_root():
    runtime_root = make_local_test_dir("persisted-runtime")

    run_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "run",
            "-w",
            "examples/runtime-contract/research-review-loop.yaml",
            "-i",
            '{"goal":"cli persisted resume"}',
            "--writeback",
            "markdown",
            "--writeback-root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert run_completed.returncode == 0, run_completed.stderr
    run_payload = json.loads(run_completed.stdout)
    run_id = run_payload["run"]["run_id"]
    checkpoint_id = next(
        item["checkpoint_id"]
        for item in run_payload["checkpoints"]
        if item["state"]["current_node_id"] == "researcher"
    )

    runs_list_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "runs",
            "list",
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert runs_list_completed.returncode == 0, runs_list_completed.stderr
    runs_list_payload = json.loads(runs_list_completed.stdout)
    assert any(item["run_id"] == run_id for item in runs_list_payload)

    run_get_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "runs",
            "get",
            "--run-id",
            run_id,
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run_get_completed.returncode == 0, run_get_completed.stderr
    run_get_payload = json.loads(run_get_completed.stdout)
    assert run_get_payload["run"]["run_id"] == run_id

    run_graph_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "runs",
            "graph",
            "--run-id",
            run_id,
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run_graph_completed.returncode == 0, run_graph_completed.stderr
    run_graph_payload = json.loads(run_graph_completed.stdout)
    assert run_graph_payload["run_id"] == run_id
    assert any(node["id"] == "reviewer" for node in run_graph_payload["nodes"])

    checkpoint_get_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "checkpoints",
            "get",
            "--checkpoint-id",
            checkpoint_id,
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checkpoint_get_completed.returncode == 0, checkpoint_get_completed.stderr
    checkpoint_payload = json.loads(checkpoint_get_completed.stdout)
    assert checkpoint_payload["checkpoint_id"] == checkpoint_id

    resume_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "resume",
            "--run-id",
            run_id,
            "--checkpoint-id",
            checkpoint_id,
            "--root",
            str(runtime_root),
            "--metadata",
            '{"source_system":"pytest-cli-resume"}',
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert resume_completed.returncode == 0, resume_completed.stderr
    resume_payload = json.loads(resume_completed.stdout)
    assert resume_payload["run"]["metadata"]["resumed_from_run_id"] == run_id
    assert resume_payload["run"]["metadata"]["resumed_from_checkpoint_id"] == checkpoint_id
    assert resume_payload["steps"][0]["node_id"] == "reviewer"
    shutil.rmtree(runtime_root, ignore_errors=True)


def test_cli_sessions_commands_support_persisted_chat_sessions():
    runtime_root = make_local_test_dir("persisted-chat")
    chat_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "chat",
            "--kind",
            "cli",
            "--provider",
            "generic",
            "--command",
            sys.executable,
            "--args-json",
            json.dumps(
                [
                    "-c",
                    (
                        "import json,sys;"
                        "payload=json.load(sys.stdin);"
                        "print(json.dumps({'message':'chat-test:' + payload['step_input']['message']}))"
                    ),
                ]
            ),
            "--stdin-mode",
            "json",
            "--parse",
            "json",
            "--message",
            "hello persisted",
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert chat_completed.returncode == 0, chat_completed.stderr
    chat_payload = json.loads(chat_completed.stdout)
    session_id = chat_payload["session"]["session_id"]

    sessions_list_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "sessions",
            "list",
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sessions_list_completed.returncode == 0, sessions_list_completed.stderr
    sessions_list_payload = json.loads(sessions_list_completed.stdout)
    assert any(item["session_id"] == session_id for item in sessions_list_payload)

    session_get_completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "xueban.cli",
            "sessions",
            "get",
            "--session-id",
            session_id,
            "--root",
            str(runtime_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert session_get_completed.returncode == 0, session_get_completed.stderr
    session_payload = json.loads(session_get_completed.stdout)
    assert session_payload["session"]["session_id"] == session_id
    assert [item["role"] for item in session_payload["messages"]] == ["user", "assistant"]
    shutil.rmtree(runtime_root, ignore_errors=True)


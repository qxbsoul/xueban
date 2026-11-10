import asyncio
import shutil
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from xueban.runtime import (
    ChatSessionCreateRequest,
    ChatSession,
    FileChatSessionStore,
    FileCheckpointStore,
    FileRequestContextStore,
    FileRunStore,
    InMemoryCheckpointStore,
    ReferenceWritebackAdapter,
    ResumeRequest,
    RuntimeRequest,
    RuntimeService,
    RunResult,
    get_official_example,
    load_official_workflow,
)

def make_local_test_dir(prefix: str) -> Path:
    path = Path.home() / ".codex" / "memories" / "xueban-test-output" / f"{prefix}-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_service_with_reference_adapter():
    checkpoint_store = InMemoryCheckpointStore()
    adapter = ReferenceWritebackAdapter(checkpoint_store=checkpoint_store)
    service = RuntimeService(writeback_adapter=adapter, checkpoint_store=checkpoint_store)
    return service, adapter, checkpoint_store


def test_reference_writeback_adapter_persists_docs_memory_and_graph_targets():
    service, adapter, checkpoint_store = build_service_with_reference_adapter()

    docs_example = get_official_example("docs-gap-review")
    docs_result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=load_official_workflow(docs_example),
                input=docs_example.input,
                metadata={"source_system": "pytest-adapter", **docs_example.metadata},
            )
        )
    )

    parallel_example = get_official_example("parallel-synthesis")
    parallel_result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=load_official_workflow(parallel_example),
                input=parallel_example.input,
                metadata={"source_system": "pytest-adapter", **parallel_example.metadata},
            )
        )
    )

    snapshot = adapter.snapshot()
    assert docs_result.run.metadata["writeback_receipts"]
    assert parallel_result.run.metadata["writeback_receipts"]
    assert "docs-gap-review-notes.md" in {
        payload["name"] for payload in snapshot["artifacts"]["docs"].values()
    }
    docs_payload = next(
        payload
        for payload in snapshot["artifacts"]["docs"].values()
        if payload["name"] == "docs-gap-review-notes.md"
    )
    assert docs_payload["content"] == "# Docs Gap Review Notes"
    assert checkpoint_store.list_run(docs_result.run.run_id)
    assert checkpoint_store.list_run(parallel_result.run.run_id)
    assert snapshot["checkpoints"]["memory"]
    assert snapshot["checkpoints"]["graph"]


def test_reference_writeback_adapter_respects_node_override_and_reference_modes():
    service, adapter, _ = build_service_with_reference_adapter()
    example = get_official_example("content-creation-phase1")
    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=load_official_workflow(example),
                input=example.input,
                metadata={"source_system": "pytest-adapter", **example.metadata},
            )
        )
    )

    snapshot = adapter.snapshot()
    docs_artifacts = {payload["name"]: payload for payload in snapshot["artifacts"]["docs"].values()}
    memory_artifacts = {payload["name"]: payload for payload in snapshot["artifacts"]["memory"].values()}

    assert "content-creation-final.md" in docs_artifacts
    assert docs_artifacts["content-creation-final.md"]["mode"] == "inline"
    assert docs_artifacts["content-creation-final.md"]["content"] == "# Content Creation Final\n\nThe final version is ready."
    assert "content-creation-draft.md" in memory_artifacts
    assert memory_artifacts["content-creation-draft.md"]["mode"] == "reference"
    assert memory_artifacts["content-creation-draft.md"]["content"] is None
    assert result.run.metadata["writeback_receipts"]


def test_checkpoint_store_supports_resume_across_runtime_instances_with_registered_request_context():
    checkpoint_store = InMemoryCheckpointStore()
    service1 = RuntimeService(checkpoint_store=checkpoint_store)

    example = get_official_example("research-review-loop")
    workflow = load_official_workflow(example)
    request = RuntimeRequest(
        workflow=workflow,
        input=example.input,
        metadata={"source_system": "pytest-store", **example.metadata},
    )

    initial = asyncio.run(service1.run(request))
    checkpoint = next(
        item for item in initial.checkpoints if item.state.current_node_id == example.resume_from_checkpoint_node_id
    )
    assert checkpoint_store.get_record(checkpoint.checkpoint_id) is not None

    service2 = RuntimeService(checkpoint_store=checkpoint_store)
    service2.register_request_context(initial.run.run_id, request)

    stored_checkpoint = service2.get_checkpoint(checkpoint.checkpoint_id)
    assert stored_checkpoint is not None
    assert stored_checkpoint.checkpoint_id == checkpoint.checkpoint_id

    resumed = asyncio.run(
        service2.resume(
            initial.run.run_id,
            ResumeRequest(
                checkpoint_id=checkpoint.checkpoint_id,
                metadata={"source_system": "pytest-store", "resume_case": example.id},
            ),
        )
    )

    assert resumed.run.status == "succeeded"
    assert resumed.run.metadata["resumed_from_checkpoint_id"] == checkpoint.checkpoint_id
    assert [step.node_id for step in resumed.steps] == example.expected_resumed_nodes


def test_file_backed_runtime_stores_support_run_listing_and_resume_across_instances():
    root = make_local_test_dir("file-runtime-store")
    checkpoint_store = FileCheckpointStore(root / "checkpoint-store")
    run_store = FileRunStore(root / "runs")
    request_context_store = FileRequestContextStore(root / "requests")

    example = get_official_example("research-review-loop")
    workflow = load_official_workflow(example)
    request = RuntimeRequest(
        workflow=workflow,
        input=example.input,
        metadata={"source_system": "pytest-file-store", **example.metadata},
    )

    service1 = RuntimeService(
        checkpoint_store=checkpoint_store,
        run_store=run_store,
        request_context_store=request_context_store,
    )
    initial = asyncio.run(service1.run(request))
    checkpoint = next(
        item for item in initial.checkpoints if item.state.current_node_id == example.resume_from_checkpoint_node_id
    )

    service2 = RuntimeService(
        checkpoint_store=checkpoint_store,
        run_store=run_store,
        request_context_store=request_context_store,
    )
    runs = service2.list_runs()
    assert any(item.run_id == initial.run.run_id for item in runs)
    persisted = service2.get_run(initial.run.run_id)
    assert persisted is not None
    assert persisted.run.run_id == initial.run.run_id

    resumed = asyncio.run(
        service2.resume(
            initial.run.run_id,
            ResumeRequest(
                checkpoint_id=checkpoint.checkpoint_id,
                metadata={"source_system": "pytest-file-store-resume"},
            ),
        )
    )

    assert resumed.run.status == "succeeded"
    assert resumed.run.metadata["resumed_from_checkpoint_id"] == checkpoint.checkpoint_id
    assert [step.node_id for step in resumed.steps] == example.expected_resumed_nodes
    shutil.rmtree(root, ignore_errors=True)


def test_file_chat_session_store_supports_cross_instance_listing_and_readback():
    root = make_local_test_dir("file-chat-store")
    chat_session_store = FileChatSessionStore(root / "chat" / "sessions")

    service1 = RuntimeService(chat_session_store=chat_session_store)
    session = service1.create_chat_session(
        ChatSessionCreateRequest(
            title="Persisted Chat",
            executor={
                "kind": "cli",
                "provider": "generic",
                "command": "python",
                "args": ["-c", "print('unused')"],
                "stdin": "none",
                "parse": "text",
            },
            metadata={"source_system": "pytest-chat-store"},
        )
    )

    service2 = RuntimeService(chat_session_store=chat_session_store)
    sessions = service2.list_chat_sessions()
    assert any(item.session_id == session.session.session_id for item in sessions)
    restored = service2.get_chat_session(session.session.session_id)
    assert restored is not None
    assert restored.session.session_id == session.session.session_id
    shutil.rmtree(root, ignore_errors=True)


def test_file_run_store_list_prefers_summary_files_over_full_results():
    root = make_local_test_dir("file-run-summary-index")
    run_store = FileRunStore(root / "runs")
    example = get_official_example("docs-gap-review")
    service = RuntimeService(run_store=run_store)

    result = asyncio.run(
        service.run(
            RuntimeRequest(
                workflow=load_official_workflow(example),
                input=example.input,
                metadata={"source_system": "pytest-summary-index", **example.metadata},
            )
        )
    )

    with patch.object(RunResult, "model_validate_json", side_effect=AssertionError("full run payload should not be parsed")):
        runs = run_store.list_runs()

    assert any(item.run_id == result.run.run_id for item in runs)
    shutil.rmtree(root, ignore_errors=True)


def test_file_chat_session_store_list_prefers_summary_files_over_full_sessions():
    root = make_local_test_dir("file-session-summary-index")
    store = FileChatSessionStore(root / "chat" / "sessions")
    service = RuntimeService(chat_session_store=store)

    session = service.create_chat_session(
        ChatSessionCreateRequest(
            title="Indexed Chat",
            executor={
                "kind": "cli",
                "provider": "generic",
                "command": "python",
                "args": ["-c", "print('unused')"],
                "stdin": "none",
                "parse": "text",
            },
            metadata={"source_system": "pytest-summary-index"},
        )
    )

    with patch.object(ChatSession, "model_validate_json", side_effect=AssertionError("full session payload should not be parsed")):
        sessions = store.list_sessions()

    assert any(item.session.session_id == session.session.session_id for item in sessions)
    shutil.rmtree(root, ignore_errors=True)


from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from xueban.runtime.checkpoint_store import BaseCheckpointStore, StoredCheckpointRecord
from xueban.runtime.contracts import (
    ArtifactRef,
    ChatSession,
    ChatSessionRecord,
    CheckpointRef,
    RunResult,
    RunSummary,
    RuntimeRequest,
)
from xueban.runtime.host_adapter import BaseWritebackAdapter, WritebackReceipt


class FileCheckpointStore(BaseCheckpointStore):
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.checkpoints_dir = self.root_dir / "checkpoints"
        self.records_dir = self.root_dir / "records"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)

    def put(self, checkpoint: CheckpointRef) -> StoredCheckpointRecord:
        checkpoint_copy = checkpoint.model_copy(deep=True)
        checkpoint_path = self.checkpoints_dir / f"{checkpoint_copy.checkpoint_id}.json"
        record = StoredCheckpointRecord(
            checkpoint_id=checkpoint_copy.checkpoint_id,
            run_id=checkpoint_copy.run_id,
            step_id=checkpoint_copy.step_id,
            target=checkpoint_copy.writeback.target,
            location=str(checkpoint_path),
            state_ref=checkpoint_copy.state_ref,
            next_node_id=checkpoint_copy.writeback.next_node_id,
            resume_supported=bool(checkpoint_copy.writeback.resume_supported),
            metadata={
                **checkpoint_copy.metadata,
                "current_node_id": checkpoint_copy.state.current_node_id,
            },
        )
        checkpoint_path.write_text(
            checkpoint_copy.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (self.records_dir / f"{checkpoint_copy.checkpoint_id}.json").write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return record

    def get(self, checkpoint_id: str) -> Optional[CheckpointRef]:
        path = self.checkpoints_dir / f"{checkpoint_id}.json"
        if not path.exists():
            return None
        return CheckpointRef.model_validate_json(path.read_text(encoding="utf-8"))

    def get_record(self, checkpoint_id: str) -> Optional[StoredCheckpointRecord]:
        path = self.records_dir / f"{checkpoint_id}.json"
        if not path.exists():
            return None
        return StoredCheckpointRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_run(self, run_id: str) -> List[StoredCheckpointRecord]:
        records: List[StoredCheckpointRecord] = []
        for path in sorted(self.records_dir.glob("*.json")):
            record = StoredCheckpointRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.run_id == run_id:
                records.append(record)
        return records


class FileRunStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.summaries_dir = self.root_dir / "_summaries"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)

    def put(self, result: RunResult) -> Path:
        path = self.root_dir / f"{result.run.run_id}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        (self.summaries_dir / f"{result.run.run_id}.json").write_text(
            self._build_summary(result).model_dump_json(indent=2),
            encoding="utf-8",
        )
        return path

    def get(self, run_id: str) -> Optional[RunResult]:
        path = self.root_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return RunResult.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self) -> List[RunSummary]:
        runs: List[RunSummary] = []
        summary_paths = sorted(self.summaries_dir.glob("*.json"))
        if summary_paths:
            for path in summary_paths:
                runs.append(RunSummary.model_validate_json(path.read_text(encoding="utf-8")))
            return sorted(runs, key=lambda item: item.started_at, reverse=True)

        for path in sorted(self.root_dir.glob("*.json")):
            result = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
            runs.append(self._build_summary(result))
        return sorted(runs, key=lambda item: item.started_at, reverse=True)

    def _build_summary(self, result: RunResult) -> RunSummary:
        return RunSummary(
            run_id=result.run.run_id,
            request_id=result.run.request_id,
            workflow_id=result.run.workflow_id,
            status=result.run.status,
            started_at=result.run.started_at,
            ended_at=result.run.ended_at,
            current_step_id=result.run.current_step_id,
            metadata=result.run.metadata,
        )


class FileRequestContextStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def put(self, run_id: str, request: RuntimeRequest) -> Path:
        path = self.root_dir / f"{run_id}.json"
        path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        return path

    def get(self, run_id: str) -> Optional[RuntimeRequest]:
        path = self.root_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return RuntimeRequest.model_validate_json(path.read_text(encoding="utf-8"))


class FileChatSessionStore:
    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.summaries_dir = self.root_dir / "_summaries"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.summaries_dir.mkdir(parents=True, exist_ok=True)

    def put(self, session: ChatSession) -> Path:
        path = self.root_dir / f"{session.session.session_id}.json"
        path.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        (self.summaries_dir / f"{session.session.session_id}.json").write_text(
            self._build_summary_record(session).model_dump_json(indent=2),
            encoding="utf-8",
        )
        return path

    def get(self, session_id: str) -> Optional[ChatSession]:
        path = self.root_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return ChatSession.model_validate_json(path.read_text(encoding="utf-8"))

    def list_sessions(self) -> List[ChatSession]:
        sessions: List[ChatSession] = []
        summary_paths = sorted(self.summaries_dir.glob("*.json"))
        if summary_paths:
            for path in summary_paths:
                record = ChatSessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
                sessions.append(ChatSession(session=record, messages=[]))
            return sorted(sessions, key=lambda item: item.session.updated_at, reverse=True)

        for path in sorted(self.root_dir.glob("*.json")):
            sessions.append(ChatSession.model_validate_json(path.read_text(encoding="utf-8")))
        return sorted(sessions, key=lambda item: item.session.updated_at, reverse=True)

    def _build_summary_record(self, session: ChatSession) -> ChatSessionRecord:
        return session.session.model_copy(deep=True)


class MarkdownWritebackAdapter(BaseWritebackAdapter):
    def __init__(self, root_dir: str | Path, checkpoint_store: Optional[BaseCheckpointStore] = None) -> None:
        self.root_dir = Path(root_dir)
        self.docs_dir = self.root_dir / "docs"
        self.memory_dir = self.root_dir / "memory"
        self.graph_dir = self.root_dir / "graph"
        self.host_dir = self.root_dir / "host"
        self.runs_dir = self.root_dir / "runs"
        for directory in (self.docs_dir, self.memory_dir, self.graph_dir, self.host_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.checkpoint_store = checkpoint_store or FileCheckpointStore(self.root_dir / "checkpoint-store")
        self.run_store = FileRunStore(self.runs_dir)
        self._receipts: List[WritebackReceipt] = []

    def persist_artifact(self, artifact: ArtifactRef) -> WritebackReceipt:
        target_dir = self._resolve_artifact_dir(artifact.writeback.target)
        target_dir.mkdir(parents=True, exist_ok=True)
        extension = self._suggest_extension(artifact)
        safe_name = artifact.name if Path(artifact.name).suffix else f"{artifact.name}{extension}"
        output_path = target_dir / safe_name

        content = artifact.metadata.get("content")
        if artifact.writeback.mode == "inline" and content is not None:
            output_path.write_text(str(content), encoding="utf-8")
        else:
            output_path.write_text(self._render_reference_markdown(artifact), encoding="utf-8")

        receipt = WritebackReceipt(
            channel="artifact",
            target=artifact.writeback.target,
            mode=artifact.writeback.mode,
            source_id=artifact.artifact_id,
            location=str(output_path),
            host_action=artifact.writeback.host_action,
            metadata={
                "artifact_name": artifact.name,
                "workflow_id": artifact.metadata.get("workflow_id"),
                "producer_node_id": artifact.metadata.get("producer_node_id"),
            },
        )
        self._receipts.append(receipt)
        return receipt

    def persist_checkpoint(
        self,
        checkpoint: CheckpointRef,
        checkpoint_store: Optional[BaseCheckpointStore] = None,
    ) -> WritebackReceipt:
        store = checkpoint_store or self.checkpoint_store
        record = store.put(checkpoint)
        receipt = WritebackReceipt(
            channel="checkpoint",
            target=checkpoint.writeback.target,
            mode=checkpoint.writeback.mode,
            source_id=checkpoint.checkpoint_id,
            location=record.location,
            host_action=checkpoint.writeback.host_action,
            metadata={
                "run_id": checkpoint.run_id,
                "workflow_id": checkpoint.metadata.get("workflow_id"),
                "current_node_id": checkpoint.state.current_node_id,
            },
        )
        self._receipts.append(receipt)
        return receipt

    def persist_run_result(
        self,
        result: RunResult,
        checkpoint_store: Optional[BaseCheckpointStore] = None,
    ) -> List[WritebackReceipt]:
        receipts: List[WritebackReceipt] = []
        receipts.append(self._persist_run_summary(result))
        for artifact in result.artifacts:
            receipts.append(self.persist_artifact(artifact))
        for checkpoint in result.checkpoints:
            receipts.append(self.persist_checkpoint(checkpoint, checkpoint_store=checkpoint_store))
        return receipts

    @property
    def receipts(self) -> List[WritebackReceipt]:
        return [receipt.model_copy(deep=True) for receipt in self._receipts]

    def _resolve_artifact_dir(self, target: str) -> Path:
        if target == "docs":
            return self.docs_dir
        if target == "memory":
            return self.memory_dir
        if target == "graph":
            return self.graph_dir
        return self.host_dir

    def _suggest_extension(self, artifact: ArtifactRef) -> str:
        if artifact.kind in {"document", "report", "text"}:
            return ".md"
        if artifact.kind == "json":
            return ".json"
        return ".txt"

    def _render_reference_markdown(self, artifact: ArtifactRef) -> str:
        lines = [
            f"# {artifact.name}",
            "",
            f"- artifact_id: `{artifact.artifact_id}`",
            f"- kind: `{artifact.kind}`",
            f"- uri: `{artifact.uri}`",
            f"- workflow_id: `{artifact.metadata.get('workflow_id')}`",
            f"- producer_node_id: `{artifact.metadata.get('producer_node_id')}`",
            f"- mode: `{artifact.writeback.mode}`",
        ]
        content = artifact.metadata.get("content")
        if content is not None:
            lines.extend(["", "## Content Snapshot", "", "```", str(content), "```"])
        else:
            lines.extend(["", "## Content Snapshot", "", "_reference only_"])
        return "\n".join(lines) + "\n"

    def _persist_run_summary(self, result: RunResult) -> WritebackReceipt:
        output_path = self.run_store.put(result)
        receipt = WritebackReceipt(
            channel="artifact",
            target="host",
            mode="reference",
            source_id=result.run.run_id,
            location=str(output_path),
            host_action="persist_artifact_ref",
            metadata={
                "workflow_id": result.run.workflow_id,
                "kind": "run_summary",
            },
        )
        self._receipts.append(receipt)
        return receipt

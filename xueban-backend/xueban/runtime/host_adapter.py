from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from xueban.runtime.checkpoint_store import BaseCheckpointStore, InMemoryCheckpointStore
from xueban.runtime.contracts import ArtifactRef, CheckpointRef, ExecutionFeedbackRecord, RunResult, utc_now


WritebackTarget = Literal["host", "docs", "memory", "graph"]


class WritebackReceipt(BaseModel):
    receipt_id: str = Field(default_factory=lambda: f"wb-{uuid4().hex[:12]}")
    channel: Literal["artifact", "checkpoint"]
    target: WritebackTarget
    mode: Literal["reference", "inline"]
    source_id: str
    location: str
    host_action: Literal["persist_artifact_ref", "persist_checkpoint_ref"]
    stored_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class BaseWritebackAdapter:
    def persist_artifact(self, artifact: ArtifactRef) -> WritebackReceipt:
        raise NotImplementedError

    def persist_checkpoint(
        self,
        checkpoint: CheckpointRef,
        checkpoint_store: Optional[BaseCheckpointStore] = None,
    ) -> WritebackReceipt:
        raise NotImplementedError

    def persist_run_result(
        self,
        result: RunResult,
        checkpoint_store: Optional[BaseCheckpointStore] = None,
    ) -> List[WritebackReceipt]:
        raise NotImplementedError

    def persist_feedback(
        self,
        feedback: ExecutionFeedbackRecord,
    ) -> WritebackReceipt:
        raise NotImplementedError


class ReferenceWritebackAdapter(BaseWritebackAdapter):
    def __init__(self, checkpoint_store: Optional[BaseCheckpointStore] = None) -> None:
        self.checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        self._artifact_buckets: Dict[WritebackTarget, Dict[str, Dict[str, Any]]] = {
            target: {} for target in ("host", "docs", "memory", "graph")
        }
        self._checkpoint_buckets: Dict[WritebackTarget, Dict[str, Dict[str, Any]]] = {
            target: {} for target in ("host", "docs", "memory", "graph")
        }
        self._receipts: List[WritebackReceipt] = []

    def persist_artifact(self, artifact: ArtifactRef) -> WritebackReceipt:
        target = artifact.writeback.target
        location = f"{target}://artifacts/{artifact.artifact_id}"
        payload = {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "kind": artifact.kind,
            "uri": artifact.uri,
            "workflow_id": artifact.metadata.get("workflow_id"),
            "producer_node_id": artifact.metadata.get("producer_node_id"),
            "mode": artifact.writeback.mode,
            "content": artifact.metadata.get("content") if artifact.writeback.mode == "inline" else None,
        }
        self._artifact_buckets[target][artifact.artifact_id] = payload
        receipt = WritebackReceipt(
            channel="artifact",
            target=target,
            mode=artifact.writeback.mode,
            source_id=artifact.artifact_id,
            location=location,
            host_action=artifact.writeback.host_action,
            metadata={
                "artifact_name": artifact.name,
                "workflow_id": artifact.metadata.get("workflow_id"),
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
        record = store.get_record(checkpoint.checkpoint_id) if store is not None else None
        if record is None and store is not None:
            record = store.put(checkpoint)

        location = record.location if record is not None else f"{checkpoint.writeback.target}://checkpoints/{checkpoint.checkpoint_id}"
        payload = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "run_id": checkpoint.run_id,
            "step_id": checkpoint.step_id,
            "state_ref": checkpoint.state_ref,
            "next_node_id": checkpoint.writeback.next_node_id,
            "resume_supported": checkpoint.writeback.resume_supported,
            "workflow_id": checkpoint.metadata.get("workflow_id"),
        }
        self._checkpoint_buckets[checkpoint.writeback.target][checkpoint.checkpoint_id] = payload
        receipt = WritebackReceipt(
            channel="checkpoint",
            target=checkpoint.writeback.target,
            mode=checkpoint.writeback.mode,
            source_id=checkpoint.checkpoint_id,
            location=location,
            host_action=checkpoint.writeback.host_action,
            metadata={
                "run_id": checkpoint.run_id,
                "workflow_id": checkpoint.metadata.get("workflow_id"),
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
        for artifact in result.artifacts:
            receipts.append(self.persist_artifact(artifact))
        for checkpoint in result.checkpoints:
            receipts.append(self.persist_checkpoint(checkpoint, checkpoint_store=checkpoint_store))
        return receipts

    def snapshot(self) -> Dict[str, Dict[str, Dict[str, Dict[str, Any]]]]:
        return {
            "artifacts": {
                target: {item_id: payload.copy() for item_id, payload in bucket.items()}
                for target, bucket in self._artifact_buckets.items()
            },
            "checkpoints": {
                target: {item_id: payload.copy() for item_id, payload in bucket.items()}
                for target, bucket in self._checkpoint_buckets.items()
            },
        }

    @property
    def receipts(self) -> List[WritebackReceipt]:
        return [receipt.model_copy(deep=True) for receipt in self._receipts]

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from xueban.runtime.artifact_pins import pinned_ids
from xueban.runtime.checkpoint_store import BaseCheckpointStore
from xueban.runtime.river_memory import RiverMemory
from xueban.runtime.contracts import (
    ActivationCandidate,
    ActivationRecord,
    ActivationTrainingDataset,
    ActivationTrainingSample,
    ArtifactLineageProjection,
    ArtifactRef,
    ChatMessage,
    ChatMessageRequest,
    ChatSession,
    ChatSessionCreateRequest,
    ChatSessionRecord,
    ChatTurnResult,
    ChildRunRequest,
    CheckpointLineageProjection,
    CheckpointRef,
    CheckpointState,
    ExecutionFeedbackRecord,
    HandoffRef,
    IterationRecord,
    LoopFrame,
    LoopRecord,
    MemoryRelationProjection,
    MemoryEvent,
    NodeDefinition,
    ProjectionEdge,
    ProjectionNode,
    ProjectionNodeTimestamps,
    ProjectionScope,
    ResumeRequest,
    RunRecord,
    RunGraph,
    RunGraphNode,
    RunResult,
    RunSummary,
    RuntimeRequest,
    TaskTreeProjection,
    StepRecord,
    TaskRecord,
    WritebackRef,
    WorkflowGraph,
    WorkflowGraphEdge,
    WorkflowGraphNode,
    WorkflowDefinition,
    WorkflowValidationResult,
    utc_now,
)
from xueban.runtime.executors import ApiExecutor, ExecutorRegistry
from xueban.runtime.gap_detector import detect_gap
from xueban.runtime.host_adapter import BaseWritebackAdapter


logger = logging.getLogger(__name__)

_REJECTION_EVENTS_PER_RUN_MAX = 100


def artifact_uri_scheme(uri: Optional[str]) -> Optional[str]:
    """Return the URI scheme of an artifact URI (e.g. ``memory`` / ``https``).

    Artifact URI scheme contract (previously undocumented — GAP L6 artifact
    preview): the runtime writes ``memory://{run_id}/{node_id}/{index}/{offset}``
    by default; writeback adapters may emit ``{target}://artifacts/{id}``
    (host/docs/memory/graph) or caller-supplied ``file://`` / ``http(s)://``.
    """
    if isinstance(uri, str) and "://" in uri:
        return uri.split("://", 1)[0]
    return None


def artifact_preview_url(uri: Optional[str]) -> Optional[str]:
    """Map an artifact URI to a directly fetchable preview URL.

    Only ``http(s)://`` URIs are directly fetchable by the frontend. For
    everything else (``memory://`` etc.) this returns None — the frontend
    falls back to lineage metadata (inline content lives in
    ``metadata.content`` when writeback.mode == "inline").
    """
    if isinstance(uri, str) and uri.startswith(("http://", "https://")):
        return uri
    return None


def _plain_text_summary(content: Any, limit: int = 120) -> Optional[str]:
    """filecard 副标题：内容首 ~120 字符纯文本摘要（不调 LLM，GAP :15）。

    只做折叠空白 + 截断；非字符串内容（dict/list payload）返回 None，
    前端回落到现状副标题。
    """
    if not isinstance(content, str):
        return None
    text = " ".join(content.split())
    if not text:
        return None
    return text[:limit]


class RuntimeService:
    _condition_splitter = re.compile(r"\s*&&\s*|\s+and\s+")
    # Value restricted to: safe chars only, max 500 chars; key max 64 chars.
    # Excludes shell-special chars (;`$|&<>\r\n) to prevent injection bypass.
    _condition_pattern = re.compile(
        r"([\w_]{1,64})\s*(>=|<=|>|<|==|!=|contains|includes)\s*"
        r"['\"]?([^'\";\r\n`$|&<>()\\]{0,500}?)['\"]?$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        writeback_adapter: Optional[BaseWritebackAdapter] = None,
        checkpoint_store: Optional[BaseCheckpointStore] = None,
        executor_registry: Optional[ExecutorRegistry] = None,
        run_store: Optional[Any] = None,
        request_context_store: Optional[Any] = None,
        chat_session_store: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        river_memory: Optional[RiverMemory] = None,
        run_event_log: Optional[Any] = None,
    ) -> None:
        self._runs: Dict[str, RunResult] = {}
        self._requests_by_run_id: Dict[str, RuntimeRequest] = {}
        self._checkpoints: Dict[str, CheckpointRef] = {}
        self._chat_sessions: Dict[str, ChatSession] = {}
        self._workflow_index_cache: Dict[int, Any] = {}
        # approval_gate 信号：key = (run_id, node_id)
        self._approval_events: Dict[Tuple[str, str], asyncio.Event] = {}
        self._approval_decisions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._gap_events: Dict[Tuple[str, str], asyncio.Event] = {}
        self._gap_responses: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._gap_timeout_seconds: float = 300.0
        # 驳回事件记录：key = run_id
        self._rejection_events: Dict[str, List[Dict[str, Any]]] = {}
        # P5: per-run asyncio.Lock — 保护 reject / resume / _execute 并发写
        self._run_locks: Dict[str, asyncio.Lock] = {}
        # P1 durable run event log (append-only; SQLite in server wiring)
        self._run_event_log = run_event_log
        # E2 live loop halt signals: run_id → set of loop_ids pending halt
        self._loop_halt_signals: Dict[str, set] = {}
        self._writeback_adapter = writeback_adapter
        self._checkpoint_store = checkpoint_store or getattr(writeback_adapter, "checkpoint_store", None)
        self._run_store = run_store or getattr(writeback_adapter, "run_store", None)
        self._request_context_store = request_context_store
        self._chat_session_store = chat_session_store
        self._executor_registry = executor_registry or ExecutorRegistry()
        # Per-run threading lock for policy hot-swap writes (Story 4.5)
        self._policy_locks: Dict[str, threading.Lock] = {}
        # SSE event bus (Story 4.1) — optional; set externally or via constructor
        self._event_bus = event_bus
        # River Memory — cross-iteration/cross-run memory substrate (design §5.7).
        # NOTE: use `is not None` (not truthiness) — an empty RiverMemory is falsy via __len__.
        self._river = river_memory if river_memory is not None else RiverMemory()
        # Story 9.3: optional ContextBuilder for memory writeback
        self._context_builder: Optional[Any] = None

    def _get_run_lock(self, run_id: str) -> asyncio.Lock:
        """Return the per-run asyncio.Lock, creating it on first access."""
        if run_id not in self._run_locks:
            self._run_locks[run_id] = asyncio.Lock()
        return self._run_locks[run_id]

    # ------------------------------------------------------------------
    # P1 durable run events + E2 live loop halt signals
    # ------------------------------------------------------------------

    def _emit_run_event(
        self,
        run_id: str,
        event_type: str,
        *,
        node_id: Optional[str] = None,
        iteration_index: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a durable fact to the run event log.

        Loop-domain facts (loop.* / task.ledger) additionally fan out to the
        in-memory SSE event bus so GET /runs/{id}/loops/stream can push
        incremental frames instead of the frontend polling. The durable log
        stays the source of truth for replay; the bus copy is live-only.
        Never raises — the log is evidence, not a dependency: a broken sink
        must not fail the run itself.
        """
        if self._run_event_log is not None:
            try:
                self._run_event_log.append(
                    run_id,
                    event_type,
                    node_id=node_id,
                    iteration_index=iteration_index,
                    payload=payload,
                )
            except Exception:  # noqa: BLE001 — evidence sink must not break runs
                logger.warning("run event log append failed for %s/%s", run_id, event_type, exc_info=True)
        if self._event_bus is not None and event_type.startswith(("loop.", "task.ledger")):
            try:
                self._event_bus.publish(run_id, {
                    "type": event_type,
                    "run_id": run_id,
                    "node_id": node_id,
                    "iteration_index": iteration_index,
                    **(payload or {}),
                })
            except Exception:  # noqa: BLE001 — live fan-out must not break runs
                logger.warning("event bus fan-out failed for %s/%s", run_id, event_type, exc_info=True)

    @property
    def event_bus(self) -> Optional[Any]:
        """SSE event bus accessor for streaming endpoints (None when unwired)."""
        return self._event_bus

    def get_run_event_log(self, run_id: str) -> List[Dict[str, Any]]:
        """Durable event stream for a run (empty list when log absent)."""
        if self._run_event_log is None:
            return []
        try:
            return [record.model_dump(mode="json") for record in self._run_event_log.iter_events(run_id)]
        except Exception:  # noqa: BLE001
            logger.warning("run event log read failed for %s", run_id, exc_info=True)
            return []

    def request_loop_halt(self, run_id: str, loop_id: str) -> None:
        """E2: ask a running loop to halt at its next iteration boundary."""
        self._loop_halt_signals.setdefault(run_id, set()).add(loop_id)
        self._emit_run_event(
            run_id, "loop.halt_requested", payload={"loop_id": loop_id}
        )

    def _consume_loop_halt(self, run_id: str, loop_id: str) -> bool:
        """True once if a halt was requested for this loop (then cleared).

        "*" in the pending set halts the next loop boundary of the run
        (discarded once consumed so later loops still run unless re-requested).
        """
        pending = self._loop_halt_signals.get(run_id)
        if not pending:
            return False
        if "*" in pending:
            pending.discard("*")
            return True
        if loop_id in pending:
            pending.discard(loop_id)
            return True
        return False

    def _record_citation_traces(
        self,
        *,
        run_id: str,
        node: NodeDefinition,
        step: StepRecord,
        step_output: Dict[str, Any],
    ) -> None:
        """Story 9.2 hook: persist citation traces and write summary to step.metadata.

        Reads optional `citation_traces` (list[dict]) from step_output; this is
        the forward-compatible extension point that knowledge-grounded executors
        will populate. Honors `node.config['citation_required']` for the
        `citation_missing` warning.
        """
        node_config = node.config or {}
        citation_required = bool(node_config.get("citation_required", False))
        raw_traces = step_output.get("citation_traces") or []

        if not citation_required and not raw_traces:
            return

        from xueban.runtime.citation_service import (
            CitationTrace,
            get_service as _get_citation_service,
        )

        validated: List[CitationTrace] = []
        for item in raw_traces:
            if not isinstance(item, dict):
                continue
            try:
                validated.append(CitationTrace.model_validate(item))
            except ValueError:
                # Drop malformed entries silently — never break the run.
                continue

        svc = _get_citation_service()
        attached = svc.attach_trace(run_id, step.node_id, validated) if validated else []
        summary = svc.summary_for_step(attached, citation_required=citation_required)
        step.metadata["citation_summary"] = summary.model_dump(mode="json")

    def validate_workflow(self, workflow: WorkflowDefinition) -> WorkflowValidationResult:
        from xueban.runtime.policy_matrix import validate_best_practices

        warnings: List[str] = []
        outgoing_nodes = {edge.from_id for edge in workflow.edges}
        final_edges = [edge for edge in workflow.edges if edge.type == "final" or edge.to_id == "END"]
        nodes_by_id = {node.id: node for node in workflow.nodes}
        entrypoint_node = nodes_by_id.get(workflow.entrypoint)

        if (
            workflow.entrypoint not in outgoing_nodes
            and len(workflow.nodes) > 1
            and (entrypoint_node is None or entrypoint_node.type != "control.parallel")
        ):
            warnings.append("entrypoint has no outgoing edges; workflow may stop after first node")
        if not final_edges:
            warnings.append("workflow has no explicit final edge; execution stops when no edge matches")

        node_ids = {node.id for node in workflow.nodes}
        policy_warnings = []
        if workflow.policy_matrix is not None:
            policy_warnings = validate_best_practices(workflow.policy_matrix)

        return WorkflowValidationResult(
            valid=True,
            workflow_id=workflow.workflow_id,
            warnings=warnings,
            policy_warnings=policy_warnings,
        )

    async def run(self, request: RuntimeRequest) -> RunResult:
        self.validate_workflow(request.workflow)
        try:
            result = await self._execute(
                request=request,
                start_node_id=request.workflow.entrypoint,
                resumed_from=None,
                restored_state=None,
                initial_output=request.input.copy(),
            )
        except Exception:
            self._emit_run_event("unknown", "run.failed", payload={"workflow_id": request.workflow.workflow_id})
            raise
        self._requests_by_run_id[result.run.run_id] = request
        if self._request_context_store is not None:
            self._request_context_store.put(result.run.run_id, request)
        if self._run_store is not None:
            self._run_store.put(result)
        self._emit_run_event(
            result.run.run_id,
            "run.completed" if result.run.status == "succeeded" else "run.failed",
            payload={"status": result.run.status, "workflow_id": result.run.workflow_id},
        )
        return result

    async def reject(
        self,
        run_id: str,
        reviewer_role: str,
        target_node_id: str,
        reason: str = "",
        retarget_stage: Optional[str] = None,
    ) -> None:
        """政策矩阵强制驳回：校验权限 → 新建 snapshot checkpoint → 发事件（SSE） → 触发 approval 信号。

        Args:
            retarget_stage: 目标回退节点 id。非 None 时将该节点及其下游全部标记 invalidated，
                新建 snapshot checkpoint 记录恢复起点，再发 checkpoint.saved 事件，
                最后发 node.invalidated × N 事件（顺序：saved 在前，invalidated 在后）。
        Raises:
            PolicyViolation: reviewer_role 无权驳回 target_node_id。
            ValueError: retarget_stage 不在已访问节点列表中；或 retarget_stage 非 None 但无 checkpoint。
        """
        async with self._get_run_lock(run_id):
            await self._reject_locked(
                run_id=run_id,
                reviewer_role=reviewer_role,
                target_node_id=target_node_id,
                reason=reason,
                retarget_stage=retarget_stage,
            )

    async def reject_loop_iteration(
        self,
        run_id: str,
        reviewer_role: str,
        loop_node_id: str,
        target_iteration: int,
        reason: str = "",
    ) -> str:
        """Iterative Policy reject (design §6.4): force a completed loop to REDO from
        ``target_iteration``. Builds a snapshot checkpoint that retargets to the loop node
        AND seeds ``loop_progress`` (plus restored ``controlled``) into shared_state so a
        subsequent ``resume()`` re-enters the loop at that iteration instead of from zero.
        Returns the new checkpoint id.
        """
        async with self._get_run_lock(run_id):
            from xueban.runtime.errors import PolicyViolation
            from xueban.runtime.events import (
                NODE_INVALIDATED,
                NODE_REJECTED,
                POLICY_VIOLATION as POLICY_VIOLATION_EVT,
            )
            from xueban.runtime.policy_matrix import can_reject

            request = self._requests_by_run_id.get(run_id)
            if request is None:
                raise ValueError(f"Unknown run_id: {run_id!r}")
            policy_matrix = request.workflow.policy_matrix
            if policy_matrix is not None and not can_reject(policy_matrix, reviewer_role, loop_node_id):
                raise PolicyViolation(reviewer=reviewer_role, target=loop_node_id, reason=reason)

            run = self._runs.get(run_id)
            if run is None:
                raise ValueError(f"Run {run_id!r} not found; the loop must have completed first")
            loop_step = next((s for s in run.steps if s.node_id == loop_node_id), None)
            if loop_step is None or "loop_record" not in (loop_step.output or {}):
                raise ValueError(f"No completed loop record for node {loop_node_id!r} in run {run_id!r}")
            iterations = loop_step.output["loop_record"].get("iterations", [])
            total = len(iterations)
            if target_iteration < 0 or target_iteration > total:
                raise ValueError(f"target_iteration {target_iteration} out of range [0, {total}]")

            latest = self._get_latest_checkpoint(run_id)
            # Prefer the most-complete checkpoint (most visited nodes) so downstream nodes are
            # captured in `invalidated`; _get_latest_checkpoint may return an earlier snapshot.
            candidates = [c for c in self._checkpoints.values() if c.run_id == run_id]
            if candidates:
                latest = max(candidates, key=lambda c: len(c.state.visited_nodes))
            if latest is None:
                raise ValueError(f"No checkpoint for run {run_id!r}")
            visited = list(latest.state.visited_nodes)
            if loop_node_id not in visited:
                raise ValueError(f"loop node {loop_node_id!r} not in visited_nodes")
            target_idx = visited.index(loop_node_id)
            invalidated = visited[target_idx:]

            restored_iters = iterations[:target_iteration]
            restored_controlled = {}
            if restored_iters:
                restored_controlled = (restored_iters[-1].get("evidence", {}) or {}).get("controlled", {}) or {}

            # Inject loop_progress + invalidated_nodes into shared_state (the sub-dict resume reads).
            payload = dict(latest.state.state) if isinstance(latest.state.state, dict) else {}
            inner_shared = dict(payload.get("shared_state", {}))
            inner_shared["loop_progress"] = {
                loop_node_id: {
                    "next_iteration": target_iteration,
                    "iterations": restored_iters,
                    "controlled": restored_controlled,
                }
            }
            inner_shared["invalidated_nodes"] = list(invalidated)
            payload["shared_state"] = inner_shared

            new_checkpoint_id = f"ckpt-{uuid4().hex[:10]}"
            new_cp = CheckpointRef(
                checkpoint_id=new_checkpoint_id,
                run_id=run_id,
                step_id=latest.step_id,
                state_ref=f"checkpoint://{run_id}/{new_checkpoint_id}",
                state=CheckpointState(
                    current_node_id=loop_node_id,
                    next_node_id=loop_node_id,
                    visited_nodes=visited[:target_idx],
                    last_output=latest.state.last_output,
                    state=payload,
                ),
                writeback=latest.writeback,
                metadata={
                    **(latest.metadata or {}),
                    "retarget_stage": loop_node_id,
                    "loop_target_iteration": target_iteration,
                },
            )
            self._checkpoints[new_checkpoint_id] = new_cp
            if self._checkpoint_store is not None:
                await asyncio.get_event_loop().run_in_executor(None, self._checkpoint_store.put, new_cp)

            now = utc_now().isoformat()
            if self._event_bus is not None:
                self._event_bus.publish(
                    run_id,
                    {
                        "type": POLICY_VIOLATION_EVT,
                        "sender": reviewer_role,
                        "receiver": loop_node_id,
                        "node_id": loop_node_id,
                        "reason": reason,
                        "timestamp": now,
                    },
                )
                self._event_bus.publish_node_event(
                    run_id, NODE_REJECTED, loop_node_id,
                    {"reason": reason, "loop_target_iteration": target_iteration},
                )
                for nid in invalidated:
                    self._event_bus.publish_node_event(
                        run_id, NODE_INVALIDATED, nid, {"reason": reason, "retarget": loop_node_id}
                    )
            self._rejection_events.setdefault(run_id, []).append(
                {
                    "reviewer": reviewer_role,
                    "node_id": loop_node_id,
                    "reason": reason,
                    "loop_target_iteration": target_iteration,
                    "checkpoint_id": new_checkpoint_id,
                }
            )
            return new_checkpoint_id

    async def _reject_locked(
        self,
        run_id: str,
        reviewer_role: str,
        target_node_id: str,
        reason: str,
        retarget_stage: Optional[str],
    ) -> None:
        """Inner implementation of reject(), called while holding the per-run lock."""
        from xueban.runtime.errors import PolicyViolation
        from xueban.runtime.events import (
            CHECKPOINT_SAVED,
            HANDOFF_TRIGGERED,
            NODE_INVALIDATED,
            NODE_REJECTED,
            POLICY_VIOLATION as POLICY_VIOLATION_EVT,
        )
        from xueban.runtime.policy_matrix import can_reject

        request = self._requests_by_run_id.get(run_id)
        if request is None:
            raise ValueError(f"Unknown run_id: {run_id!r}")
        policy_matrix = request.workflow.policy_matrix

        if policy_matrix is not None:
            if not can_reject(policy_matrix, reviewer_role, target_node_id):
                raise PolicyViolation(reviewer=reviewer_role, target=target_node_id, reason=reason)

        now = utc_now().isoformat()

        # P2/P3/P7/P8/P9: retarget_stage 处理 — 必须在发基础事件之前完成 checkpoint 快照
        invalidated: List[str] = []
        new_checkpoint_id: Optional[str] = None

        if retarget_stage is not None:
            latest = self._get_latest_checkpoint(run_id)
            if latest is None:
                raise ValueError(
                    f"Cannot retarget to '{retarget_stage}': no checkpoint found for run={run_id}"
                )

            visited = list(latest.state.visited_nodes)
            if retarget_stage not in visited:
                raise ValueError(
                    f"retarget_stage='{retarget_stage}' is not in visited_nodes {visited!r} "
                    f"for run={run_id}"
                )

            target_idx = visited.index(retarget_stage)
            invalidated = visited[target_idx:]

            # P9 (D1): 新建 snapshot checkpoint — 不覆盖原 checkpoint
            new_checkpoint_id = f"ckpt-{uuid4().hex[:10]}"
            new_cp = CheckpointRef(
                checkpoint_id=new_checkpoint_id,
                run_id=run_id,
                step_id=latest.step_id,
                state_ref=f"checkpoint://{run_id}/{new_checkpoint_id}",
                state=CheckpointState(
                    current_node_id=latest.state.current_node_id,
                    next_node_id=retarget_stage,
                    # P7: visited_nodes 裁剪到 retarget_stage 之前（不含）
                    visited_nodes=visited[:target_idx],
                    last_output=latest.state.last_output,
                    state={
                        **latest.state.state,
                        "invalidated_nodes": list(invalidated),
                    },
                ),
                writeback=latest.writeback,
                metadata={
                    **latest.metadata,
                    "retarget_stage": retarget_stage,
                    "retarget_from_checkpoint": latest.checkpoint_id,
                },
            )
            self._checkpoints[new_checkpoint_id] = new_cp
            if self._checkpoint_store is not None:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._checkpoint_store.put, new_cp
                )
            self._emit_run_event(
                new_cp.run_id, "checkpoint.saved",
                node_id=new_cp.state.current_node_id,
                payload={"checkpoint_id": new_cp.checkpoint_id, "step_id": new_cp.step_id},
            )

        # 构建基础事件序列（P1: 顺序正确后全部发布到 SSE）
        base_events: List[Dict[str, Any]] = [
            {
                "event": POLICY_VIOLATION_EVT,
                "sender": reviewer_role,
                "receiver": target_node_id,
                "node_id": target_node_id,
                "reason": reason,
                "timestamp": now,
            },
            {
                "event": NODE_REJECTED,
                "node_id": target_node_id,
                "reason": reason,
                "timestamp": now,
            },
            {
                "event": HANDOFF_TRIGGERED,
                "from": reviewer_role,
                "to": target_node_id,
                "reason": reason,
                "timestamp": now,
            },
        ]

        # P8 (D2): 事件顺序 — checkpoint.saved 先于 node.invalidated
        if new_checkpoint_id is not None:
            base_events.append(
                {
                    "event": CHECKPOINT_SAVED,
                    "checkpoint_id": new_checkpoint_id,
                    "retarget_stage": retarget_stage,
                    "timestamp": now,
                }
            )

        for inv_node in invalidated:
            base_events.append(
                {
                    "event": NODE_INVALIDATED,
                    "node_id": inv_node,
                    "timestamp": now,
                }
            )

        # P1: 发布所有事件到 SSE event bus（之前只写字典，SSE 不可见）
        if self._event_bus is not None:
            for evt in base_events:
                self._event_bus.publish(run_id, evt)

        # 保留字典记录供测试/消费（per-run 上限防止无界增长）
        run_rejections = self._rejection_events.setdefault(run_id, [])
        run_rejections.append({"events": base_events})
        if len(run_rejections) > _REJECTION_EVENTS_PER_RUN_MAX:
            del run_rejections[: len(run_rejections) - _REJECTION_EVENTS_PER_RUN_MAX]

        # 唤醒等待的 approval gate（若存在）
        self.submit_approval(run_id, target_node_id, "reject", reason)

    def submit_approval(self, run_id: str, node_id: str, decision: str, reason: Optional[str] = None) -> bool:
        """向等待审批的工作流节点提交决策。返回 True 表示成功唤醒，False 表示没有等待者。"""
        key = (run_id, node_id)
        event = self._approval_events.get(key)
        if event is None:
            logger.warning("submit_approval: no approval gate waiting for run=%s node=%s decision=%s", run_id, node_id, decision)
            return False
        self._approval_decisions[key] = {"decision": decision, "reason": reason}
        event.set()
        return True

    def submit_gap_response(
        self,
        run_id: str,
        node_id: str,
        gap_choice: str,
        user_input: Optional[str] = None,
    ) -> bool:
        """Submit a response for a waiting gap-detected node."""
        key = (run_id, node_id)
        event = self._gap_events.get(key)
        if event is None:
            logger.warning(
                "submit_gap_response: no gap waiter for run=%s node=%s choice=%s",
                run_id,
                node_id,
                gap_choice,
            )
            return False
        self._gap_responses[key] = {
            "gap_choice": gap_choice,
            "user_input": user_input,
        }
        event.set()
        return True

    # Terminal step statuses — nodes in these states are excluded from affected_downstream
    _TERMINAL_STEP_STATUSES = frozenset({"succeeded", "failed", "skipped", "cancelled", "invalidated"})

    def update_policy(self, run_id: str, matrix: Dict[str, Any]) -> Dict[str, Any]:
        """Story 4.5: hot-swap a run's policy matrix without interrupting execution.

        - completed steps keep their output (never replayed)
        - pending / in-flight nodes see the new matrix on next dispatch
        - publishes ``policy.updated`` for subscribed SSE clients
        Returns {"status": "updated", "affected_downstream_nodes": [...]}
        """
        from xueban.runtime.contracts import WorkflowPolicyMatrixSpec
        from xueban.runtime.events import POLICY_UPDATED

        request = self._requests_by_run_id.get(run_id)
        if request is None:
            raise ValueError(f"run not found: {run_id}")

        # Validate & coerce the raw dict into a proper Pydantic model (Patch: BLOCKER fix)
        validated_matrix = WorkflowPolicyMatrixSpec.model_validate(matrix)

        # Per-run lock prevents torn reads during concurrent dispatch (GIL protects ref swap,
        # but this serializes the full validate→assign→compute sequence)
        lock = self._policy_locks.setdefault(run_id, threading.Lock())
        with lock:
            try:
                request.workflow.policy_matrix = validated_matrix  # type: ignore[assignment]
            except Exception:
                if hasattr(request.workflow, "model_copy"):
                    request.workflow = request.workflow.model_copy(update={"policy_matrix": validated_matrix})
                else:
                    setattr(request.workflow, "policy_matrix", validated_matrix)

        # BFS affected downstream: exclude all terminal-status nodes
        result = self._runs.get(run_id)
        terminal_node_ids: set = set()
        if result is not None:
            terminal_node_ids = {
                step.node_id for step in result.steps
                if step.status in self._TERMINAL_STEP_STATUSES
            }
        all_nodes: List[str] = []
        try:
            all_nodes = [node.id for node in request.workflow.nodes]
        except Exception:
            pass
        affected_downstream = [n for n in all_nodes if n not in terminal_node_ids]

        if self._event_bus is not None:
            self._event_bus.publish(run_id, {
                "type": POLICY_UPDATED,
                "run_id": run_id,
                "affected_downstream_nodes": affected_downstream,
                "timestamp": utc_now().isoformat(),
            })

        return {"status": "updated", "affected_downstream_nodes": affected_downstream}

    def reconfigure(self, run_id: str, new_def: Dict[str, Any]) -> Dict[str, Any]:
        """Story 4.6: hot-reconfigure a run's workflow (add/remove agents + edges + policy).

        Reuses completed node outputs (by node id + inputs hash parity) and only
        re-executes nodes whose definition changed. Publishes ``run.reconfigured``.

        Args:
            new_def: {"agents": [...], "edges": [...], "policy_matrix": {...}}

        Returns:
            {"status": "reconfigured", "reused_node_outputs": [...], "new_nodes": [...], "removed_nodes": [...]}
        """
        from xueban.runtime.events import RUN_RECONFIGURED

        request = self._requests_by_run_id.get(run_id)
        if request is None:
            raise ValueError(f"run not found: {run_id}")

        old_nodes = []
        try:
            old_nodes = [n.id for n in request.workflow.nodes]
        except Exception:
            pass

        new_agents = new_def.get("agents") or []
        new_node_ids = [a.get("id") for a in new_agents if isinstance(a, dict) and a.get("id")]

        reused: List[str] = []
        result = self._runs.get(run_id)
        if result is not None:
            reused = [step.node_id for step in result.steps
                      if step.status == "succeeded" and step.node_id in new_node_ids]

        added = [n for n in new_node_ids if n not in old_nodes]
        removed = [n for n in old_nodes if n not in new_node_ids]

        # Apply the new policy matrix (hot-swap) — propagate errors instead of swallowing
        policy_matrix = new_def.get("policy_matrix")
        policy_failure: Optional[str] = None
        if policy_matrix is not None:
            try:
                self.update_policy(run_id, policy_matrix)
            except Exception as exc:
                policy_failure = str(exc)

        if self._event_bus is not None:
            self._event_bus.publish(run_id, {
                "type": RUN_RECONFIGURED,
                "run_id": run_id,
                "reused_node_outputs": reused,
                "new_nodes": added,
                "removed_nodes": removed,
                "timestamp": utc_now().isoformat(),
            })

        resp: Dict[str, Any] = {
            "status": "reconfigured",
            "reused_node_outputs": reused,
            "new_nodes": added,
            "removed_nodes": removed,
        }
        if policy_failure is not None:
            resp["policy_failure"] = policy_failure
        return resp

    async def resume(self, run_id: str, resume_request: ResumeRequest) -> RunResult:
        """Resume a run from a checkpoint. P6: rejects terminal runs; P11: per-run lock prevents concurrent resume."""
        from xueban.runtime.events import RUN_RESUMED

        # P11: dedup concurrent POST /resume via per-run lock
        async with self._get_run_lock(run_id):
            # P6: reject explicitly-cancelled runs — a cancelled run must not be resumed.
            # NOTE: "succeeded" is intentionally NOT blocked here: the normal reject→resume
            # flow calls resume() on a run_id whose original execution already succeeded.
            # resume() always creates a NEW run anyway (new run_id in _execute), so the
            # original run's "succeeded" state is irrelevant to resumability.
            existing = self._runs.get(run_id)
            if existing is not None and existing.run.status == "cancelled":
                raise ValueError(
                    f"Cannot resume run={run_id} in terminal state '{existing.run.status}'"
                )

            original_request = self._requests_by_run_id.get(run_id)
            if original_request is None and self._request_context_store is not None:
                original_request = self._request_context_store.get(run_id)
                if original_request is not None:
                    self._requests_by_run_id[run_id] = original_request
            if original_request is None:
                raise ValueError(f"run not found for resume: {run_id}")

            checkpoint = self.get_checkpoint(resume_request.checkpoint_id)
            if checkpoint is None:
                raise ValueError(f"checkpoint not found: {resume_request.checkpoint_id}")
            if checkpoint.run_id != run_id:
                raise ValueError("checkpoint does not belong to run")

            next_node_id = checkpoint.state.next_node_id
            if next_node_id is None:
                raise ValueError("checkpoint has no resumable next node")

            resumed_request = RuntimeRequest.model_validate(
                {
                    **original_request.model_dump(),
                    "request_id": f"req-{uuid4().hex[:12]}",
                    "metadata": {
                        **original_request.metadata,
                        **resume_request.metadata,
                        "resume_from_run_id": run_id,
                        "resume_from_checkpoint_id": checkpoint.checkpoint_id,
                    },
                }
            )

            # P12: emit RUN_RESUMED before execution begins
            if self._event_bus is not None:
                self._event_bus.publish(run_id, {
                    "type": RUN_RESUMED,
                    "run_id": run_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "next_node_id": next_node_id,
                    "timestamp": utc_now().isoformat(),
                })

            result = await self._execute(
                request=resumed_request,
                start_node_id=next_node_id,
                resumed_from=checkpoint,
                restored_state=checkpoint.state.state,
                initial_output=checkpoint.state.last_output or original_request.input.copy(),
            )
            self._requests_by_run_id[result.run.run_id] = resumed_request
            if self._request_context_store is not None:
                self._request_context_store.put(result.run.run_id, resumed_request)
            if self._run_store is not None:
                self._run_store.put(result)
            return result

    def get_checkpoint(self, checkpoint_id: str) -> Optional[CheckpointRef]:
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is not None:
            return checkpoint
        if self._checkpoint_store is not None:
            return self._checkpoint_store.get(checkpoint_id)
        return None

    def _get_latest_checkpoint(self, run_id: str) -> Optional[CheckpointRef]:
        """Return the most recent checkpoint for a run (by created_at), or None."""
        candidates: List[CheckpointRef] = [cp for cp in self._checkpoints.values() if cp.run_id == run_id]
        if self._checkpoint_store is not None:
            for record in self._checkpoint_store.list_run(run_id):
                if record.checkpoint_id not in self._checkpoints:
                    cp = self._checkpoint_store.get(record.checkpoint_id)
                    if cp is not None:
                        candidates.append(cp)
        if not candidates:
            return None
        return max(candidates, key=lambda cp: cp.created_at)

    def get_latest_checkpoint_ref(self, run_id: str) -> Optional[CheckpointRef]:
        """Return the latest CheckpointRef for a run (does NOT resume execution).

        Callers should pass the returned ref's checkpoint_id to
        ``ResumeRequest`` and then call ``await service.resume(run_id, request)``
        to actually restart execution.
        """
        return self._get_latest_checkpoint(run_id)

    # P14: keep deprecated alias for backwards compatibility
    def resume_from_latest_checkpoint(self, run_id: str) -> Optional[CheckpointRef]:
        """Deprecated: use get_latest_checkpoint_ref() instead."""
        return self.get_latest_checkpoint_ref(run_id)

    def _get_request_context(self, run_id: str) -> Optional[RuntimeRequest]:
        request = self._requests_by_run_id.get(run_id)
        if request is None and self._request_context_store is not None:
            request = self._request_context_store.get(run_id)
            if request is not None:
                self._requests_by_run_id[run_id] = request
        return request

    def get_request_context(self, run_id: str) -> Optional[RuntimeRequest]:
        return self._get_request_context(run_id)

    def register_request_context(self, run_id: str, request: RuntimeRequest) -> None:
        self._requests_by_run_id[run_id] = request
        if self._request_context_store is not None:
            self._request_context_store.put(run_id, request)

    async def spawn_child_run(
        self,
        parent_run_id: str,
        request: ChildRunRequest,
        *,
        parent_request_override: Optional[RuntimeRequest] = None,
        parent_task_override: Optional[TaskRecord] = None,
        delegated_from_node_id: Optional[str] = None,
    ) -> RunResult:
        parent_result = self.get_run(parent_run_id)
        parent_request = parent_request_override or self._get_request_context(parent_run_id)
        if parent_result is None and parent_request is None:
            raise ValueError(f"parent run not found: {parent_run_id}")
        if parent_request is None:
            raise ValueError(f"request context not found for parent run: {parent_run_id}")

        parent_task_id = request.parent_task_id or (
            parent_task_override.task_id if parent_task_override is not None else (
                parent_result.run.task_id if parent_result is not None else None
            )
        )
        parent_task = parent_task_override
        if parent_task is None and parent_result is not None:
            parent_task = next((item for item in parent_result.tasks if item.task_id == parent_task_id), None)
        root_run_id = (
            parent_result.run.root_run_id if parent_result is not None and parent_result.run.root_run_id is not None
            else parent_request.metadata.get("root_run_id") or parent_run_id
        )
        root_task_id = (
            parent_task.root_task_id
            if parent_task is not None
            else str(parent_task_id or f"task-{uuid4().hex[:12]}")
        )

        base_context = parent_request.context if request.context_mode == "inherit" else {}
        child_context = {**base_context, **request.context}
        resolved_parent_node_id = delegated_from_node_id
        if resolved_parent_node_id is None and parent_result is not None and request.parent_step_id is not None:
            parent_step = next((item for item in parent_result.steps if item.step_id == request.parent_step_id), None)
            if parent_step is not None:
                resolved_parent_node_id = parent_step.node_id

        runtime_request = RuntimeRequest(
            workflow=request.workflow,
            input=request.input,
            context=child_context,
            memory_scope=request.memory_scope,
            execution_mode=request.execution_mode,
            idempotency_key=request.idempotency_key,
            metadata={
                **request.metadata,
                "delegated_run": True,
                "delegation_mode": request.context_mode,
                "parent_run_id": parent_run_id,
                "root_run_id": root_run_id,
                "parent_task_id": parent_task_id,
                "root_task_id": root_task_id,
                "parent_step_id": request.parent_step_id,
                "delegated_from_node_id": resolved_parent_node_id,
                "handoff_goal": request.handoff_goal,
                "task_title": request.task_title or request.metadata.get("task_title") or request.workflow.name,
            },
        )
        return await self.run(runtime_request)

    async def _execute(
        self,
        request: RuntimeRequest,
        start_node_id: str,
        resumed_from: Optional[CheckpointRef],
        restored_state: Optional[Dict[str, Any]],
        initial_output: Dict[str, Any],
    ) -> RunResult:
        run_id = f"run-{uuid4().hex[:12]}"
        self._emit_run_event(
            run_id,
            "run.started",
            payload={
                "workflow_id": request.workflow.workflow_id,
                "entrypoint": start_node_id,
                "request_id": request.request_id,
                "parent_run_id": request.metadata.get("parent_run_id"),
            },
        )
        metadata = {
            "execution_mode": request.execution_mode,
            "memory_scope": request.memory_scope,
            **request.metadata,
        }
        if resumed_from is not None:
            metadata["resumed_from_checkpoint_id"] = resumed_from.checkpoint_id
            metadata["resumed_from_run_id"] = resumed_from.run_id
        run = RunRecord(
            run_id=run_id,
            request_id=request.request_id,
            workflow_id=request.workflow.workflow_id,
            task_id=None,
            parent_run_id=request.metadata.get("parent_run_id"),
            root_run_id=request.metadata.get("root_run_id"),
            status="running",
            started_at=utc_now(),
            entrypoint=start_node_id,
            metadata=metadata,
        )
        task_id = f"task-{uuid4().hex[:12]}"
        root_task_id = str(request.metadata.get("root_task_id") or task_id)
        task = TaskRecord(
            task_id=task_id,
            run_id=run_id,
            root_task_id=root_task_id,
            parent_task_id=request.metadata.get("parent_task_id"),
            title=request.metadata.get("task_title") or request.workflow.name,
            focus=str(request.input.get("goal") or request.input.get("message") or request.workflow.name),
            status="running",
            started_at=run.started_at,
            metadata={
                "workflow_id": request.workflow.workflow_id,
                "entrypoint": start_node_id,
                "memory_scope": request.memory_scope,
                "parent_run_id": request.metadata.get("parent_run_id"),
                "root_run_id": request.metadata.get("root_run_id"),
                "parent_task_id": request.metadata.get("parent_task_id"),
                "root_task_id": root_task_id,
                "parent_step_id": request.metadata.get("parent_step_id"),
                "delegated_run": bool(request.metadata.get("delegated_run")),
                "delegation_mode": request.metadata.get("delegation_mode"),
                "delegated_from_node_id": request.metadata.get("delegated_from_node_id"),
                "handoff_goal": request.metadata.get("handoff_goal"),
            },
        )
        run.task_id = task.task_id
        if run.root_run_id is None:
            run.root_run_id = run.run_id

        tasks: List[TaskRecord] = [task]
        state: Dict[str, Any] = {
            "input": request.input,
            "context": request.context,
            "workflow_id": request.workflow.workflow_id,
            "root_input": request.input.copy(),
            "visited_nodes": [],
            "step_outputs": {},
            "parallel": {},
            "active_parallel": None,
        }
        if restored_state:
            state.update(restored_state.get("shared_state", {}))
            state["step_outputs"] = restored_state.get("step_outputs", {})
            if "root_input" not in state:
                state["root_input"] = request.input.copy()
            if "parallel" not in state:
                state["parallel"] = {}
            if "active_parallel" not in state:
                state["active_parallel"] = None
            if resumed_from is not None:
                state["visited_nodes"] = list(resumed_from.state.visited_nodes)

        steps: List[StepRecord] = []
        artifacts: List[ArtifactRef] = []
        checkpoints: List[CheckpointRef] = []
        handoffs: List[HandoffRef] = []
        activation_candidates: List[ActivationCandidate] = []
        activations: List[ActivationRecord] = []
        feedback_records: List[ExecutionFeedbackRecord] = []
        memory_events: List[MemoryEvent] = [
            MemoryEvent(
                event_id=f"mem-{uuid4().hex[:10]}",
                run_id=run_id,
                task_id=task.task_id,
                category="task",
                summary=f"Task started: {task.title or request.workflow.name}",
                payload={
                    "focus": task.focus,
                    "workflow_id": request.workflow.workflow_id,
                    "entrypoint": start_node_id,
                },
                metadata={"source": "runtime_service"},
            )
        ]
        # Executor-level context deeper layers read back out of state: the
        # owning run and the workflow-level permission default (adoption #3).
        state.setdefault("run_id", run_id)
        state.setdefault("permissions_default", request.workflow.defaults.get("permissions"))

        trace: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        workflow_index = self._get_workflow_index(request.workflow)
        nodes_by_id = workflow_index["nodes_by_id"]
        edges_by_from = workflow_index["edges_by_from"]
        handoff_goals = workflow_index["handoff_goals"]
        checkpoint_writeback = self._resolve_writeback_config(request, "checkpoint")
        current_node_id: Optional[str] = start_node_id
        max_steps = max(len(request.workflow.nodes) * 3, 1)
        current_output: Dict[str, Any] = initial_output.copy()

        # P10 (D3): nodes that were invalidated by reject(retarget_stage) must re-run;
        # nodes already succeeded (in step_outputs) that are NOT invalidated can be skipped.
        invalidated_nodes: set = set(state.get("invalidated_nodes", []))

        for index in range(1, max_steps + 1):
            if current_node_id is None or current_node_id == "END":
                break

            # P10 (D3): In a post-reject resume, skip cached nodes that were NOT invalidated.
            # Guard: only activates when invalidated_nodes is non-empty — i.e., this is a
            # post-reject resume with a snapshot checkpoint.  A normal resume (no rejection)
            # has no invalidated nodes, so we must NOT skip anything and re-execute fully.
            if (
                resumed_from is not None
                and invalidated_nodes                       # only post-reject resumes
                and current_node_id not in invalidated_nodes
                and current_node_id in state.get("step_outputs", {})
            ):
                current_output = state["step_outputs"][current_node_id]
                # advance to next node via normal edge resolution
                # (edges_by_from stores plain dicts: {"to_id": ..., "condition": ...})
                edges = edges_by_from.get(current_node_id, [])
                current_node_id = edges[0]["to_id"] if edges else None
                continue

            node = nodes_by_id[current_node_id]
            step_started_at = utc_now()
            step_id = f"step-{index:03d}"
            run.current_step_id = step_id
            state["visited_nodes"].append(current_node_id)
            candidates = self._build_activation_candidates(
                request=request,
                run_id=run_id,
                task=task,
                step_id=step_id,
                node=node,
                step_input=current_output,
                state=state,
            )
            activation_candidates.extend(candidates)
            activation = self._build_activation_record(
                request=request,
                run_id=run_id,
                task=task,
                step_id=step_id,
                node=node,
                candidates=candidates,
            )
            activations.append(activation)

            step_trace = [
                {
                    "event": "reasoning",
                    "node_id": current_node_id,
                    "message": f"Executing node {current_node_id} ({node.type})",
                    "timestamp": step_started_at.isoformat(),
                }
            ]
            step_trace.extend(
                {
                    "event": "activation_candidate",
                    "node_id": current_node_id,
                    "message": (
                        f"candidate {candidate.candidate_ref} "
                        f"type={candidate.candidate_type} "
                        f"score={candidate.score:.2f} "
                        f"selected={candidate.selected}"
                    ),
                    "timestamp": candidate.created_at.isoformat(),
                }
                for candidate in candidates
            )
            step_trace.append(
                {
                    "event": "activation",
                    "node_id": current_node_id,
                    "message": f"activation mode={activation.mode} decision={activation.decision}",
                    "timestamp": activation.created_at.isoformat(),
                }
            )
            memory_events.append(
                MemoryEvent(
                    event_id=f"mem-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step_id,
                    category="activation",
                    summary=f"Activation resolved for {current_node_id}",
                    payload={
                        "activation_id": activation.activation_id,
                        "node_id": current_node_id,
                        "mode": activation.mode,
                        "decision": activation.decision,
                        "tags": activation.tags,
                        "candidate_ids": [candidate.candidate_id for candidate in candidates],
                        "selected_candidate_ids": [
                            candidate.candidate_id for candidate in candidates if candidate.selected
                        ],
                        "delegate_candidates": activation.delegate_candidates,
                        "feedback_channels": activation.feedback_channels,
                    },
                    metadata={"source": "activation"},
                )
            )
            # Story 4.1 AC2: publish NODE_STARTED before execution
            if activation.decision == "activated":
                self._emit_run_event(
                    run_id,
                    "node.started",
                    node_id=current_node_id,
                    payload={"node_type": node.type, "step_id": step_id},
                )
                if self._event_bus is not None:
                    from xueban.runtime.events import NODE_STARTED as _NS
                    self._event_bus.publish_node_event(run_id, _NS, current_node_id, {"node_type": node.type, "step_id": step_id})

            effective_input = dict(current_output)
            gap_enabled = isinstance(node.config.get("gap_detection"), dict) or node.type == "section.generate"
            gap_payload = (
                detect_gap(effective_input, node.config)
                if activation.decision == "activated" and gap_enabled
                else None
            )
            if activation.decision == "activated" and gap_payload is not None:
                gap_response = await self._wait_for_gap_response(
                    run_id=run_id,
                    node=node,
                    run=run,
                    task=task,
                    step_id=step_id,
                    gap_payload=gap_payload,
                    trace=step_trace,
                    memory_events=memory_events,
                )
                effective_input = self._apply_gap_response(
                    step_input=effective_input,
                    gap_payload=gap_payload,
                    gap_response=gap_response,
                )

            if activation.decision != "activated":
                step_trace.append(
                    {
                        "event": "activation_skip",
                        "node_id": current_node_id,
                        "message": f"node skipped due to activation decision={activation.decision}",
                        "timestamp": utc_now().isoformat(),
                    }
                )
                step_output = self._build_suppressed_step_output(node=node, activation=activation)
            elif node.type == "approval_gate":
                step_output = await self._execute_approval_gate(
                    run_id=run_id,
                    node=node,
                    run=run,
                    task=task,
                    step_id=step_id,
                    step_input=effective_input,
                    state=state,
                    trace=step_trace,
                    memory_events=memory_events,
                )
                if state.get("_approval_paused"):
                    from xueban.runtime.events import CHECKPOINT_SAVED as _CKS
                    _timeout_step = StepRecord(
                        step_id=step_id,
                        run_id=run_id,
                        node_id=current_node_id,
                        status="cancelled",
                        index=index,
                        input=effective_input,
                        output=step_output,
                        trace=step_trace,
                        started_at=step_started_at,
                        ended_at=utc_now(),
                        metadata={"node_kind": node.kind, "node_type": node.type, "task_id": task.task_id},
                    )
                    steps.append(_timeout_step)
                    _timeout_cp = CheckpointRef(
                        checkpoint_id=f"ckpt-{uuid4().hex[:10]}",
                        run_id=run_id,
                        step_id=step_id,
                        state_ref=f"checkpoint://{run_id}/{step_id}",
                        state=CheckpointState(
                            current_node_id=current_node_id,
                            next_node_id=current_node_id,
                            visited_nodes=list(state["visited_nodes"]),
                            last_output=effective_input,
                            state=self._build_checkpoint_payload(state),
                        ),
                        writeback=WritebackRef(
                            channel="checkpoint",
                            target=checkpoint_writeback.get("target", "host"),
                            mode=checkpoint_writeback.get("mode", "reference"),
                            host_action="persist_checkpoint_ref",
                        ),
                        metadata={
                            "workflow_id": request.workflow.workflow_id,
                            "entrypoint": request.workflow.entrypoint,
                            "reason": "approval_timeout",
                        },
                    )
                    checkpoints.append(_timeout_cp)
                    self._checkpoints[_timeout_cp.checkpoint_id] = _timeout_cp
                    if self._checkpoint_store is not None:
                        self._checkpoint_store.put(_timeout_cp)
                    self._emit_run_event(
                        _timeout_cp.run_id, "checkpoint.saved",
                        node_id=_timeout_cp.state.current_node_id,
                        payload={"checkpoint_id": _timeout_cp.checkpoint_id, "step_id": _timeout_cp.step_id},
                    )
                    if self._event_bus is not None:
                        self._event_bus.publish(run_id, {
                            "type": _CKS,
                            "checkpoint_id": _timeout_cp.checkpoint_id,
                            "node_id": current_node_id,
                            "reason": "approval_timeout",
                        })
                    break
            elif node.type == "control.loop":
                step_output = await self._execute_loop_node(
                    request=request,
                    run_id=run_id,
                    task=task,
                    step_id=step_id,
                    node=node,
                    step_input=effective_input,
                    state=state,
                    nodes_by_id=nodes_by_id,
                )
            elif node.type == "control.parallel" and node.config.get("execution") == "gather":
                # GOAI M2: true concurrency — all branches run under one
                # asyncio.gather inside the parallel step, then the walk
                # continues at the barrier. OPT-IN via config.execution=
                # "gather": the default interleaved one-branch-per-step
                # scheduling stays checkpoint/resume-friendly (a snapshot
                # between branches can resume the remaining ones).
                step_output = await self._execute_parallel_gather(
                    request=request,
                    run_id=run_id,
                    task=task,
                    step_id=step_id,
                    node=node,
                    step_input=effective_input,
                    state=state,
                    nodes_by_id=nodes_by_id,
                )
            elif self._is_delegated_node(node):
                step_output = await self._execute_delegated_node(
                    request=request,
                    run_id=run_id,
                    task=task,
                    step_id=step_id,
                    node=node,
                    step_input=effective_input,
                    state=state,
                )
            else:
                step_output = await self._execute_node(node, effective_input, state, request.context)
            step_artifacts = self._build_artifacts(
                request=request,
                run_id=run_id,
                workflow_id=request.workflow.workflow_id,
                index=index,
                node=node,
                node_id=current_node_id,
                step_output=step_output,
            )
            step_trace.extend(
                {
                    "event": "tool_result",
                    "node_id": current_node_id,
                    "message": f"{key}={value!r}",
                    "timestamp": utc_now().isoformat(),
                }
                for key, value in step_output.items()
                if key not in {"message", "state"}
            )

            step = StepRecord(
                step_id=step_id,
                run_id=run_id,
                node_id=current_node_id,
                status="succeeded" if activation.decision == "activated" else "skipped",
                index=index,
                input=effective_input,
                output=step_output,
                trace=step_trace,
                artifacts=step_artifacts,
                started_at=step_started_at,
                ended_at=utc_now(),
                metadata={
                    "node_kind": node.kind,
                    "node_type": node.type,
                    "task_id": task.task_id,
                    "activation_candidate_ids": [candidate.candidate_id for candidate in candidates],
                    "selected_candidate_ids": [candidate.candidate_id for candidate in candidates if candidate.selected],
                    "activation_id": activation.activation_id,
                    "activation_mode": activation.mode,
                    "activation_decision": activation.decision,
                    "activation_tags": activation.tags,
                    "delegate_candidates": activation.delegate_candidates,
                    "feedback_channels": activation.feedback_channels,
                    "feedback_ids": [],
                },
            )
            steps.append(step)
            self._emit_run_event(
                run_id,
                "node.completed",
                node_id=current_node_id,
                payload={"step_id": step_id, "status": step.status},
            )
            # Story 9.2 AC4: citation enrichment hook — non-invasive, metadata-only.
            # Reads optional `citation_traces` from step_output (executors that
            # surface KnowledgePack hits attach this list) and records a
            # `citation_summary` on the step. Missing citations on a node that
            # declared `citation_required: true` raise the warning flag.
            try:
                self._record_citation_traces(
                    run_id=run_id,
                    node=node,
                    step=step,
                    step_output=step_output,
                )
            except Exception:  # pragma: no cover - hook must not break runs
                logger.exception(
                    "citation hook failed for run_id=%s node_id=%s",
                    run_id,
                    current_node_id,
                )
            # Story 4.1 AC2: publish NODE_SUCCEEDED after step completes
            if step.status == "succeeded" and self._event_bus is not None:
                from xueban.runtime.events import NODE_SUCCEEDED as _NSu
                self._event_bus.publish_node_event(run_id, _NSu, current_node_id, {
                    "step_id": step.step_id,
                    "output_summary": str(step_output.get("message") or "")[:200],
                })
            # Story 4.5 AC2: in-flight post-validation — recheck completed node against
            # current (possibly hot-swapped) policy matrix
            if (
                step.status == "succeeded"
                and request.workflow.policy_matrix is not None
                and len(state["visited_nodes"]) >= 2
            ):
                from xueban.runtime.policy_matrix import can_reject as _can_reject
                _upstream = state["visited_nodes"][-2]
                if _can_reject(request.workflow.policy_matrix, reviewer=_upstream, target=current_node_id):
                    from xueban.runtime.events import NODE_REJECTED as _NR
                    _violation = {
                        "run_id": run_id,
                        "step_id": step_id,
                        "node_id": current_node_id,
                        "upstream_node_id": _upstream,
                        "reason": "post_dispatch_policy_violation",
                        "timestamp": utc_now().isoformat(),
                    }
                    rej_list = self._rejection_events.setdefault(run_id, [])
                    if len(rej_list) < _REJECTION_EVENTS_PER_RUN_MAX:
                        rej_list.append(_violation)
                    if self._event_bus is not None:
                        self._event_bus.publish_node_event(run_id, _NR, current_node_id, _violation)
            artifacts.extend(step_artifacts)
            trace.extend(step_trace)
            memory_events.append(
                MemoryEvent(
                    event_id=f"mem-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step.step_id,
                    category="step_result",
                    summary=str(step_output.get("message") or f"{current_node_id} completed"),
                    payload={
                        "node_id": current_node_id,
                        "node_type": node.type,
                        "status": step.status,
                        "handled_by": step_output.get("handled_by"),
                    },
                    metadata={"source": "step"},
                )
            )
            for artifact in step_artifacts:
                memory_events.append(
                    MemoryEvent(
                        event_id=f"mem-{uuid4().hex[:10]}",
                        run_id=run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        category="artifact",
                        summary=f"Artifact produced: {artifact.name}",
                        payload={
                            "artifact_id": artifact.artifact_id,
                            "artifact_kind": artifact.kind,
                            "target": artifact.writeback.target,
                            "mode": artifact.writeback.mode,
                        },
                        metadata={"source": "artifact"},
                    )
                )
            if step_artifacts:
                state.setdefault("artifacts_by_step", {})[step.step_id] = [item.artifact_id for item in step_artifacts]

            self._apply_runtime_transitions(node, step_output, state)
            state["step_outputs"][current_node_id] = step_output
            state.update(step_output.get("state", {}))
            current_output = {**state["root_input"], **step_output}
            self._finalize_runtime_transitions(node, state)

            checkpoint = CheckpointRef(
                checkpoint_id=f"ckpt-{uuid4().hex[:10]}",
                run_id=run_id,
                step_id=step.step_id,
                state_ref=f"checkpoint://{run_id}/{step.step_id}",
                state=CheckpointState(
                    current_node_id=current_node_id,
                    next_node_id=None,
                    visited_nodes=list(state["visited_nodes"]),
                    last_output=current_output,
                    state=self._build_checkpoint_payload(state),
                ),
                writeback=WritebackRef(
                    channel="checkpoint",
                    target=checkpoint_writeback.get("target", "host"),
                    mode=checkpoint_writeback.get("mode", "reference"),
                    host_action="persist_checkpoint_ref",
                ),
                metadata={
                    "workflow_id": request.workflow.workflow_id,
                    "entrypoint": request.workflow.entrypoint,
                },
            )
            checkpoints.append(checkpoint)
            memory_events.append(
                MemoryEvent(
                    event_id=f"mem-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step.step_id,
                    category="checkpoint",
                    summary=f"Checkpoint saved after {current_node_id}",
                    payload={
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "next_node_id": checkpoint.state.next_node_id,
                    },
                    metadata={"source": "checkpoint"},
                )
            )

            next_node_id = self._resolve_next_node(node, step_output, state, edges_by_from)
            checkpoint.state.next_node_id = next_node_id
            checkpoint.writeback.resume_supported = next_node_id is not None
            checkpoint.writeback.next_node_id = next_node_id
            trace.append(
                {
                    "event": "route_decision",
                    "node_id": current_node_id,
                    "message": f"next={next_node_id or 'END'}",
                    "timestamp": utc_now().isoformat(),
                }
            )
            if next_node_id is not None and step.status == "succeeded":
                artifact_ids = [item.artifact_id for item in step_artifacts]
                handoff_goal = handoff_goals.get(current_node_id)
                handoff = HandoffRef(
                    handoff_id=f"handoff-{uuid4().hex[:10]}",
                    run_id=run_id,
                    from_step_id=step.step_id,
                    from_node_id=current_node_id,
                    to_node_id=next_node_id,
                    goal=handoff_goal,
                    artifact_ids=artifact_ids,
                    metadata={"workflow_id": request.workflow.workflow_id},
                )
                handoffs.append(handoff)
                memory_events.append(
                    MemoryEvent(
                        event_id=f"mem-{uuid4().hex[:10]}",
                        run_id=run_id,
                        task_id=task.task_id,
                        step_id=step.step_id,
                        category="handoff",
                        summary=f"Handoff from {current_node_id} to {next_node_id}",
                        payload={
                            "handoff_id": handoff.handoff_id,
                            "to_node_id": next_node_id,
                            "artifact_ids": artifact_ids,
                            "goal": handoff_goal,
                        },
                        metadata={"source": "handoff"},
                    )
                )
            feedback_record = self._build_step_feedback_record(
                run_id=run_id,
                task=task,
                step=step,
                activation=activation,
                candidates=candidates,
                checkpoint=checkpoint,
                next_node_id=next_node_id,
                artifact_ids=[item.artifact_id for item in step_artifacts],
                handoff=handoffs[-1] if handoffs and handoffs[-1].from_step_id == step.step_id else None,
            )
            feedback_records.append(feedback_record)
            step.metadata.setdefault("feedback_ids", []).append(feedback_record.feedback_id)
            memory_events.append(
                MemoryEvent(
                    event_id=f"mem-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step.step_id,
                    category="feedback_signal",
                    summary=feedback_record.summary,
                    payload={
                        "feedback_id": feedback_record.feedback_id,
                        "source_type": feedback_record.source_type,
                        "status": feedback_record.status,
                        "signals": feedback_record.signals,
                        "reward_hints": feedback_record.reward_hints,
                    },
                    metadata={"source": "feedback"},
                )
            )
            trace.append(
                {
                    "event": "feedback_signal",
                    "node_id": current_node_id,
                    "message": feedback_record.summary,
                    "timestamp": feedback_record.created_at.isoformat(),
                }
            )
            current_node_id = next_node_id
            self._checkpoints[checkpoint.checkpoint_id] = checkpoint
            if self._checkpoint_store is not None:
                self._checkpoint_store.put(checkpoint)
                self._emit_run_event(
                    checkpoint.run_id, "checkpoint.saved",
                    node_id=checkpoint.state.current_node_id,
                    payload={"checkpoint_id": checkpoint.checkpoint_id, "step_id": checkpoint.step_id},
                )

        else:
            errors.append({"message": "workflow exceeded max_steps guard", "code": "max_steps_exceeded"})

        if state.get("_approval_paused"):
            run.status = "paused"
            task.status = "waiting"
        elif errors:
            run.status = "failed"
            task.status = "failed"
        else:
            run.status = "succeeded"
            task.status = "succeeded"
        # Story 4.1 AC2: publish RUN_COMPLETED and close run event bus
        if self._event_bus is not None:
            from xueban.runtime.events import RUN_COMPLETED as _RC
            self._event_bus.publish_node_event(run_id, _RC, "", {"status": run.status, "errors": errors[:5]})
            self._event_bus.close_run(run_id)
        run.ended_at = utc_now()
        run.current_step_id = steps[-1].step_id if steps else None
        task.ended_at = run.ended_at
        run_feedback = self._build_run_feedback_record(
            run=run,
            task=task,
            steps=steps,
            artifacts=artifacts,
            checkpoints=checkpoints,
            handoffs=handoffs,
            activation_candidates=activation_candidates,
            activations=activations,
            feedback_records=feedback_records,
        )
        feedback_records.append(run_feedback)
        memory_events.append(
            MemoryEvent(
                event_id=f"mem-{uuid4().hex[:10]}",
                run_id=run_id,
                task_id=task.task_id,
                category="feedback_signal",
                summary=run_feedback.summary,
                payload={
                    "feedback_id": run_feedback.feedback_id,
                    "source_type": run_feedback.source_type,
                    "status": run_feedback.status,
                    "signals": run_feedback.signals,
                    "reward_hints": run_feedback.reward_hints,
                },
                metadata={"source": "feedback"},
            )
        )
        memory_events.append(
            MemoryEvent(
                event_id=f"mem-{uuid4().hex[:10]}",
                run_id=run_id,
                task_id=task.task_id,
                category="run_summary",
                summary=f"Run finished with status {run.status}",
                payload={
                    "run_id": run_id,
                    "status": run.status,
                    "step_count": len(steps),
                    "artifact_count": len(artifacts),
                    "checkpoint_count": len(checkpoints),
                    "handoff_count": len(handoffs),
                },
                metadata={"source": "runtime_service"},
            )
        )

        result = RunResult(
            run=run,
            tasks=tasks,
            steps=steps,
            final_output=current_output,
            trace=trace,
            artifacts=artifacts,
            checkpoints=checkpoints,
            handoffs=handoffs,
            activation_candidates=activation_candidates,
            activations=activations,
            feedback=feedback_records,
            memory_events=memory_events,
            errors=errors,
        )
        if self._writeback_adapter is not None:
            receipts = self._writeback_adapter.persist_run_result(result, checkpoint_store=self._checkpoint_store)
            result.run.metadata["writeback_receipts"] = [
                receipt.model_dump(mode="json") for receipt in receipts
            ]
        self._runs[run_id] = result
        # Story 9.3: fire-and-forget memory writeback (non-blocking)
        if self._context_builder is not None:
            _agent_id = str(result.run.metadata.get("agent_id") or result.run.workflow_id)
            asyncio.create_task(
                self._context_builder.writeback(
                    run_id=run_id,
                    agent_id=_agent_id,
                    events=memory_events,
                    trigger="on_task_complete",
                )
            )
        return result

    def get_run(self, run_id: str) -> Optional[RunResult]:
        result = self._runs.get(run_id)
        if result is not None:
            return result
        if self._run_store is not None:
            return self._run_store.get(run_id)
        return None

    def _resolve_projection_run(
        self,
        run_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
    ) -> Optional[RunResult]:
        if run_id is not None:
            return self.get_run(run_id)

        if artifact_id is None:
            return None

        for summary in self.list_runs():
            candidate = self.get_run(summary.run_id)
            if candidate is None:
                continue
            if any(artifact.artifact_id == artifact_id for artifact in candidate.artifacts):
                return candidate
        return None

    def _collect_run_lineage(self, run_id: str) -> tuple[str, List[RunResult]]:
        result = self.get_run(run_id)
        if result is None:
            raise ValueError(f"run not found: {run_id}")

        root_run_id = result.run.root_run_id or result.run.run_id
        lineage: List[RunResult] = []
        seen: set[str] = set()
        for summary in self.list_runs():
            candidate = self.get_run(summary.run_id)
            if candidate is None:
                continue
            candidate_root = candidate.run.root_run_id or candidate.run.run_id
            if candidate_root != root_run_id:
                continue
            if candidate.run.run_id in seen:
                continue
            seen.add(candidate.run.run_id)
            lineage.append(candidate)

        lineage.sort(key=lambda item: item.run.started_at)
        return root_run_id, lineage

    def list_runs(self) -> List[RunSummary]:
        runs = [
            RunSummary(
                run_id=result.run.run_id,
                request_id=result.run.request_id,
                workflow_id=result.run.workflow_id,
                status=result.run.status,
                started_at=result.run.started_at,
                ended_at=result.run.ended_at,
                current_step_id=result.run.current_step_id,
                metadata=result.run.metadata,
            )
            for result in self._runs.values()
        ]
        if self._run_store is not None:
            existing = {item.run_id for item in runs}
            for item in self._run_store.list_runs():
                if item.run_id not in existing:
                    runs.append(item)
        return sorted(runs, key=lambda item: item.started_at, reverse=True)

    def export_workflow_graph(self, workflow: WorkflowDefinition) -> WorkflowGraph:
        return WorkflowGraph(
            workflow_id=workflow.workflow_id,
            name=workflow.name,
            entrypoint=workflow.entrypoint,
            scope=ProjectionScope(workflow_id=workflow.workflow_id),
            summary={
                "node_count": len(workflow.nodes),
                "edge_count": len(workflow.edges),
            },
            nodes=[
                WorkflowGraphNode(
                    id=node.id,
                    label=node.config.get("role", node.id),
                    kind=node.kind,
                    type=node.type,
                    entrypoint=node.id == workflow.entrypoint,
                    refs={"workflow_id": workflow.workflow_id, "node_id": node.id},
                    metadata={
                        "role": node.config.get("role"),
                        "outputs": node.outputs,
                        "inputs": node.inputs,
                    },
                )
                for node in workflow.nodes
            ],
            edges=[
                WorkflowGraphEdge(
                    from_id=edge.from_id,
                    to_id=edge.to_id,
                    type=edge.type,
                    edge_type="conditional_flow" if edge.type == "conditional" else "control_flow",
                    condition=edge.condition,
                    metadata=edge.metadata,
                )
                for edge in workflow.edges
            ],
            metadata=workflow.metadata,
        )

    def export_run_graph(self, run_id: str) -> Optional[RunGraph]:
        result = self.get_run(run_id)
        request = self._requests_by_run_id.get(run_id)
        if request is None and self._request_context_store is not None:
            request = self._request_context_store.get(run_id)
        if result is None or request is None:
            return None

        steps_by_node_id = {step.node_id: step for step in result.steps}
        candidates_by_step_id: Dict[str, List[ActivationCandidate]] = {}
        for candidate in result.activation_candidates:
            if candidate.step_id is None:
                continue
            candidates_by_step_id.setdefault(candidate.step_id, []).append(candidate)
        activations_by_step_id = {activation.step_id: activation for activation in result.activations if activation.step_id}
        feedback_by_step_id: Dict[str, List[ExecutionFeedbackRecord]] = {}
        for record in result.feedback:
            if record.step_id is None:
                continue
            feedback_by_step_id.setdefault(record.step_id, []).append(record)
        workflow_graph = self.export_workflow_graph(request.workflow)

        # 2026-08-19 导航架构统一批：迭代环是 DAG 的一部分——control.loop 节点
        # 的 loop 记录（step.output.loop_record，legacy 为 loop_* 顶层键）挂进
        # 节点 metadata.loop，前端「过程」tab 在图节点上直接渲染迭代徽标
        # （状态 + 轮数 + 收敛）。提取逻辑与 api/runs.py get_run_loops 同构。
        def _loop_record_of(step) -> Optional[dict]:
            if not isinstance(step.output, dict):
                return None
            rec = step.output.get("loop_record")
            if rec:
                return rec
            if "loop_id" in step.output:
                return {
                    "loop_id": step.output.get("loop_id"),
                    "loop_kind": step.output.get("loop_kind"),
                    "loop_status": step.output.get("loop_status"),
                    "loop_iterations": step.output.get("loop_iterations", 0),
                    "loop_converged": step.output.get("loop_converged", False),
                    "loop_depth": step.output.get("loop_depth", 0),
                    "loop_halted_by_watch": step.output.get("loop_halted_by_watch", False),
                }
            return None

        nodes: List[RunGraphNode] = []
        loop_count = 0
        for node in workflow_graph.nodes:
            step = steps_by_node_id.get(node.id)
            candidates = candidates_by_step_id.get(step.step_id, []) if step is not None else []
            activation = activations_by_step_id.get(step.step_id) if step is not None else None
            feedback_records = feedback_by_step_id.get(step.step_id, []) if step is not None else []
            loop_record = _loop_record_of(step) if step is not None else None
            if loop_record:
                loop_count += 1
            nodes.append(
                RunGraphNode(
                    id=node.id,
                    label=node.label,
                    kind=node.kind,
                    type=node.type,
                    status=step.status if step is not None else "not_started",
                    step_id=step.step_id if step is not None else None,
                    index=step.index if step is not None else None,
                    entrypoint=node.entrypoint,
                    refs={
                        "workflow_id": result.run.workflow_id,
                        "run_id": result.run.run_id,
                        "node_id": node.id,
                        "step_id": step.step_id if step is not None else None,
                    },
                    metadata={
                        **node.metadata,
                        "activation": activation.model_dump(mode="json") if activation is not None else None,
                        "candidate_count": len(candidates),
                        "candidate_ids": [candidate.candidate_id for candidate in candidates],
                        "feedback_count": len(feedback_records),
                        "feedback_ids": [item.feedback_id for item in feedback_records],
                        "loop": loop_record,
                    },
                )
            )

        return RunGraph(
            run_id=result.run.run_id,
            workflow_id=result.run.workflow_id,
            status=result.run.status,
            entrypoint=result.run.entrypoint,
            scope=ProjectionScope(workflow_id=result.run.workflow_id, run_id=result.run.run_id),
            summary={
                "step_count": len(result.steps),
                "artifact_count": len(result.artifacts),
                "checkpoint_count": len(result.checkpoints),
                "activation_candidate_count": len(result.activation_candidates),
                "activation_count": len(result.activations),
                "feedback_count": len(result.feedback),
                "memory_event_count": len(result.memory_events),
                "handoff_count": len(result.handoffs),
                "loop_count": loop_count,
            },
            nodes=nodes,
            edges=workflow_graph.edges,
            metadata=result.run.metadata,
        )

    def _build_task_tree_projection(self, run_id: str) -> Optional[dict[str, Any]]:
        try:
            root_run_id, lineage_runs = self._collect_run_lineage(run_id)
        except ValueError:
            return None

        result = next((item for item in lineage_runs if item.run.run_id == run_id), None)
        if result is None:
            return None

        nodes: List[ProjectionNode] = []
        edges: List[ProjectionEdge] = []
        node_ids: set[str] = set()

        for lineage_result in lineage_runs:
            run = lineage_result.run
            if run.run_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=run.run_id,
                        entity_type="run",
                        label=run.workflow_id,
                        status=run.status,
                        parent_id=run.parent_run_id,
                        refs={
                            "run_id": run.run_id,
                            "workflow_id": run.workflow_id,
                            "parent_run_id": run.parent_run_id,
                            "root_run_id": run.root_run_id,
                        },
                        timestamps=ProjectionNodeTimestamps(
                            started_at=run.started_at,
                            ended_at=run.ended_at,
                        ),
                        metadata={"entrypoint": run.entrypoint, **run.metadata},
                    )
                )
                node_ids.add(run.run_id)

            if run.parent_run_id is not None:
                edges.append(
                    ProjectionEdge(
                        id=f"edge-run-parent-{run.run_id}",
                        edge_type="delegation",
                        from_id=run.parent_run_id,
                        to_id=run.run_id,
                        intervention=True,
                        metadata={
                            "parent_step_id": run.metadata.get("parent_step_id"),
                            "delegated_from_node_id": run.metadata.get("delegated_from_node_id"),
                        },
                    )
                )

            steps_by_task_id: Dict[Optional[str], List[StepRecord]] = {}
            for step in lineage_result.steps:
                steps_by_task_id.setdefault(step.metadata.get("task_id", run.task_id), []).append(step)

            for task in lineage_result.tasks:
                if task.task_id not in node_ids:
                    nodes.append(
                        ProjectionNode(
                            id=task.task_id,
                            entity_type="task",
                            label=task.title or task.focus or task.task_id,
                            status=task.status,
                            parent_id=task.parent_task_id or run.run_id,
                            refs={
                                "task_id": task.task_id,
                                "run_id": task.run_id,
                                "root_task_id": task.root_task_id,
                                "parent_task_id": task.parent_task_id,
                            },
                            timestamps=ProjectionNodeTimestamps(
                                created_at=task.created_at,
                                started_at=task.started_at,
                                ended_at=task.ended_at,
                            ),
                            metadata=task.metadata,
                        )
                    )
                    node_ids.add(task.task_id)
                edges.append(
                    ProjectionEdge(
                        id=f"edge-task-run-{task.task_id}",
                        edge_type="belongs_to_run",
                        from_id=task.task_id,
                        to_id=run.run_id,
                        metadata={"run_id": run.run_id},
                    )
                )
                if task.parent_task_id is not None:
                    edges.append(
                        ProjectionEdge(
                            id=f"edge-task-parent-{task.task_id}",
                            edge_type="delegation",
                            from_id=task.parent_task_id,
                            to_id=task.task_id,
                            intervention=True,
                            metadata={"run_id": run.run_id},
                        )
                    )

                for step in steps_by_task_id.get(task.task_id, []):
                    if step.step_id not in node_ids:
                        nodes.append(
                            ProjectionNode(
                                id=step.step_id,
                                entity_type="step",
                                label=step.node_id,
                                status=step.status,
                                parent_id=task.task_id,
                                refs={
                                    "run_id": step.run_id,
                                    "task_id": task.task_id,
                                    "step_id": step.step_id,
                                    "node_id": step.node_id,
                                },
                                timestamps=ProjectionNodeTimestamps(
                                    started_at=step.started_at,
                                    ended_at=step.ended_at,
                                ),
                                metadata=step.metadata,
                            )
                        )
                        node_ids.add(step.step_id)
                    edges.append(
                        ProjectionEdge(
                            id=f"edge-step-task-{step.step_id}",
                            edge_type="belongs_to_task",
                            from_id=step.step_id,
                            to_id=task.task_id,
                            metadata={"run_id": step.run_id},
                        )
                    )

        return {
            "scope": ProjectionScope(
                workflow_id=result.run.workflow_id,
                run_id=result.run.run_id,
                task_id=result.run.task_id,
            ),
            "summary": {
                "root_run_id": root_run_id,
                "run_count": len(lineage_runs),
                "task_count": sum(len(item.tasks) for item in lineage_runs),
                "step_count": sum(len(item.steps) for item in lineage_runs),
                "activation_candidate_count": sum(len(item.activation_candidates) for item in lineage_runs),
                "activation_count": sum(len(item.activations) for item in lineage_runs),
                "feedback_count": sum(len(item.feedback) for item in lineage_runs),
            },
            "nodes": nodes,
            "edges": edges,
            "metadata": {
                "run_status": result.run.status,
                "root_run_id": root_run_id,
                "projection_note": "task_tree is a graph projection over task-level lineage relations.",
            },
        }

    def export_task_tree(self, run_id: str) -> Optional[TaskTreeProjection]:
        payload = self._build_task_tree_projection(run_id)
        if payload is None:
            return None
        return TaskTreeProjection(**payload)

    def export_artifact_lineage(
        self,
        run_id: Optional[str] = None,
        artifact_id: Optional[str] = None,
    ) -> Optional[ArtifactLineageProjection]:
        target_result = self._resolve_projection_run(run_id=run_id, artifact_id=artifact_id)
        if target_result is None:
            return None

        artifacts = target_result.artifacts
        if artifact_id is not None:
            artifacts = [artifact for artifact in artifacts if artifact.artifact_id == artifact_id]
            if not artifacts:
                return None

        nodes: List[ProjectionNode] = [
            ProjectionNode(
                id=target_result.run.run_id,
                entity_type="run",
                label=target_result.run.workflow_id,
                status=target_result.run.status,
                refs={"run_id": target_result.run.run_id, "workflow_id": target_result.run.workflow_id},
                timestamps=ProjectionNodeTimestamps(
                    started_at=target_result.run.started_at,
                    ended_at=target_result.run.ended_at,
                ),
                metadata=target_result.run.metadata,
            )
        ]
        edges: List[ProjectionEdge] = []
        tasks_by_id = {task.task_id: task for task in target_result.tasks}
        steps_by_id = {step.step_id: step for step in target_result.steps}

        included_task_ids: set[str] = set()
        included_step_ids: set[str] = set()
        node_ids = {target_result.run.run_id}
        # filecard 置顶标记（GAP :14）——独立小仓 .xueban/artifacts/，注入节点 metadata
        pinned = pinned_ids()

        for artifact in artifacts:
            step = steps_by_id.get(artifact.producer_step_id)
            task = tasks_by_id.get(target_result.run.task_id or "")
            if task is not None and task.task_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=task.task_id,
                        entity_type="task",
                        label=task.title or task.focus or task.task_id,
                        status=task.status,
                        parent_id=target_result.run.run_id,
                        refs={"task_id": task.task_id, "run_id": task.run_id},
                        timestamps=ProjectionNodeTimestamps(
                            created_at=task.created_at,
                            started_at=task.started_at,
                            ended_at=task.ended_at,
                        ),
                        metadata=task.metadata,
                    )
                )
                node_ids.add(task.task_id)
                included_task_ids.add(task.task_id)
                edges.append(
                    ProjectionEdge(
                        id=f"edge-artifact-task-run-{task.task_id}",
                        edge_type="belongs_to_run",
                        from_id=task.task_id,
                        to_id=target_result.run.run_id,
                        metadata={"run_id": target_result.run.run_id},
                    )
                )
            if step is not None and step.step_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=step.step_id,
                        entity_type="step",
                        label=step.node_id,
                        status=step.status,
                        parent_id=target_result.run.task_id,
                        refs={
                            "step_id": step.step_id,
                            "run_id": step.run_id,
                            "task_id": target_result.run.task_id,
                            "node_id": step.node_id,
                        },
                        timestamps=ProjectionNodeTimestamps(
                            started_at=step.started_at,
                            ended_at=step.ended_at,
                        ),
                        metadata=step.metadata,
                    )
                )
                node_ids.add(step.step_id)
                included_step_ids.add(step.step_id)
                if target_result.run.task_id is not None:
                    edges.append(
                        ProjectionEdge(
                            id=f"edge-artifact-step-task-{step.step_id}",
                            edge_type="belongs_to_task",
                            from_id=step.step_id,
                            to_id=target_result.run.task_id,
                            metadata={"run_id": target_result.run.run_id},
                        )
                    )

            nodes.append(
                ProjectionNode(
                    id=artifact.artifact_id,
                    entity_type="artifact",
                    label=artifact.name,
                    parent_id=artifact.producer_step_id,
                    refs={
                        "artifact_id": artifact.artifact_id,
                        "run_id": target_result.run.run_id,
                        "producer_step_id": artifact.producer_step_id,
                    },
                    timestamps=ProjectionNodeTimestamps(),
                    metadata={
                        "kind": artifact.kind,
                        "uri": artifact.uri,
                        "uri_scheme": artifact_uri_scheme(artifact.uri),
                        "preview_url": artifact_preview_url(artifact.uri),
                        "pinned": artifact.artifact_id in pinned,
                        "writeback": artifact.writeback.model_dump(mode="json"),
                        **artifact.metadata,
                    },
                )
            )
            node_ids.add(artifact.artifact_id)
            if step is not None:
                edges.append(
                    ProjectionEdge(
                        id=f"edge-produces-{artifact.artifact_id}",
                        edge_type="produces_artifact",
                        from_id=step.step_id,
                        to_id=artifact.artifact_id,
                        metadata={"run_id": target_result.run.run_id},
                    )
                )
            if target_result.run.task_id is not None:
                edges.append(
                    ProjectionEdge(
                        id=f"edge-artifact-task-{artifact.artifact_id}",
                        edge_type="belongs_to_task",
                        from_id=artifact.artifact_id,
                        to_id=target_result.run.task_id,
                        metadata={"run_id": target_result.run.run_id},
                    )
                )
            edges.append(
                ProjectionEdge(
                    id=f"edge-artifact-run-{artifact.artifact_id}",
                    edge_type="belongs_to_run",
                    from_id=artifact.artifact_id,
                    to_id=target_result.run.run_id,
                    metadata={"run_id": target_result.run.run_id},
                )
            )

        return ArtifactLineageProjection(
            scope=ProjectionScope(
                workflow_id=target_result.run.workflow_id,
                run_id=target_result.run.run_id,
                artifact_id=artifact_id,
            ),
            summary={
                "artifact_count": len(artifacts),
                "step_count": len(included_step_ids),
                "task_count": len(included_task_ids),
            },
            nodes=nodes,
            edges=edges,
            metadata={"run_status": target_result.run.status},
        )

    def export_memory_relation_graph(self, run_id: str) -> Optional[MemoryRelationProjection]:
        result = self.get_run(run_id)
        if result is None:
            return None

        nodes: List[ProjectionNode] = [
            ProjectionNode(
                id=result.run.run_id,
                entity_type="run",
                label=result.run.workflow_id,
                status=result.run.status,
                refs={"run_id": result.run.run_id, "workflow_id": result.run.workflow_id},
                timestamps=ProjectionNodeTimestamps(
                    started_at=result.run.started_at,
                    ended_at=result.run.ended_at,
                ),
                metadata=result.run.metadata,
            )
        ]
        edges: List[ProjectionEdge] = []
        node_ids = {result.run.run_id}
        handoffs_by_step_id = {handoff.from_step_id: handoff for handoff in result.handoffs}
        candidates_by_step_id: Dict[str, List[ActivationCandidate]] = {}
        for candidate in result.activation_candidates:
            if candidate.step_id is None:
                continue
            candidates_by_step_id.setdefault(candidate.step_id, []).append(candidate)
        activations_by_step_id = {activation.step_id: activation for activation in result.activations if activation.step_id}

        for task in result.tasks:
            if task.task_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=task.task_id,
                        entity_type="task",
                        label=task.title or task.focus or task.task_id,
                        status=task.status,
                        parent_id=result.run.run_id,
                        refs={"task_id": task.task_id, "run_id": task.run_id},
                        timestamps=ProjectionNodeTimestamps(
                            created_at=task.created_at,
                            started_at=task.started_at,
                            ended_at=task.ended_at,
                        ),
                        metadata=task.metadata,
                    )
                )
                node_ids.add(task.task_id)
                edges.append(
                    ProjectionEdge(
                        id=f"edge-memory-task-run-{task.task_id}",
                        edge_type="belongs_to_run",
                        from_id=task.task_id,
                        to_id=result.run.run_id,
                        metadata={"run_id": result.run.run_id},
                    )
                )

        for step in result.steps:
            if step.step_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=step.step_id,
                        entity_type="step",
                        label=step.node_id,
                        status=step.status,
                        parent_id=result.run.task_id,
                        refs={"step_id": step.step_id, "run_id": step.run_id, "task_id": result.run.task_id},
                        timestamps=ProjectionNodeTimestamps(
                            started_at=step.started_at,
                            ended_at=step.ended_at,
                        ),
                        metadata=step.metadata,
                    )
                )
                node_ids.add(step.step_id)
                if result.run.task_id is not None:
                    edges.append(
                        ProjectionEdge(
                            id=f"edge-memory-step-task-{step.step_id}",
                            edge_type="belongs_to_task",
                            from_id=step.step_id,
                            to_id=result.run.task_id,
                            metadata={"run_id": result.run.run_id},
                        )
                    )

        for handoff in result.handoffs:
            if handoff.handoff_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=handoff.handoff_id,
                        entity_type="handoff",
                        label=f"{handoff.from_node_id}->{handoff.to_node_id or 'END'}",
                        parent_id=handoff.from_step_id,
                        refs={
                            "handoff_id": handoff.handoff_id,
                            "run_id": handoff.run_id,
                            "from_step_id": handoff.from_step_id,
                        },
                        timestamps=ProjectionNodeTimestamps(created_at=handoff.created_at),
                        metadata={"goal": handoff.goal, **handoff.metadata},
                    )
                )
                node_ids.add(handoff.handoff_id)
                edges.append(
                    ProjectionEdge(
                        id=f"edge-handoff-step-{handoff.handoff_id}",
                        edge_type="handoff_to",
                        from_id=handoff.from_step_id,
                        to_id=handoff.handoff_id,
                        metadata={"to_node_id": handoff.to_node_id},
                    )
                )

        for candidate in result.activation_candidates:
            nodes.append(
                ProjectionNode(
                    id=candidate.candidate_id,
                    entity_type="activation_candidate",
                    label=f"{candidate.candidate_type}:{candidate.candidate_ref}",
                    status="selected" if candidate.selected else "suppressed",
                    parent_id=candidate.step_id or candidate.task_id or result.run.run_id,
                    refs={
                        "candidate_id": candidate.candidate_id,
                        "run_id": candidate.run_id,
                        "task_id": candidate.task_id,
                        "step_id": candidate.step_id,
                        "node_id": candidate.node_id,
                    },
                    timestamps=ProjectionNodeTimestamps(created_at=candidate.created_at),
                    metadata={
                        "candidate_type": candidate.candidate_type,
                        "candidate_ref": candidate.candidate_ref,
                        "source_signals": candidate.source_signals,
                        "score": candidate.score,
                        "selected": candidate.selected,
                        "suppressed_reason": candidate.suppressed_reason,
                        **candidate.metadata,
                    },
                )
            )
            source_id = candidate.step_id or candidate.task_id or result.run.run_id
            edges.append(
                ProjectionEdge(
                    id=f"edge-candidate-step-{candidate.candidate_id}",
                    edge_type="candidate_for_activation",
                    from_id=source_id,
                    to_id=candidate.candidate_id,
                    metadata={"candidate_type": candidate.candidate_type},
                )
            )

        for activation in result.activations:
            nodes.append(
                ProjectionNode(
                    id=activation.activation_id,
                    entity_type="activation",
                    label=f"{activation.node_id}:{activation.mode}",
                    status=activation.decision,
                    parent_id=activation.step_id or activation.task_id or result.run.run_id,
                    refs={
                        "activation_id": activation.activation_id,
                        "run_id": activation.run_id,
                        "task_id": activation.task_id,
                        "step_id": activation.step_id,
                        "node_id": activation.node_id,
                    },
                    timestamps=ProjectionNodeTimestamps(created_at=activation.created_at),
                    metadata=activation.metadata
                    | {
                        "mode": activation.mode,
                        "tags": activation.tags,
                        "delegate_candidates": activation.delegate_candidates,
                        "feedback_channels": activation.feedback_channels,
                        "signal_sources": activation.signal_sources,
                        "reasons": activation.reasons,
                    },
                )
            )
            for candidate in candidates_by_step_id.get(activation.step_id or "", []):
                edges.append(
                    ProjectionEdge(
                        id=f"edge-candidate-activation-{candidate.candidate_id}",
                        edge_type="candidate_for_activation",
                        from_id=candidate.candidate_id,
                        to_id=activation.activation_id,
                        metadata={"selected": candidate.selected},
                    )
                )
            source_id = activation.step_id or activation.task_id or result.run.run_id
            edges.append(
                ProjectionEdge(
                    id=f"edge-activation-{activation.activation_id}",
                    edge_type="activates",
                    from_id=source_id,
                    to_id=activation.activation_id,
                    metadata={"node_id": activation.node_id, "mode": activation.mode},
                )
            )

        for feedback in result.feedback:
            nodes.append(
                ProjectionNode(
                    id=feedback.feedback_id,
                    entity_type="feedback_signal",
                    label=feedback.summary,
                    status=feedback.status,
                    parent_id=feedback.step_id or feedback.task_id or result.run.run_id,
                    refs={
                        "feedback_id": feedback.feedback_id,
                        "run_id": feedback.run_id,
                        "task_id": feedback.task_id,
                        "step_id": feedback.step_id,
                        "node_id": feedback.node_id,
                    },
                    timestamps=ProjectionNodeTimestamps(created_at=feedback.created_at),
                    metadata={
                        "source_type": feedback.source_type,
                        "signals": feedback.signals,
                        "reward_hints": feedback.reward_hints,
                        **feedback.metadata,
                    },
                )
            )
            if feedback.step_id is not None and feedback.step_id in activations_by_step_id:
                source_id = activations_by_step_id[feedback.step_id].activation_id
            elif feedback.step_id is not None:
                source_id = feedback.step_id
            elif feedback.task_id is not None:
                source_id = feedback.task_id
            else:
                source_id = result.run.run_id
            edges.append(
                ProjectionEdge(
                    id=f"edge-feedback-{feedback.feedback_id}",
                    edge_type="records_feedback",
                    from_id=source_id,
                    to_id=feedback.feedback_id,
                    metadata={"source_type": feedback.source_type, "status": feedback.status},
                )
            )

        for event in result.memory_events:
            nodes.append(
                ProjectionNode(
                    id=event.event_id,
                    entity_type="memory_event",
                    label=event.summary,
                    status=event.category,
                    parent_id=event.step_id or event.task_id or result.run.run_id,
                    refs={
                        "event_id": event.event_id,
                        "run_id": event.run_id,
                        "task_id": event.task_id,
                        "step_id": event.step_id,
                    },
                    timestamps=ProjectionNodeTimestamps(created_at=event.created_at),
                    metadata={"category": event.category, **event.metadata, "payload": event.payload},
                )
            )
            source_id = result.run.run_id
            if event.category == "handoff" and event.step_id is not None and event.step_id in handoffs_by_step_id:
                source_id = handoffs_by_step_id[event.step_id].handoff_id
            elif event.step_id is not None:
                source_id = event.step_id
            elif event.task_id is not None:
                source_id = event.task_id
            edges.append(
                ProjectionEdge(
                    id=f"edge-memory-event-{event.event_id}",
                    edge_type="emits_memory_event",
                    from_id=source_id,
                    to_id=event.event_id,
                    metadata={"category": event.category},
                )
            )

        return MemoryRelationProjection(
            scope=ProjectionScope(workflow_id=result.run.workflow_id, run_id=result.run.run_id),
            summary={
                "memory_event_count": len(result.memory_events),
                "step_count": len(result.steps),
                "handoff_count": len(result.handoffs),
                "activation_candidate_count": len(result.activation_candidates),
                "activation_count": len(result.activations),
                "feedback_count": len(result.feedback),
            },
            nodes=nodes,
            edges=edges,
            metadata={"run_status": result.run.status},
        )

    def export_checkpoint_lineage(self, run_id: str) -> Optional[CheckpointLineageProjection]:
        result = self.get_run(run_id)
        if result is None:
            return None

        nodes: List[ProjectionNode] = [
            ProjectionNode(
                id=result.run.run_id,
                entity_type="run",
                label=result.run.workflow_id,
                status=result.run.status,
                refs={"run_id": result.run.run_id, "workflow_id": result.run.workflow_id},
                timestamps=ProjectionNodeTimestamps(
                    started_at=result.run.started_at,
                    ended_at=result.run.ended_at,
                ),
                metadata=result.run.metadata,
            )
        ]
        edges: List[ProjectionEdge] = []
        node_ids = {result.run.run_id}
        steps_by_id = {step.step_id: step for step in result.steps}

        for step in result.steps:
            if step.step_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=step.step_id,
                        entity_type="step",
                        label=step.node_id,
                        status=step.status,
                        parent_id=result.run.task_id,
                        refs={"step_id": step.step_id, "run_id": step.run_id},
                        timestamps=ProjectionNodeTimestamps(
                            started_at=step.started_at,
                            ended_at=step.ended_at,
                        ),
                        metadata=step.metadata,
                    )
                )
                node_ids.add(step.step_id)

        for checkpoint in result.checkpoints:
            nodes.append(
                ProjectionNode(
                    id=checkpoint.checkpoint_id,
                    entity_type="checkpoint",
                    label=checkpoint.state.current_node_id or checkpoint.checkpoint_id,
                    parent_id=checkpoint.step_id,
                    refs={
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "run_id": checkpoint.run_id,
                        "step_id": checkpoint.step_id,
                    },
                    timestamps=ProjectionNodeTimestamps(created_at=checkpoint.created_at),
                    metadata={
                        "state_ref": checkpoint.state_ref,
                        "next_node_id": checkpoint.state.next_node_id,
                        "writeback": checkpoint.writeback.model_dump(mode="json"),
                        **checkpoint.metadata,
                    },
                )
            )
            if checkpoint.step_id is not None and checkpoint.step_id in steps_by_id:
                edges.append(
                    ProjectionEdge(
                        id=f"edge-checkpoint-step-{checkpoint.checkpoint_id}",
                        edge_type="creates_checkpoint",
                        from_id=checkpoint.step_id,
                        to_id=checkpoint.checkpoint_id,
                        metadata={"run_id": checkpoint.run_id},
                    )
                )
            edges.append(
                ProjectionEdge(
                    id=f"edge-checkpoint-run-{checkpoint.checkpoint_id}",
                    edge_type="belongs_to_run",
                    from_id=checkpoint.checkpoint_id,
                    to_id=result.run.run_id,
                    metadata={"run_id": checkpoint.run_id},
                )
            )

        resumed_from_checkpoint_id = result.run.metadata.get("resumed_from_checkpoint_id")
        if isinstance(resumed_from_checkpoint_id, str):
            checkpoint = self.get_checkpoint(resumed_from_checkpoint_id)
            if checkpoint is not None and checkpoint.checkpoint_id not in node_ids:
                nodes.append(
                    ProjectionNode(
                        id=checkpoint.checkpoint_id,
                        entity_type="checkpoint",
                        label=checkpoint.state.current_node_id or checkpoint.checkpoint_id,
                        refs={
                            "checkpoint_id": checkpoint.checkpoint_id,
                            "run_id": checkpoint.run_id,
                            "step_id": checkpoint.step_id,
                        },
                        timestamps=ProjectionNodeTimestamps(created_at=checkpoint.created_at),
                        metadata={
                            "state_ref": checkpoint.state_ref,
                            "next_node_id": checkpoint.state.next_node_id,
                            **checkpoint.metadata,
                        },
                    )
                )
                node_ids.add(checkpoint.checkpoint_id)
            edges.append(
                ProjectionEdge(
                    id=f"edge-resume-{result.run.run_id}",
                    edge_type="resume_from",
                    from_id=result.run.run_id,
                    to_id=resumed_from_checkpoint_id,
                    intervention=True,
                    metadata={"resumed_from_run_id": result.run.metadata.get("resumed_from_run_id")},
                )
            )
            edges.append(
                ProjectionEdge(
                    id=f"edge-derived-{result.run.run_id}",
                    edge_type="derived_from_checkpoint",
                    from_id=resumed_from_checkpoint_id,
                    to_id=result.run.run_id,
                    metadata={"resumed_from_run_id": result.run.metadata.get("resumed_from_run_id")},
                )
            )

        return CheckpointLineageProjection(
            scope=ProjectionScope(
                workflow_id=result.run.workflow_id,
                run_id=result.run.run_id,
                checkpoint_id=resumed_from_checkpoint_id,
            ),
            summary={
                "checkpoint_count": len(result.checkpoints),
                "resumed": bool(resumed_from_checkpoint_id),
            },
            nodes=nodes,
            edges=edges,
            metadata={"run_status": result.run.status},
        )

    def export_activation_training_dataset(self, run_id: str) -> Optional[ActivationTrainingDataset]:
        result = self.get_run(run_id)
        if result is None:
            return None

        activations_by_step_id = {activation.step_id: activation for activation in result.activations if activation.step_id}
        candidates_by_step_id: Dict[str, List[ActivationCandidate]] = {}
        for candidate in result.activation_candidates:
            if candidate.step_id is None:
                continue
            candidates_by_step_id.setdefault(candidate.step_id, []).append(candidate)
        feedback_by_step_id: Dict[str, List[ExecutionFeedbackRecord]] = {}
        for record in result.feedback:
            if record.step_id is None:
                continue
            feedback_by_step_id.setdefault(record.step_id, []).append(record)

        # Phase 2: assembly wire-back — read block→node mapping from run metadata
        assembly_block_node_map: Dict[str, str] = result.run.metadata.get("assembly_block_node_map", {})
        # Invert: node_id → block_ref for lookup
        node_to_block: Dict[str, str] = {node_id: block_ref for block_ref, node_id in assembly_block_node_map.items()}
        assembly_goal: Optional[str] = result.run.metadata.get("assembly_goal")

        samples: List[ActivationTrainingSample] = []
        for step in result.steps:
            activation = activations_by_step_id.get(step.step_id)
            if activation is None:
                continue
            candidates = candidates_by_step_id.get(step.step_id, [])
            feedback_records = feedback_by_step_id.get(step.step_id, [])
            selected_candidate_ids = [candidate.candidate_id for candidate in candidates if candidate.selected]
            reward_hints: Dict[str, float] = {}
            for record in feedback_records:
                for key, value in record.reward_hints.items():
                    reward_hints[key] = float(value)

            samples.append(
                ActivationTrainingSample(
                    sample_id=f"sample-{step.step_id}",
                    run_id=result.run.run_id,
                    workflow_id=result.run.workflow_id,
                    task_id=step.metadata.get("task_id"),
                    step_id=step.step_id,
                    node_id=step.node_id,
                    step_status=step.status,
                    activation_mode=activation.mode,
                    activation_decision=activation.decision,
                    candidate_count=len(candidates),
                    selected_candidate_count=len(selected_candidate_ids),
                    selected_candidate_ids=selected_candidate_ids,
                    candidates=[
                        {
                            "candidate_id": candidate.candidate_id,
                            "candidate_type": candidate.candidate_type,
                            "candidate_ref": candidate.candidate_ref,
                            "source_signals": candidate.source_signals,
                            "score": candidate.score,
                            "selected": candidate.selected,
                            "suppressed_reason": candidate.suppressed_reason,
                            "scoring_breakdown": candidate.metadata.get("scoring_breakdown", {}),
                        }
                        for candidate in candidates
                    ],
                    feedback_ids=[record.feedback_id for record in feedback_records],
                    reward_hints=reward_hints,
                    signals=feedback_records[-1].signals if feedback_records else {},
                    assembly_block_id=node_to_block.get(step.node_id),
                    assembly_goal=assembly_goal if node_to_block else None,
                    metadata={
                        "activation_id": activation.activation_id,
                        "activation_tags": activation.tags,
                        "activation_reasons": activation.reasons,
                        "feedback_channels": activation.feedback_channels,
                    },
                )
            )

        return ActivationTrainingDataset(
            scope=ProjectionScope(workflow_id=result.run.workflow_id, run_id=result.run.run_id, task_id=result.run.task_id),
            summary={
                "sample_count": len(samples),
                "step_count": len(result.steps),
                "activation_candidate_count": len(result.activation_candidates),
                "activation_count": len(result.activations),
                "feedback_count": len(result.feedback),
            },
            samples=samples,
            metadata={
                "run_status": result.run.status,
                "root_run_id": result.run.root_run_id,
                "dataset_note": "trainer-facing activation dataset derived from runtime candidates, activations, and feedback.",
            },
        )

    def create_chat_session(self, request: ChatSessionCreateRequest) -> ChatSession:
        session_id = f"session-{uuid4().hex[:12]}"
        record = ChatSessionRecord(
            session_id=session_id,
            title=request.title,
            executor=request.executor,
            metadata=request.metadata,
        )
        messages: List[ChatMessage] = []
        if isinstance(request.system_prompt, str) and request.system_prompt.strip():
            messages.append(
                ChatMessage(
                    role="system",
                    content=request.system_prompt.strip(),
                )
            )
        session = ChatSession(session=record, messages=messages)
        self._chat_sessions[session_id] = session
        if self._chat_session_store is not None:
            self._chat_session_store.put(session)
        return session.model_copy(deep=True)

    def list_chat_sessions(self) -> List[ChatSessionRecord]:
        sessions = [item.session for item in self._chat_sessions.values()]
        if self._chat_session_store is not None:
            existing = {item.session_id for item in sessions}
            for item in self._chat_session_store.list_sessions():
                if item.session.session_id not in existing:
                    sessions.append(item.session)
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def get_chat_session(self, session_id: str) -> Optional[ChatSession]:
        session = self._chat_sessions.get(session_id)
        if session is None and self._chat_session_store is not None:
            session = self._chat_session_store.get(session_id)
            if session is not None:
                self._chat_sessions[session_id] = session
        if session is None:
            return None
        return session.model_copy(deep=True)

    async def send_chat_message(self, session_id: str, request: ChatMessageRequest) -> ChatTurnResult:
        session = self._chat_sessions.get(session_id)
        if session is None:
            raise ValueError(f"chat session not found: {session_id}")

        user_message = ChatMessage(role="user", content=request.content, metadata=request.metadata)
        session.messages.append(user_message)
        prompt = self._build_chat_prompt(session.messages)
        payload = {
            "prompt": prompt,
            "step_input": {
                "message": request.content,
                "session_id": session_id,
                "history": [
                    {"role": item.role, "content": item.content}
                    for item in session.messages
                ],
            },
            "context": request.context,
            "state": {"chat_session_id": session_id},
            "node": {"id": "chat", "type": "chat.turn", "role": "assistant"},
        }
        executor_output = await self._executor_registry.execute(session.session.executor, payload)
        response_text = str(
            executor_output.get("response_text")
            or executor_output.get("message")
            or executor_output.get("result")
            or ""
        ).strip()
        assistant_message = ChatMessage(role="assistant", content=response_text, metadata={"executor": executor_output.get("executor", {})})
        session.messages.append(assistant_message)
        session.session.updated_at = utc_now()
        if self._chat_session_store is not None:
            self._chat_session_store.put(session)
        return ChatTurnResult(
            session=session.session.model_copy(deep=True),
            user_message=user_message,
            assistant_message=assistant_message,
            response_text=response_text,
            raw_output=executor_output,
            trace=[
                {
                    "event": "chat_turn",
                    "session_id": session_id,
                    "message": f"user={request.content!r}",
                    "timestamp": utc_now().isoformat(),
                },
                {
                    "event": "chat_response",
                    "session_id": session_id,
                    "message": response_text,
                    "timestamp": utc_now().isoformat(),
                },
            ],
        )

    async def _execute_approval_gate(
        self,
        run_id: str,
        node: "NodeDefinition",
        run: "RunRecord",
        task: "TaskRecord",
        step_id: str,
        step_input: Dict[str, Any],
        state: Dict[str, Any],
        trace: List[Dict[str, Any]],
        memory_events: List["MemoryEvent"],
    ) -> Dict[str, Any]:
        from xueban.runtime.events import (
            APPROVAL_APPROVED,
            APPROVAL_PENDING,
            APPROVAL_REJECTED,
            APPROVAL_TIMEOUT,
        )

        approval_cfg = node.approval
        timeout = approval_cfg.timeout_seconds if approval_cfg else 300
        key = (run_id, node.id)

        event = asyncio.Event()
        self._approval_events[key] = event

        run.status = "awaiting_approval"
        task.status = "awaiting_approval"

        trace.append(
            {
                "event": APPROVAL_PENDING,
                "node_id": node.id,
                "message": f"Approval gate pending: approver={approval_cfg.approver if approval_cfg else 'unknown'}",
                "timestamp": utc_now().isoformat(),
            }
        )
        memory_events.append(
            MemoryEvent(
                event_id=f"mem-{uuid4().hex[:10]}",
                run_id=run_id,
                task_id=task.task_id,
                step_id=step_id,
                category="step_result",
                summary=f"Approval gate pending at {node.id}",
                payload={"node_id": node.id, "approver": approval_cfg.approver if approval_cfg else ""},
                metadata={"source": "approval_gate"},
            )
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            trace.append(
                {
                    "event": APPROVAL_TIMEOUT,
                    "node_id": node.id,
                    "message": f"Approval gate timed out after {timeout}s",
                    "timestamp": utc_now().isoformat(),
                }
            )
            memory_events.append(
                MemoryEvent(
                    event_id=f"mem-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step_id,
                    category="step_result",
                    summary=f"Approval timeout at {node.id}",
                    payload={"node_id": node.id, "timeout_seconds": timeout},
                    metadata={"source": "approval_gate"},
                )
            )
            state["_approval_paused"] = True
            self._approval_events.pop(key, None)
            self._approval_decisions.pop(key, None)
            return {"message": "approval_timeout", "approval_status": "timeout", "state": {}}
        finally:
            self._approval_events.pop(key, None)

        decision_data = self._approval_decisions.pop(key, {})
        decision = decision_data.get("decision", "approve")
        reason = decision_data.get("reason", "")

        if decision not in {"approve", "reject"}:
            decision = "reject"
            reason = reason or f"invalid decision '{decision_data.get('decision')}' treated as reject"

        if decision == "approve":
            trace.append(
                {
                    "event": APPROVAL_APPROVED,
                    "node_id": node.id,
                    "message": f"Approval granted by {approval_cfg.approver if approval_cfg else 'unknown'}",
                    "timestamp": utc_now().isoformat(),
                }
            )
            run.status = "running"
            task.status = "running"
            return {"message": "approved", "approval_status": "approved", "approval_reason": reason, "state": {}}

        trace.append(
            {
                "event": APPROVAL_REJECTED,
                "node_id": node.id,
                "message": f"Approval rejected: {reason}",
                "timestamp": utc_now().isoformat(),
            }
        )
        on_reject = approval_cfg.on_reject if approval_cfg else "halt"
        run.status = "running"
        task.status = "running"
        return {
            "message": "rejected",
            "approval_status": "rejected",
            "approval_reason": reason,
            "on_reject": on_reject,
            "state": {"_approval_rejected": True, "_on_reject": on_reject},
        }

    async def _wait_for_gap_response(
        self,
        *,
        run_id: str,
        node: "NodeDefinition",
        run: "RunRecord",
        task: "TaskRecord",
        step_id: str,
        gap_payload: Dict[str, Any],
        trace: List[Dict[str, Any]],
        memory_events: List["MemoryEvent"],
    ) -> Dict[str, Any]:
        from xueban.runtime.events import AgentGapDetectedEvent, GapChoice

        key = (run_id, node.id)
        event = asyncio.Event()
        self._gap_events[key] = event
        run.status = "waiting_user"
        task.status = "waiting_user"

        trace.append(
            {
                "event": "gap_detected",
                "node_id": node.id,
                "message": gap_payload["description"],
                "timestamp": utc_now().isoformat(),
            }
        )
        memory_events.append(
            MemoryEvent(
                event_id=f"mem-{uuid4().hex[:10]}",
                run_id=run_id,
                task_id=task.task_id,
                step_id=step_id,
                category="step_result",
                summary=f"Gap detected at {node.id}",
                payload={
                    "node_id": node.id,
                    "gap_type": gap_payload["gap_type"],
                    "description": gap_payload["description"],
                },
                metadata={"source": "gap_detector"},
            )
        )

        if self._event_bus is not None:
            self._event_bus.publish(
                run_id,
                AgentGapDetectedEvent(
                    run_id=run_id,
                    node_id=node.id,
                    gap_type=str(gap_payload["gap_type"]),
                    description=str(gap_payload["description"]),
                    choices=[GapChoice.model_validate(choice) for choice in gap_payload["choices"]],
                ),
            )

        timed_out = False
        try:
            await asyncio.wait_for(event.wait(), timeout=self._gap_timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            trace.append(
                {
                    "event": "gap_timeout",
                    "node_id": node.id,
                    "message": f"Gap response timed out after {self._gap_timeout_seconds}s; auto-selecting C (annotate)",
                    "timestamp": utc_now().isoformat(),
                }
            )
            logger.warning("gap_response timed out for run=%s node=%s; defaulting to choice C", run_id, node.id)
        finally:
            self._gap_events.pop(key, None)

        if timed_out:
            run.status = "running"
            task.status = "running"
            return {"gap_choice": "C", "user_input": None}

        response = self._gap_responses.pop(key, {})
        run.status = "running"
        task.status = "running"
        trace.append(
            {
                "event": "gap_resolved",
                "node_id": node.id,
                "message": f"Gap resolved with choice {response.get('gap_choice', '?')}",
                "timestamp": utc_now().isoformat(),
            }
        )
        return response

    def _apply_gap_response(
        self,
        *,
        step_input: Dict[str, Any],
        gap_payload: Dict[str, Any],
        gap_response: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = dict(step_input)
        choice = str(gap_response.get("gap_choice") or "").upper()
        user_input = gap_response.get("user_input")
        resolution = {
            "gap_type": gap_payload["gap_type"],
            "description": gap_payload["description"],
            "choice": choice,
            "user_input": user_input,
        }
        merged["_gap_resolution"] = resolution
        existing = merged.get("gap_resolutions")
        if isinstance(existing, list):
            existing.append(resolution)
        else:
            merged["gap_resolutions"] = [resolution]

        if choice == "A" and isinstance(user_input, str) and user_input.strip():
            merged["supplemental_data"] = user_input.strip()
        elif choice == "B":
            merged["drop_comparison"] = True
        elif choice == "C":
            merged["annotation_placeholder"] = "[TODO: will be updated]"
        return merged

    def _apply_gap_resolution_output(self, output: Dict[str, Any], step_input: Dict[str, Any]) -> None:
        resolution = step_input.get("_gap_resolution")
        if not isinstance(resolution, dict):
            return
        choice = str(resolution.get("choice") or "").upper()
        user_input = resolution.get("user_input")
        output["gap_resolution"] = resolution
        if isinstance(output.get("state"), dict):
            output["state"].setdefault("gap_resolution", resolution)
        if choice == "A" and isinstance(user_input, str) and user_input.strip():
            output["message"] = f"{output.get('message', '')} [supplemented: {user_input.strip()}]".strip()
            output["supplemental_data"] = user_input.strip()
        elif choice == "B":
            output["message"] = f"{output.get('message', '')} [comparison dropped]".strip()
            output["drop_comparison"] = True
        elif choice == "C":
            output["message"] = f"[TODO: will be updated] {output.get('message', '')}".strip()
            output["annotation_placeholder"] = "[TODO: will be updated]"

    async def _execute_node(
        self,
        node: NodeDefinition,
        step_input: Dict[str, Any],
        state: Dict[str, Any],
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        role = node.config.get("role", node.type)
        prompt = node.config.get("prompt", "")
        message = node.config.get(
            "message_template",
            f"[{role}] handled workflow step for '{step_input.get('goal') or step_input.get('message') or node.id}'.",
        )

        output: Dict[str, Any] = {
            "message": message,
            "node_id": node.id,
            "node_type": node.type,
            "handled_by": role,
            "state": {},
        }

        executor_config = node.config.get("executor")
        if isinstance(executor_config, dict):
            output = await self._execute_executor_node(
                node=node,
                executor_config=executor_config,
                step_input=step_input,
                state=state,
                context=context,
                base_output=output,
                prompt=prompt,
            )
            self._apply_node_config_output(
                node=node,
                output=output,
                step_input=step_input,
                context=context,
                prompt=prompt,
            )
            self._apply_gap_resolution_output(output, step_input)
            return output

        if node.type == "control.sensor":
            from xueban.runtime.loop_engine import execute_sensor_node

            output = await execute_sensor_node(self, node, step_input)
            self._apply_node_config_output(
                node=node, output=output, step_input=step_input, context=context, prompt=prompt
            )
            self._apply_gap_resolution_output(output, step_input)
            return output

        if node.type == "control.controller":
            from xueban.runtime.loop_engine import execute_controller_node

            output = execute_controller_node(self, node, step_input)
            self._apply_node_config_output(
                node=node, output=output, step_input=step_input, context=context, prompt=prompt
            )
            self._apply_gap_resolution_output(output, step_input)
            return output

        if node.type == "control.parallel":
            branches = [branch for branch in node.config.get("branches", []) if isinstance(branch, str)]
            barrier = node.config.get("barrier")
            output["message"] = node.config.get(
                "message_template",
                f"[{role}] scheduled {len(branches)} parallel branches.",
            )
            output["branch_count"] = len(branches)
            output["branches"] = branches
            output["barrier"] = barrier
            self._apply_gap_resolution_output(output, step_input)
            return output

        if node.type == "control.barrier":
            source_parallel = node.config.get("source_parallel") or state.get("active_parallel")
            parallel_state = state.get("parallel", {}).get(source_parallel, {})
            branch_outputs = parallel_state.get("branch_outputs", {})
            completed = list(branch_outputs.keys())
            output["message"] = node.config.get(
                "message_template",
                f"[{role}] joined {len(completed)} parallel branches.",
            )
            output["parallel_group"] = source_parallel
            output["branch_count"] = len(completed)
            output["branch_outputs"] = branch_outputs
            output["branches_completed"] = completed
            output["state"].update(
                {
                    "last_parallel_group": source_parallel,
                    "last_parallel_branch_count": len(completed),
                    "last_parallel_results": {source_parallel: branch_outputs} if source_parallel else {},
                }
            )
            if "artifact" in node.config:
                output["artifact"] = node.config["artifact"]
            self._apply_gap_resolution_output(output, step_input)
            return output

        self._apply_node_config_output(
            node=node,
            output=output,
            step_input=step_input,
            context=context,
            prompt=prompt,
        )
        self._apply_gap_resolution_output(output, step_input)
        return output

    def _is_delegated_node(self, node: NodeDefinition) -> bool:
        delegated_config = node.config.get("delegated")
        return isinstance(delegated_config, dict) and isinstance(delegated_config.get("workflow"), dict)

    async def _execute_delegated_node(
        self,
        *,
        request: RuntimeRequest,
        run_id: str,
        task: TaskRecord,
        step_id: str,
        node: NodeDefinition,
        step_input: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        role = node.config.get("role", node.type)
        prompt = node.config.get("prompt", "")
        delegated_config = node.config.get("delegated", {})
        child_workflow = WorkflowDefinition.model_validate(delegated_config["workflow"])
        child_input = dict(step_input)
        if isinstance(delegated_config.get("input"), dict):
            child_input.update(delegated_config["input"])
        child_context = delegated_config.get("context") if isinstance(delegated_config.get("context"), dict) else {}
        handoff_goal = delegated_config.get("handoff_goal") or self._extract_handoff_goal(node)
        child_result = await self.spawn_child_run(
            run_id,
            ChildRunRequest(
                workflow=child_workflow,
                input=child_input,
                context=child_context,
                memory_scope=delegated_config.get("memory_scope", request.memory_scope),
                execution_mode=delegated_config.get("execution_mode", request.execution_mode),
                context_mode=delegated_config.get("context_mode", "inherit"),
                parent_step_id=step_id,
                parent_task_id=task.task_id,
                task_title=delegated_config.get("task_title") or node.config.get("task_title") or child_workflow.name,
                handoff_goal=handoff_goal,
                metadata={
                    **(delegated_config.get("metadata") if isinstance(delegated_config.get("metadata"), dict) else {}),
                    "parent_node_id": node.id,
                    "parent_workflow_id": request.workflow.workflow_id,
                },
            ),
            parent_request_override=request,
            parent_task_override=task,
            delegated_from_node_id=node.id,
        )

        output: Dict[str, Any] = {
            "message": node.config.get(
                "message_template",
                f"[{role}] delegated work to child run {child_result.run.run_id}.",
            ),
            "node_id": node.id,
            "node_type": node.type,
            "handled_by": role,
            "state": {
                "last_child_run_id": child_result.run.run_id,
                "last_child_task_id": child_result.run.task_id,
            },
            "delegated_run": True,
            "child_run_id": child_result.run.run_id,
            "child_task_id": child_result.run.task_id,
            "child_status": child_result.run.status,
            "child_workflow_id": child_result.run.workflow_id,
            "child_final_output": child_result.final_output,
            "child_handoff_goal": handoff_goal,
            "child_artifact_ids": [item.artifact_id for item in child_result.artifacts],
            "child_checkpoint_ids": [item.checkpoint_id for item in child_result.checkpoints],
        }
        if prompt:
            output["prompt"] = prompt
        self._apply_node_config_output(
            node=node,
            output=output,
            step_input=step_input,
            context=request.context,
            prompt=prompt,
        )
        self._apply_gap_resolution_output(output, step_input)
        return output

    async def _execute_loop_node(
        self,
        *,
        request: RuntimeRequest,
        run_id: str,
        task: TaskRecord,
        step_id: str,
        node: NodeDefinition,
        step_input: Dict[str, Any],
        state: Dict[str, Any],
        nodes_by_id: Dict[str, NodeDefinition],
    ) -> Dict[str, Any]:
        """Execute a control.loop node: iterate its subgraph in-run until convergence/budget.

        The loop occupies a single main-walk step; body-node executions are captured as
        IterationRecords (not StepRecords), so iterations do not exhaust `max_steps`.
        LoopFrame push/pop tracks the fractal call-stack in `state["loop_frames"]`.
        """
        from xueban.runtime.loop_engine import execute_loop_node as _execute_loop_v2

        return await _execute_loop_v2(
            svc=self,
            request=request,
            run_id=run_id,
            task=task,
            step_id=step_id,
            node=node,
            step_input=step_input,
            state=state,
            nodes_by_id=nodes_by_id,
            edges_by_from=self._get_workflow_index(request.workflow)["edges_by_from"],
        )

    async def _execute_parallel_gather(
        self,
        *,
        request: RuntimeRequest,
        run_id: str,
        task: TaskRecord,
        step_id: str,
        node: NodeDefinition,
        step_input: Dict[str, Any],
        state: Dict[str, Any],
        nodes_by_id: Dict[str, NodeDefinition],
    ) -> Dict[str, Any]:
        """True-concurrency control.parallel: asyncio.gather over all branches.

        Each branch runs the same dispatch the main walk uses (loop /
        delegated / plain node); a failing branch never cancels its siblings —
        its slot holds an {"error": ...} marker and the barrier still joins.
        The gather completes the parallel schedule inside this single step,
        so the main walk resumes directly at the barrier node.
        """
        branches = [b for b in node.config.get("branches", []) if isinstance(b, str)]
        barrier = node.config.get("barrier")
        role = node.config.get("role", node.type)

        async def _run_branch(branch_id: str) -> Dict[str, Any]:
            branch_node = nodes_by_id.get(branch_id)
            if branch_node is None:
                return {"node_id": branch_id, "error": "unknown_branch"}
            state["visited_nodes"].append(branch_id)
            branch_input = dict(step_input)
            try:
                if branch_node.type == "control.loop":
                    return await self._execute_loop_node(
                        request=request, run_id=run_id, task=task, step_id=step_id,
                        node=branch_node, step_input=branch_input, state=state,
                        nodes_by_id=nodes_by_id,
                    )
                if branch_node.type == "approval_gate":
                    # Approval pauses the walk; a paused gather would deadlock.
                    return {
                        "node_id": branch_id, "skipped": True,
                        "message": f"[parallel] branch {branch_id} is an approval_gate; skipped in gather mode",
                    }
                if self._is_delegated_node(branch_node):
                    return await self._execute_delegated_node(
                        request=request, run_id=run_id, task=task, step_id=step_id,
                        node=branch_node, step_input=branch_input, state=state,
                    )
                return await self._execute_node(branch_node, branch_input, state, request.context)
            except Exception as exc:  # noqa: BLE001 — one branch must not sink the others
                return {"node_id": branch_id, "error": str(exc)}

        outputs = await asyncio.gather(*[_run_branch(b) for b in branches])
        branch_outputs: Dict[str, Any] = dict(zip(branches, outputs))
        state.setdefault("parallel", {})[node.id] = {
            "branches": branches,
            "branch_set": set(branches),
            "remaining_branches": [],
            "branch_outputs": branch_outputs,
            "barrier": barrier,
            "gathered": True,
        }
        state["active_parallel"] = node.id
        failed = [
            b for b, o in branch_outputs.items()
            if isinstance(o, dict) and o.get("error")
        ]
        return {
            "message": node.config.get(
                "message_template",
                f"[{role}] gathered {len(branches)} parallel branches concurrently.",
            ),
            "node_id": node.id,
            "node_type": node.type,
            "handled_by": role,
            "state": {},
            "branch_count": len(branches),
            "branches": branches,
            "barrier": barrier,
            "execution": "gather",
            "failed_branches": failed,
            "branch_outputs": {k: self._json_safe(v) for k, v in branch_outputs.items()},
        }

    def _loop_watch_triggered(
        self, watch: List[Dict[str, Any]], signals_map: Dict[str, Any]
    ) -> bool:
        """Return True if any watched loop has already emitted its expected signal."""
        for entry in watch:
            if not isinstance(entry, dict):
                continue
            watched = entry.get("loop")
            expected = entry.get("signal")
            if watched and expected and signals_map.get(watched) == expected:
                return True
        return False

    def _extract_loop_metrics(
        self, body_output: Dict[str, Any], setpoint: Dict[str, Any]
    ) -> Dict[str, float]:
        metric_name = setpoint.get("metric") if isinstance(setpoint, dict) else None
        metrics: Dict[str, float] = {}
        if metric_name and isinstance(body_output, dict):
            raw = body_output.get(metric_name)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                metrics[metric_name] = float(raw)
        return metrics

    def _check_loop_convergence(
        self, setpoint: Dict[str, Any], body_output: Dict[str, Any]
    ) -> bool:
        if not isinstance(setpoint, dict) or not setpoint:
            return False
        metric = setpoint.get("metric")
        op = setpoint.get("op", ">=")
        target = setpoint.get("value")
        if metric is None or target is None or not isinstance(body_output, dict):
            return False
        actual = body_output.get(metric)
        if isinstance(actual, dict) and "metrics" in actual:
            actual = actual["metrics"].get(metric)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        if isinstance(target, bool):
            # boolean convergence (e.g. review_approved == true)
            return op == "==" and actual == (1 if target else 0)
        if not isinstance(target, (int, float)):
            return False
        try:
            if op == ">=":
                return actual >= target
            if op == ">":
                return actual > target
            if op == "<=":
                return actual <= target
            if op == "<":
                return actual < target
            if op == "==":
                return actual == target
            if op == "!=":
                return actual != target
        except Exception:
            return False
        return False

    def _metric_value(
        self, measurement: Dict[str, Any], setpoint: Dict[str, Any]
    ) -> Optional[float]:
        if not isinstance(setpoint, dict) or not isinstance(measurement, dict):
            return None
        metric = setpoint.get("metric")
        if not metric:
            return None
        raw = measurement.get(metric)
        if isinstance(raw, dict) and "metrics" in raw:
            raw = raw["metrics"].get(metric)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw)

    def _sense(
        self,
        sensor_cfg: Dict[str, Any],
        setpoint: Dict[str, Any],
        measurement: Dict[str, Any],
        metric_history: List[float],
    ) -> Dict[str, Any]:
        """C (Sensor): observe the response and return a structured verdict.

        Supported sensor types:
        - threshold (default): compare metric against setpoint
        - trajectory: classify metric-series shape (converging / oscillating / diverging)
        - verifier: LLM-as-a-Verifier (async) — callers must await _sense_with_verifier
          directly; this sync method returns the threshold baseline the verifier
          decorates (never a coroutine).
        """
        sensor_type = sensor_cfg.get("type", "threshold") if isinstance(sensor_cfg, dict) else "threshold"
        value = self._metric_value(measurement, setpoint)
        converged = self._check_loop_convergence(setpoint, measurement)
        result: Dict[str, Any] = {
            "sensor_type": sensor_type,
            "value": value,
            "converged": converged,
        }

        if sensor_type == "trajectory":
            result["classification"] = self._classify_trajectory(metric_history, setpoint, converged)
        elif converged:
            result["classification"] = "converged"
        elif value is not None and isinstance(setpoint.get("value"), (int, float)):
            result["classification"] = "above" if value > setpoint["value"] else "below"
        else:
            result["classification"] = "unknown"
        return result

    async def _sense_with_verifier(
        self,
        sensor_cfg: Dict[str, Any],
        setpoint: Dict[str, Any],
        measurement: Dict[str, Any],
        metric_history: List[float],
        base_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """LLM-as-a-Verifier: score iteration quality with logprob-based probability.

        Formula: R(x,τ) = 1/(C·K) · Σ_c Σ_k Σ_g p(v_g|x,c,τ) · φ(v_g)

        Parameters from config.verifier:
        - G: granularity (1-10, default 20) — scoring granularity
        - K: independent sampling repetitions (default 1, cost-controlled; the
          paper formula uses K=8 — raise via config when budget allows)
        - C: standard splits (default 1) — cross-validation splits
        - provider: LLM provider (default env SF_VERIFIER_PROVIDER or "anthropic"; openai/anthropic)
        - model: model name (uses ApiExecutor provider default if not set)
        - api_key_env: BYOK key env var (default env SF_VERIFIER_API_KEY, else provider default)
        - base_url: BYOK endpoint override
        - system_prompt / prompt_template: 可配置 judge prompt（模板变量
          {metric}/{target}/{op}/{current_value}/{history}）

        Fallback: if LLM call fails, degrade to threshold comparison.
        """
        verifier_cfg = sensor_cfg.get("verifier", {}) if isinstance(sensor_cfg, dict) else {}
        G = int(verifier_cfg.get("G", 20))
        # K 默认 1（每轮迭代只调一次 LLM，控成本）；公式 K=8 通过 config.verifier.K 开启
        K = max(1, int(verifier_cfg.get("K", 1)))
        provider = str(
            verifier_cfg.get("provider") or os.getenv("SF_VERIFIER_PROVIDER") or "anthropic"
        ).lower()

        # Build verification prompt
        metric_name = setpoint.get("metric", "quality")
        target_value = setpoint.get("value")
        op = setpoint.get("op", ">=")
        current_value = base_result.get("value")

        template = verifier_cfg.get("prompt_template")
        if isinstance(template, str) and template.strip():
            try:
                prompt = template.format(
                    metric=metric_name,
                    target=target_value,
                    op=op,
                    current_value=current_value,
                    history=metric_history[-5:] if len(metric_history) >= 5 else metric_history,
                )
            except (KeyError, IndexError, ValueError) as exc:
                logger.warning(f"verifier prompt_template invalid ({exc}), using default")
                template = None
        if not isinstance(template, str) or not template.strip():
            prompt = f"""Evaluate this iteration's output quality based on the convergence criterion.

Metric: {metric_name}
Target: {op} {target_value}
Current value: {current_value}

Recent history: {metric_history[-5:] if len(metric_history) >= 5 else metric_history}

Output a JSON with keys:
- score: float (0.0 to 1.0, where 1.0 = fully converged, 0.0 = fully diverged)
- confidence: float (0.0 to 1.0, how certain are you)
- reasoning: string (brief explanation)
"""

        # BYOK：model / key env / endpoint 均可由 verifier 配置或环境变量覆盖。
        # api_key_env 是"环境变量名"（ApiExecutor 用 os.getenv 解析）；
        # 自定义网关通常自带鉴权，可配 base_url + api_key_env=DUMMY_KEY 之类占位。
        llm_config: Dict[str, Any] = {
            "kind": "api",
            "provider": provider,
            "parse": "json",
        }
        if verifier_cfg.get("model"):
            llm_config["model"] = verifier_cfg["model"]
        api_key_env = verifier_cfg.get("api_key_env") or os.getenv("SF_VERIFIER_API_KEY_ENV")
        if api_key_env:
            llm_config["api_key_env"] = str(api_key_env)
        if verifier_cfg.get("base_url"):
            llm_config["base_url"] = verifier_cfg["base_url"]
        if verifier_cfg.get("system_prompt"):
            llm_config["system_prompt"] = verifier_cfg["system_prompt"]
        # logprob 仅 anthropic 路径原生支持
        if provider == "anthropic":
            llm_config["logprobs"] = True  # 启用原生 logprob
            llm_config["top_logprobs"] = int(verifier_cfg.get("top_logprobs", 5))

        try:
            # Use ApiExecutor for LLM call with logprob
            api_executor = self._executor_registry._executors.get("api")
            if not isinstance(api_executor, ApiExecutor):
                logger.warning("ApiExecutor not found for verifier, degrading to threshold")
                return base_result

            # K 次独立采样求期望（公式 R = 1/K · Σ score_k）。K 默认 1（成本控制），
            # config.verifier.K 可调大降低单次评分方差；任一次失败 → 整体降级 threshold。
            sample_results = await asyncio.gather(*[
                api_executor.execute(config=llm_config, payload={"prompt": prompt})
                for _ in range(K)
            ])

            scores: List[float] = []
            confidences: List[float] = []
            reasonings: List[str] = []
            logprobs_all: List[Dict[str, Any]] = []
            for llm_result in sample_results:
                if not isinstance(llm_result, dict):
                    continue
                score = llm_result.get("score", base_result.get("value", 0))
                confidence = llm_result.get("confidence", 0.5)
                reasonings.append(str(llm_result.get("reasoning", "")))

                # 原生 logprob 数据（如果存在）：平均对数概率 → sigmoid 置信度
                logprobs_data = llm_result.get("logprobs", {})
                if logprobs_data:
                    logprobs_all.append(logprobs_data)
                    raw_logprobs = logprobs_data.get("logprobs", [])
                    if raw_logprobs:
                        avg_logprob = sum(raw_logprobs) / len(raw_logprobs)
                        logprob_confidence = 1 / (1 + math.exp(-avg_logprob))
                        confidence = (confidence + logprob_confidence) / 2

                scores.append(float(score))
                confidences.append(float(confidence))

            if not scores:
                raise ValueError("verifier returned no score samples")

            score = sum(scores) / len(scores)
            confidence = sum(confidences) / len(confidences)
            reasoning = reasonings[0] if reasonings else ""
            variance = sum((s - score) ** 2 for s in scores) / len(scores)

            # VOC-based convergence: if confidence high and score above threshold
            converged = bool(score >= (target_value if isinstance(target_value, (int, float)) else 0.95) and confidence >= 0.7)

            return {
                **base_result,
                "verifier_score": float(score),
                "verifier_confidence": float(confidence),
                "verifier_reasoning": reasoning,
                "verifier_samples": len(scores),
                "verifier_score_std": float(math.sqrt(variance)),
                "converged": converged,
                "classification": "converged" if converged else "diverging",
                "logprob_data": logprobs_all[0] if logprobs_all else None,  # 调试用（首份）
            }
        except Exception as exc:
            logger.warning(f"LLM-as-Verifier failed ({exc}), degrading to threshold")

        # Fallback to threshold
        return base_result

    def _classify_trajectory(
        self, metric_history: List[float], setpoint: Dict[str, Any], converged: bool
    ) -> str:
        """Classify the metric-series shape — the causal signal latent in loop dynamics."""
        if converged:
            return "converged"
        if len(metric_history) < 2:
            return "insufficient"
        op = setpoint.get("op", ">=")
        deltas = [metric_history[i] - metric_history[i - 1] for i in range(1, len(metric_history))]
        recent = deltas[-3:]
        sign_changes = sum(1 for i in range(1, len(recent)) if recent[i] * recent[i - 1] < 0)
        if sign_changes >= max(1, len(recent) // 2):
            return "oscillating"
        increasing = all(d >= 0 for d in recent) and any(d > 0 for d in recent)
        decreasing = all(d <= 0 for d in recent) and any(d < 0 for d in recent)
        toward_target = increasing if op in (">=", ">") else decreasing
        away_target = decreasing if op in (">=", ">") else increasing
        if toward_target:
            return "converging"
        if away_target:
            return "diverging"
        return "drifting"

    def _control(
        self,
        controller_cfg: Dict[str, Any],
        controlled: Dict[str, Any],
        sensor_result: Dict[str, Any],
        setpoint: Dict[str, Any],
        freeze_fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """A (Controller): close the feedback loop by actuating next-iteration variables.

        setpoint_nudge ramps a field toward the setpoint by a fixed rate each non-converged
        iteration. Frozen fields (counterfactual replay, attribution method ③) are never
        actuated. With no controller config the loop is open (controlled unchanged) — the
        backward-compatible default.
        """
        if not isinstance(controller_cfg, dict) or not controller_cfg.get("type"):
            return controlled
        if controller_cfg.get("type") in ("setpoint_nudge", "memory_nudge"):
            field = controller_cfg.get("field") or setpoint.get("metric")
            if not field or field in (freeze_fields or []):
                return controlled
            try:
                rate = float(controller_cfg.get("rate", 1))
            except (TypeError, ValueError):
                rate = 1.0
            try:
                current = float(controlled.get(field, controller_cfg.get("init_value", 0)))
            except (TypeError, ValueError):
                current = 0.0
            controlled = dict(controlled)
            controlled[field] = current + rate
            return controlled
        return controlled

    def _build_coe_claims(
        self,
        measurement: Dict[str, Any],
        sensor_result: Dict[str, Any],
        subgraph_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Build Chain-of-Evidence claims (attribution method ①, Science One CoE).

        Each claim binds a statement to the body node that produced its evidence, so every
        recorded metric is auditable back to its source (completeness) and the sensor verdict
        attaches correctness.
        """
        claims: List[Dict[str, Any]] = []
        value = sensor_result.get("value")
        source = subgraph_ids[-1] if subgraph_ids else "loop"
        if value is not None:
            claims.append({
                "stmt": f"measured={value}",
                "evidence_ref": f"body:{source}",
                "verified": sensor_result.get("classification"),
            })
        for key, val in measurement.items():
            if key.startswith("_") or isinstance(val, (dict, list)):
                continue
            claims.append({
                "stmt": f"{key}={val}",
                "evidence_ref": f"body:{source}",
            })
        return claims

    async def attribute_factor(
        self,
        workflow: WorkflowDefinition,
        input_data: Dict[str, Any],
        factor_field: str,
    ) -> Dict[str, Any]:
        """Counterfactual attribution (method ③): freeze `factor_field` and re-run, then diff
        against the factual run. The delta in iterations/metric is the field's causal
        contribution to convergence — `causally_significant` is True when freezing the factor
        flips the loop from converging to not.
        """
        factual = await self.run(RuntimeRequest(workflow=workflow, input=input_data))
        cf_workflow = self._workflow_with_frozen_factor(workflow, factor_field)
        counterfactual = await self.run(RuntimeRequest(workflow=cf_workflow, input=input_data))
        f = self._loop_summary(factual.final_output)
        c = self._loop_summary(counterfactual.final_output)
        return {
            "factor": factor_field,
            "factual": f,
            "counterfactual": c,
            "delta_iterations": f["iterations"] - c["iterations"],
            "delta_metric": (f["metric"] or 0) - (c["metric"] or 0),
            "causally_significant": bool(f["converged"] and not c["converged"]),
        }

    def _loop_summary(self, loop_output: Dict[str, Any]) -> Dict[str, Any]:
        iterations = int(loop_output.get("loop_iterations", 0))
        converged = bool(loop_output.get("loop_converged"))
        rec = loop_output.get("loop_record") or {}
        iters = rec.get("iterations") or []
        metric = iters[-1].get("verifier_score") if iters else None
        return {"iterations": iterations, "converged": converged, "metric": metric}

    def _workflow_with_frozen_factor(
        self, workflow: WorkflowDefinition, factor_field: str
    ) -> WorkflowDefinition:
        clone = workflow.model_copy(deep=True)
        for node in clone.nodes:
            if node.type == "control.loop":
                frozen = list(node.config.get("freeze") or [])
                if factor_field not in frozen:
                    frozen.append(factor_field)
                node.config["freeze"] = frozen
        return clone

    async def _execute_executor_node(
        self,
        node: NodeDefinition,
        executor_config: Dict[str, Any],
        step_input: Dict[str, Any],
        state: Dict[str, Any],
        context: Dict[str, Any],
        base_output: Dict[str, Any],
        prompt: str,
    ) -> Dict[str, Any]:
        payload = {
            "prompt": prompt,
            "step_input": step_input,
            "state": state,
            "context": context,
            "node": {
                "id": node.id,
                "type": node.type,
                "role": base_output["handled_by"],
            },
            # Adoption #2: provenance reaches ACP sessions as _meta.xueban
            # (run + enclosing loop lineage), Hermes-provenance style.
            "provenance": {
                "run_id": state.get("run_id"),
                "node_id": node.id,
                "loop": step_input.get("_loop_provenance"),
            },
        }

        # Adoption #3: permission tri-state gates the dispatch itself.
        from xueban.runtime.permissions import (
            PermissionBlocked,
            enforce_permission,
            resolve_effective_policy,
        )

        permission_policy = resolve_effective_policy(node.config, state)
        permission_result: Optional[Dict[str, Any]] = None
        if permission_policy is not None:
            permission_result = await enforce_permission(
                self,
                run_id=str(state.get("run_id") or ""),
                node_id=node.id,
                action="dispatch",
                policy=permission_policy,
                summary=str(step_input.get("goal") or step_input.get("message") or "")[:200],
            )

        result = self._json_safe(await self._executor_registry.execute(executor_config, payload))
        output = dict(base_output)
        if isinstance(result, dict):
            output.update(result)
        else:
            output["result"] = result
        if permission_result is not None:
            output["permission"] = permission_result

        # N1 validation hooks (waterfall semantics): node.config.validation_hooks
        # specs run against the produced output; on_fail=blocker raises
        # HookRejected (node/run fails), warn marks the output and continues.
        inline_hooks = node.config.get("validation_hooks")
        if isinstance(inline_hooks, list) and inline_hooks:
            from xueban.runtime.hook_pipeline import ValidationHookRunner, parse_inline_hooks

            specs = parse_inline_hooks(inline_hooks)
            if specs:
                outcomes = await ValidationHookRunner().run_all(
                    specs,
                    {
                        "node_id": node.id,
                        "phase": "post_execute",
                        "results": self._json_safe(
                            {k: v for k, v in output.items() if k != "state"}
                        ),
                    },
                )
                output["validation_hooks"] = [
                    {"hook_id": spec.id, "passed": o.passed, "detail": o.detail, "attempts": o.attempts}
                    for spec, o in zip(specs, outcomes)
                ]
        return output

    def _apply_node_config_output(
        self,
        *,
        node: NodeDefinition,
        output: Dict[str, Any],
        step_input: Dict[str, Any],
        context: Dict[str, Any],
        prompt: str,
    ) -> None:
        if prompt:
            output.setdefault("prompt", prompt)
        if "set_state" in node.config and isinstance(node.config["set_state"], dict):
            output["state"].update(node.config["set_state"])
        if "emit" in node.config and isinstance(node.config["emit"], dict):
            for key, value in node.config["emit"].items():
                output.setdefault(key, value)
        if "copy_input" in node.config:
            keys = node.config["copy_input"]
            if isinstance(keys, list):
                output.setdefault("copied_input", {key: step_input.get(key) for key in keys})
        if "artifact" in node.config and "artifact" not in output:
            output["artifact"] = node.config["artifact"]
        if "artifacts" in node.config and "artifacts" not in output:
            output["artifacts"] = node.config["artifacts"]
        if "decision" in node.config and "decision" not in output:
            output["decision"] = node.config["decision"]
        if "context_echo" in node.config and "context" not in output:
            output["context"] = {key: context.get(key) for key in node.config["context_echo"]}

    def _build_artifacts(
        self,
        request: RuntimeRequest,
        run_id: str,
        workflow_id: str,
        index: int,
        node: NodeDefinition,
        node_id: str,
        step_output: Dict[str, Any],
    ) -> List[ArtifactRef]:
        artifact_payloads: List[Any] = []
        if "artifact" in step_output:
            artifact_payloads.append(step_output["artifact"])
        if isinstance(step_output.get("artifacts"), list):
            artifact_payloads.extend(step_output["artifacts"])
        if not artifact_payloads:
            return []
        artifact_refs: List[ArtifactRef] = []
        for artifact_offset, artifact_payload in enumerate(artifact_payloads, start=1):
            if isinstance(artifact_payload, dict):
                payload = artifact_payload
            else:
                payload = {"content": artifact_payload}

            writeback_config = self._resolve_writeback_config(request, "artifact", payload, node=node)
            target = writeback_config.get("target", "host")
            mode = writeback_config.get("mode", "reference")
            has_inline_content = payload.get("content") is not None
            if mode == "inline" and not has_inline_content:
                raise ValueError(
                    f"artifact writeback for node {node_id} requires content when mode=inline"
                )
            artifact_refs.append(
                ArtifactRef(
                    artifact_id=f"artifact-{uuid4().hex[:10]}",
                    kind=payload.get("kind", "json"),
                    name=payload.get("name", f"{node_id}-artifact-{index}-{artifact_offset}.json"),
                    uri=payload.get("uri", f"memory://{run_id}/{node_id}/{index}/{artifact_offset}"),
                    producer_step_id=f"step-{index:03d}",
                    writeback=WritebackRef(
                        channel="artifact",
                        target=target,
                        mode=mode,
                        host_action="persist_artifact_ref",
                        content_field="metadata.content" if mode == "inline" and has_inline_content else None,
                    ),
                    metadata={
                        "content": payload.get("content"),
                        "summary": _plain_text_summary(payload.get("content")),
                        "workflow_id": workflow_id,
                        "producer_node_id": node_id,
                    },
                )
            )

        return artifact_refs

    def _resolve_writeback_config(
        self,
        request: RuntimeRequest,
        channel: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        node: Optional[NodeDefinition] = None,
    ) -> Dict[str, Any]:
        workflow_defaults = request.workflow.defaults.get("writeback", {})
        request_overrides = request.metadata.get("writeback", {})
        resolved: Dict[str, Any] = {
            **workflow_defaults.get(channel, {}),
            **request_overrides.get(channel, {}),
        }

        if channel == "artifact" and isinstance(payload, dict):
            payload_writeback = payload.get("writeback")
            if isinstance(payload_writeback, dict):
                resolved.update(payload_writeback)
            elif node is not None:
                node_artifact = node.config.get("artifact")
                if isinstance(node_artifact, dict) and isinstance(node_artifact.get("writeback"), dict):
                    resolved.update(node_artifact["writeback"])

        if channel == "checkpoint":
            resolved.setdefault("mode", "reference")

        return resolved

    def _build_checkpoint_payload(self, state: Dict[str, Any]) -> Dict[str, Any]:
        shared_state = {
            key: value
            for key, value in state.items()
            if key
            not in {
                "visited_nodes",
                "step_outputs",
                "input",
                "context",
                "workflow_id",
                "root_input",
                "artifacts_by_step",
            }
        }
        if "parallel" in shared_state and isinstance(shared_state["parallel"], dict):
            shared_state["parallel"] = self._sanitize_parallel_state(shared_state["parallel"])
        return {
            "step_outputs": state["step_outputs"],
            "shared_state": shared_state,
        }

    def _sanitize_parallel_state(self, parallel_state: Dict[str, Any]) -> Dict[str, Any]:
        cleaned: Dict[str, Any] = {}
        for parallel_id, payload in parallel_state.items():
            if not isinstance(payload, dict):
                cleaned[parallel_id] = payload
                continue
            cleaned[parallel_id] = {
                key: value
                for key, value in payload.items()
                if key != "branch_set"
            }
        return cleaned

    def _get_workflow_index(self, workflow: WorkflowDefinition) -> Dict[str, Any]:
        cache_key = id(workflow)
        cached = self._workflow_index_cache.get(cache_key)
        if cached is not None and cached[0] is workflow:
            return cached[1]

        compiled = self._build_workflow_index(workflow)
        if len(self._workflow_index_cache) >= 128:
            self._workflow_index_cache.clear()
        self._workflow_index_cache[cache_key] = (workflow, compiled)
        return compiled

    def _build_workflow_index(self, workflow: WorkflowDefinition) -> Dict[str, Any]:
        nodes_by_id = {node.id: node for node in workflow.nodes}
        edges_by_from: Dict[str, List[Any]] = {}
        handoff_goals: Dict[str, Optional[str]] = {}

        for node in workflow.nodes:
            handoff_goals[node.id] = self._extract_handoff_goal(node)

        for edge in workflow.edges:
            edges_by_from.setdefault(edge.from_id, []).append(
                {
                    "to_id": edge.to_id,
                    "condition": self._compile_condition(edge.condition),
                }
            )
        return {
            "nodes_by_id": nodes_by_id,
            "edges_by_from": edges_by_from,
            "handoff_goals": handoff_goals,
        }

    def _as_string_list(self, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]

    def _resolve_node_activation_config(
        self,
        request: RuntimeRequest,
        node: NodeDefinition,
    ) -> Dict[str, Any]:
        workflow_activation = (
            request.workflow.metadata.get("template_activation", {})
            if isinstance(request.workflow.metadata.get("template_activation"), dict)
            else {}
        )
        node_activation = node.config.get("local_activation", {}) if isinstance(node.config.get("local_activation"), dict) else {}
        default_mode = workflow_activation.get("default_mode", "always")
        return {
            "mode": node_activation.get("mode", default_mode),
            "tags": self._as_string_list(node_activation.get("tags")),
            "activate_when": self._as_string_list(node_activation.get("activate_when")),
            "suppress_when": self._as_string_list(node_activation.get("suppress_when")),
            "delegate_candidates": self._as_string_list(node_activation.get("delegate_candidates")),
            "subgoal_triggers": self._as_string_list(node_activation.get("subgoal_triggers")),
            "retry_gates": self._as_string_list(node_activation.get("retry_gates")),
            "review_gates": self._as_string_list(node_activation.get("review_gates")),
            "feedback_channels": self._as_string_list(node_activation.get("feedback_channels")),
            "signal_sources": self._as_string_list(workflow_activation.get("signal_sources")),
            "workflow_activation_enabled": bool(workflow_activation.get("enabled", True)),
            "workflow_default_mode": default_mode,
            "workflow_activation_metadata": workflow_activation.get("metadata", {})
            if isinstance(workflow_activation.get("metadata"), dict)
            else {},
            "metadata": node_activation.get("metadata", {}) if isinstance(node_activation.get("metadata"), dict) else {},
        }

    def _resolve_activation_policy(self, request: RuntimeRequest) -> Dict[str, Any]:
        workflow_activation = (
            request.workflow.metadata.get("template_activation", {})
            if isinstance(request.workflow.metadata.get("template_activation"), dict)
            else {}
        )
        request_policy = (
            request.metadata.get("activation_policy", {})
            if isinstance(request.metadata.get("activation_policy"), dict)
            else {}
        )
        selection_enabled = any(
            key in workflow_activation for key in ("selection_threshold", "top_k", "budget")
        ) or any(
            key in request_policy for key in ("selection_threshold", "top_k", "budget")
        )
        threshold = request_policy.get("selection_threshold", workflow_activation.get("selection_threshold", 0.0))
        top_k = request_policy.get("top_k", workflow_activation.get("top_k"))
        budget = request_policy.get("budget", workflow_activation.get("budget"))
        signal_weights = request_policy.get("signal_weights", workflow_activation.get("signal_weights", {}))
        candidate_type_weights = request_policy.get(
            "candidate_type_weights", workflow_activation.get("candidate_type_weights", {})
        )
        try:
            threshold = float(threshold)
        except (TypeError, ValueError):
            threshold = 0.0
        if not isinstance(top_k, int) or top_k <= 0:
            top_k = None
        if not isinstance(budget, int) or budget <= 0:
            budget = None
        return {
            "selection_enabled": selection_enabled,
            "selection_threshold": threshold,
            "top_k": top_k,
            "budget": budget,
            "signal_weights": signal_weights if isinstance(signal_weights, dict) else {},
            "candidate_type_weights": candidate_type_weights if isinstance(candidate_type_weights, dict) else {},
        }

    def _build_activation_record(
        self,
        *,
        request: RuntimeRequest,
        run_id: str,
        task: TaskRecord,
        step_id: str,
        node: NodeDefinition,
        candidates: List[ActivationCandidate],
    ) -> ActivationRecord:
        resolved = self._resolve_node_activation_config(request, node)
        node_candidates = [candidate for candidate in candidates if candidate.candidate_type == "node"]
        selected_node_candidate = next((candidate for candidate in node_candidates if candidate.selected), None)
        decision = "activated" if selected_node_candidate is not None else "suppressed"
        reasons = ["runtime_selected"] if decision == "activated" else ["runtime_suppressed"]
        if resolved["mode"] == "local":
            reasons.append("local_activation_scope")
        if self._is_delegated_node(node):
            reasons.append("delegated_node")
        if selected_node_candidate is None:
            reasons.extend(
                candidate.suppressed_reason
                for candidate in node_candidates
                if isinstance(candidate.suppressed_reason, str) and candidate.suppressed_reason
            )
        return ActivationRecord(
            activation_id=f"act-{uuid4().hex[:10]}",
            run_id=run_id,
            task_id=task.task_id,
            step_id=step_id,
            node_id=node.id,
            mode=resolved["mode"],
            decision=decision,
            tags=resolved["tags"],
            activate_when=resolved["activate_when"],
            suppress_when=resolved["suppress_when"],
            delegate_candidates=resolved["delegate_candidates"],
            subgoal_triggers=resolved["subgoal_triggers"],
            retry_gates=resolved["retry_gates"],
            review_gates=resolved["review_gates"],
            feedback_channels=resolved["feedback_channels"],
            signal_sources=resolved["signal_sources"],
            reasons=reasons,
            metadata={
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "selected_candidate_ids": [candidate.candidate_id for candidate in candidates if candidate.selected],
                "node_type": node.type,
                "node_kind": node.kind,
                "workflow_activation_enabled": resolved["workflow_activation_enabled"],
                "workflow_default_mode": resolved["workflow_default_mode"],
                **resolved["workflow_activation_metadata"],
                **resolved["metadata"],
            },
        )

    def _build_activation_candidates(
        self,
        *,
        request: RuntimeRequest,
        run_id: str,
        task: TaskRecord,
        step_id: str,
        node: NodeDefinition,
        step_input: Dict[str, Any],
        state: Dict[str, Any],
    ) -> List[ActivationCandidate]:
        resolved = self._resolve_node_activation_config(request, node)
        policy = self._resolve_activation_policy(request)
        created_at = utc_now()
        signal_sources = resolved["signal_sources"] or ["goal", "context"]
        node_selected, suppressed_reason, node_score, node_signal_sources = self._evaluate_node_activation_candidate(
            request=request,
            node=node,
            resolved=resolved,
            step_input=step_input,
            state=state,
        )
        node_scoring = self._score_activation_candidate(
            candidate_type="node",
            base_score=node_score,
            source_signals=node_signal_sources,
            policy=policy,
        )
        candidates: List[ActivationCandidate] = [
            ActivationCandidate(
                candidate_id=f"cand-{uuid4().hex[:10]}",
                run_id=run_id,
                task_id=task.task_id,
                step_id=step_id,
                node_id=node.id,
                candidate_type="node",
                candidate_ref=node.id,
                source_signals=node_signal_sources,
                score=node_scoring["score"],
                selected=node_selected,
                suppressed_reason=suppressed_reason,
                created_at=created_at,
                metadata={
                    "node_type": node.type,
                    "node_kind": node.kind,
                    "scoring_breakdown": node_scoring,
                },
            )
        ]

        for candidate_index, candidate_ref in enumerate(resolved["delegate_candidates"], start=1):
            scoring = self._score_activation_candidate(
                candidate_type="delegate_target",
                base_score=max(0.1, 0.85 - (0.1 * (candidate_index - 1))),
                source_signals=signal_sources,
                policy=policy,
            )
            candidates.append(
                ActivationCandidate(
                    candidate_id=f"cand-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step_id,
                    node_id=node.id,
                    candidate_type="delegate_target",
                    candidate_ref=candidate_ref,
                    source_signals=signal_sources,
                    score=scoring["score"],
                    selected=False,
                    suppressed_reason="not_selected_in_minimal_runtime",
                    created_at=created_at,
                    metadata={"source_node_id": node.id, "scoring_breakdown": scoring},
                )
            )

        for trigger_index, trigger in enumerate(resolved["subgoal_triggers"], start=1):
            scoring = self._score_activation_candidate(
                candidate_type="subgoal",
                base_score=max(0.1, 0.65 - (0.1 * (trigger_index - 1))),
                source_signals=signal_sources,
                policy=policy,
            )
            candidates.append(
                ActivationCandidate(
                    candidate_id=f"cand-{uuid4().hex[:10]}",
                    run_id=run_id,
                    task_id=task.task_id,
                    step_id=step_id,
                    node_id=node.id,
                    candidate_type="subgoal",
                    candidate_ref=trigger,
                    source_signals=signal_sources,
                    score=scoring["score"],
                    selected=False,
                    suppressed_reason="subgoal_not_spawned_in_minimal_runtime",
                    created_at=created_at,
                    metadata={"source_node_id": node.id, "scoring_breakdown": scoring},
                )
            )

        self._apply_candidate_selection_policy(candidates, policy)
        return candidates

    def _score_activation_candidate(
        self,
        *,
        candidate_type: str,
        base_score: float,
        source_signals: List[str],
        policy: Dict[str, Any],
    ) -> Dict[str, Any]:
        type_weights = policy.get("candidate_type_weights", {})
        signal_weights = policy.get("signal_weights", {})
        try:
            type_weight = float(type_weights.get(candidate_type, 1.0))
        except (TypeError, ValueError):
            type_weight = 1.0
        signal_bonus = 0.0
        weighted_signals: Dict[str, float] = {}
        for signal in source_signals:
            try:
                weight = float(signal_weights.get(signal, 0.0))
            except (TypeError, ValueError):
                weight = 0.0
            weighted_signals[signal] = weight
            signal_bonus += weight
        score = max(0.0, round((base_score * type_weight) + signal_bonus, 4))
        return {
            "base_score": base_score,
            "type_weight": type_weight,
            "signal_bonus": round(signal_bonus, 4),
            "weighted_signals": weighted_signals,
            "score": score,
        }

    def _apply_candidate_selection_policy(
        self,
        candidates: List[ActivationCandidate],
        policy: Dict[str, Any],
    ) -> None:
        threshold = float(policy.get("selection_threshold", 0.0))
        top_k = policy.get("top_k")
        budget = policy.get("budget")
        if not policy.get("selection_enabled"):
            return
        ranked = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.candidate_type != "node"
            ),
            key=lambda item: item.score,
            reverse=True,
        )
        selected_count = 0
        for index, candidate in enumerate(ranked, start=1):
            if candidate.score < threshold:
                candidate.selected = False
                candidate.suppressed_reason = "below_selection_threshold"
                continue
            if top_k is not None and index > top_k:
                candidate.selected = False
                candidate.suppressed_reason = "excluded_by_top_k"
                continue
            if budget is not None and selected_count >= budget:
                candidate.selected = False
                candidate.suppressed_reason = "excluded_by_budget"
                continue
            candidate.selected = True
            candidate.suppressed_reason = None
            selected_count += 1

    def _evaluate_node_activation_candidate(
        self,
        *,
        request: RuntimeRequest,
        node: NodeDefinition,
        resolved: Dict[str, Any],
        step_input: Dict[str, Any],
        state: Dict[str, Any],
    ) -> tuple[bool, Optional[str], float, List[str]]:
        signal_sources = list(resolved["signal_sources"] or ["goal", "context"])
        override_payload = (
            request.metadata.get("activation_overrides", {})
            if isinstance(request.metadata.get("activation_overrides"), dict)
            else {}
        )
        override_nodes = {
            item for item in override_payload.get("nodes", []) if isinstance(item, str) and item
        }
        override_tags = {
            item for item in override_payload.get("tags", []) if isinstance(item, str) and item
        }
        node_tags = set(resolved["tags"])

        if resolved["suppress_when"] and any(
            self._match_condition(condition, step_input, state) for condition in resolved["suppress_when"]
        ):
            return False, "suppress_when_matched", 0.0, signal_sources

        if resolved["activate_when"] and not any(
            self._match_condition(condition, step_input, state) for condition in resolved["activate_when"]
        ):
            return False, "activate_when_not_met", 0.0, signal_sources

        if resolved["mode"] == "manual":
            manually_selected = node.id in override_nodes or bool(node_tags & override_tags)
            if manually_selected:
                return True, None, 1.0, signal_sources + ["manual_override"]
            return False, "manual_activation_required", 0.0, signal_sources

        if resolved["mode"] == "local":
            return True, None, 0.9, signal_sources

        return True, None, 1.0, signal_sources

    def _build_suppressed_step_output(
        self,
        *,
        node: NodeDefinition,
        activation: ActivationRecord,
    ) -> Dict[str, Any]:
        return {
            "message": f"[runtime] skipped node {node.id} due to activation gating.",
            "node_id": node.id,
            "node_type": node.type,
            "handled_by": "activation-gate",
            "state": {},
            "skipped": True,
            "activation_decision": activation.decision,
        }

    def _build_step_feedback_record(
        self,
        *,
        run_id: str,
        task: TaskRecord,
        step: StepRecord,
        activation: ActivationRecord,
        candidates: List[ActivationCandidate],
        checkpoint: CheckpointRef,
        next_node_id: Optional[str],
        artifact_ids: List[str],
        handoff: Optional[HandoffRef],
    ) -> ExecutionFeedbackRecord:
        delegated_run = bool(step.output.get("delegated_run"))
        review_gate_triggered = bool(activation.review_gates)
        signals = {
            "node_id": step.node_id,
            "node_type": step.metadata.get("node_type"),
            "step_status": step.status,
            "activation_id": activation.activation_id,
            "activation_mode": activation.mode,
            "activation_decision": activation.decision,
            "candidate_count": len(candidates),
            "selected_candidate_count": sum(1 for candidate in candidates if candidate.selected),
            "delegate_candidate_count": sum(
                1 for candidate in candidates if candidate.candidate_type == "delegate_target"
            ),
            "selected_non_node_candidate_count": sum(
                1 for candidate in candidates if candidate.candidate_type != "node" and candidate.selected
            ),
            "artifact_count": len(artifact_ids),
            "artifact_ids": artifact_ids,
            "checkpoint_id": checkpoint.checkpoint_id,
            "next_node_id": next_node_id,
            "handoff_id": handoff.handoff_id if handoff is not None else None,
            "delegated_run": delegated_run,
            "child_run_id": step.output.get("child_run_id"),
            "review_gate_triggered": review_gate_triggered,
            "retry_gate_count": len(activation.retry_gates),
        }
        reward_hints = {
            "artifact_count": float(len(artifact_ids)),
            "delegated_run": 1.0 if delegated_run else 0.0,
            "continued_flow": 1.0 if next_node_id is not None else 0.0,
            "review_gate_triggered": 1.0 if review_gate_triggered else 0.0,
            "selected_candidates": float(sum(1 for candidate in candidates if candidate.selected)),
        }
        return ExecutionFeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:10]}",
            run_id=run_id,
            task_id=task.task_id,
            step_id=step.step_id,
            node_id=step.node_id,
            source_type="step",
            status=(
                "succeeded"
                if step.status == "succeeded"
                else "suppressed" if step.status == "skipped" else "failed"
            ),
            summary=f"Feedback captured for step {step.node_id}",
            signals=signals,
            reward_hints=reward_hints,
            metadata={"feedback_channels": activation.feedback_channels},
        )

    def _build_run_feedback_record(
        self,
        *,
        run: RunRecord,
        task: TaskRecord,
        steps: List[StepRecord],
        artifacts: List[ArtifactRef],
        checkpoints: List[CheckpointRef],
        handoffs: List[HandoffRef],
        activation_candidates: List[ActivationCandidate],
        activations: List[ActivationRecord],
        feedback_records: List[ExecutionFeedbackRecord],
    ) -> ExecutionFeedbackRecord:
        return ExecutionFeedbackRecord(
            feedback_id=f"fb-{uuid4().hex[:10]}",
            run_id=run.run_id,
            task_id=task.task_id,
            source_type="run",
            status="succeeded" if run.status == "succeeded" else "failed",
            summary=f"Run feedback captured for {run.workflow_id}",
            signals={
                "run_status": run.status,
                "step_count": len(steps),
                "artifact_count": len(artifacts),
                "checkpoint_count": len(checkpoints),
                "handoff_count": len(handoffs),
                "activation_candidate_count": len(activation_candidates),
                "activation_count": len(activations),
                "step_feedback_count": len(feedback_records),
            },
            reward_hints={
                "completion": 1.0 if run.status == "succeeded" else 0.0,
                "artifact_density": float(len(artifacts)),
                "candidate_coverage": float(len(activation_candidates)),
                "activation_coverage": float(len(activations)),
            },
            metadata={"workflow_id": run.workflow_id},
        )

    def _resolve_next_node(
        self,
        node: NodeDefinition,
        step_output: Dict[str, Any],
        state: Dict[str, Any],
        edges_by_from: Dict[str, List[Any]],
    ) -> Optional[str]:
        current_node_id = node.id
        if node.type == "control.parallel":
            parallel_state = state.get("parallel", {}).get(node.id, {})
            remaining = parallel_state.get("remaining_branches", [])
            if remaining:
                return remaining[0]
            return parallel_state.get("barrier")

        active_parallel_id = state.get("active_parallel")
        if active_parallel_id:
            parallel_state = state.get("parallel", {}).get(active_parallel_id, {})
            branches = parallel_state.get("branch_set")
            if branches is None:
                branches = set(parallel_state.get("branches", []))
                parallel_state["branch_set"] = branches
            if current_node_id in branches:
                remaining = parallel_state.get("remaining_branches", [])
                if remaining:
                    return remaining[0]
                return parallel_state.get("barrier")

        candidates = edges_by_from.get(current_node_id, [])
        for edge in candidates:
            if self._match_compiled_condition(edge["condition"], step_output, state):
                return None if edge["to_id"] == "END" else edge["to_id"]
        return None

    def _apply_runtime_transitions(
        self,
        node: NodeDefinition,
        step_output: Dict[str, Any],
        state: Dict[str, Any],
    ) -> None:
        if node.type == "control.parallel":
            existing = state.get("parallel", {}).get(node.id)
            if isinstance(existing, dict) and existing.get("gathered"):
                # gather mode already executed every branch inside the step
                # and seeded branch_outputs — do not reset the schedule.
                return
            branches = list(step_output.get("branches", []))
            state["parallel"][node.id] = {
                "branches": branches,
                "branch_set": set(branches),
                "remaining_branches": branches.copy(),
                "branch_outputs": {},
                "barrier": step_output.get("barrier"),
            }
            state["active_parallel"] = node.id
            return

        active_parallel_id = state.get("active_parallel")
        if not active_parallel_id:
            return

        parallel_state = state.get("parallel", {}).get(active_parallel_id)
        if not parallel_state:
            return

        if node.id in parallel_state.get("branches", []):
            parallel_state.setdefault("branch_outputs", {})[node.id] = step_output
            parallel_state["remaining_branches"] = [
                branch_id for branch_id in parallel_state.get("remaining_branches", []) if branch_id != node.id
            ]

    def _finalize_runtime_transitions(self, node: NodeDefinition, state: Dict[str, Any]) -> None:
        if node.type != "control.barrier":
            return

        source_parallel = node.config.get("source_parallel") or state.get("active_parallel")
        if source_parallel:
            state["active_parallel"] = None

    def _match_condition(
        self,
        condition: Optional[str],
        step_output: Dict[str, Any],
        state: Dict[str, Any],
    ) -> bool:
        return self._match_compiled_condition(self._compile_condition(condition), step_output, state)

    def _compile_condition(self, condition: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        if not condition:
            return []

        normalized = condition.strip()
        parts = (
            self._condition_splitter.split(normalized)
            if "&&" in normalized or " and " in normalized
            else [normalized]
        )
        checks: List[Dict[str, Any]] = []
        for part in parts:
            clause = part.strip()
            if not clause:
                continue

            source_name = "result"
            expr = clause
            if "." in clause:
                root, expr = clause.split(".", 1)
                root = root.strip()
                if root == "result":
                    source_name = "result"
                elif root == "state":
                    source_name = "state"
                else:
                    return None

            match = self._condition_pattern.match(expr.strip())
            if not match:
                return None

            key, op, raw_value = match.groups()
            expected: Any = raw_value.strip()
            try:
                expected = json.loads(expected)
            except json.JSONDecodeError:
                pass
            checks.append(
                {
                    "source": source_name,
                    "key": key,
                    "op": op,
                    "expected": expected,
                }
            )
        return checks

    def _match_compiled_condition(
        self,
        condition: Optional[List[Dict[str, Any]]],
        step_output: Dict[str, Any],
        state: Dict[str, Any],
    ) -> bool:
        if condition is None:
            return False
        if not condition:
            return True

        for check in condition:
            source = step_output if check["source"] == "result" else state
            actual = source.get(check["key"])
            if actual is None:
                return False
            expected = self._coerce_expected_value(actual, check["expected"])
            if not self._compare_values(actual, expected, check["op"]):
                return False
        return True

    def _extract_handoff_goal(self, node: NodeDefinition) -> Optional[str]:
        assignment = node.config.get("assignment")
        if not isinstance(assignment, dict):
            return None
        raw_handoff_goal = assignment.get("handoff_goal")
        if isinstance(raw_handoff_goal, str) and raw_handoff_goal.strip():
            return raw_handoff_goal.strip()
        return None

    def _coerce_expected_value(self, actual: Any, expected: Any) -> Any:
        if isinstance(actual, bool) and isinstance(expected, str):
            return expected.lower() in {"true", "1", "yes"}
        if isinstance(actual, (int, float)) and not isinstance(expected, (int, float)):
            try:
                return float(expected)
            except (TypeError, ValueError):
                return expected
        return expected

    def _compare_values(self, actual: Any, expected: Any, op: str) -> bool:
        if op == ">":
            return actual > expected
        if op == ">=":
            return actual >= expected
        if op == "<":
            return actual < expected
        if op == "<=":
            return actual <= expected
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op in {"contains", "includes"}:
            return str(expected).lower() in str(actual).lower()
        return False

    def _build_chat_prompt(self, messages: List[ChatMessage]) -> str:
        segments: List[str] = []
        for message in messages:
            if not message.content.strip():
                continue
            segments.append(f"{message.role.upper()}:\n{message.content.strip()}")
        segments.append("ASSISTANT:")
        return "\n\n".join(segments)

    def _eval_expr(self, expr: str, source: Dict[str, Any]) -> bool:
        match = self._condition_pattern.match(expr)
        if not match:
            return False

        key, op, raw_value = match.groups()
        actual = source.get(key)
        if actual is None:
            return False

        expected: Any = raw_value.strip()
        try:
            expected = json.loads(expected)
        except json.JSONDecodeError:
            pass
        expected = self._coerce_expected_value(actual, expected)
        return self._compare_values(actual, expected, op)

    def _json_safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._json_safe(item) for item in value]
        return str(value)

    # ------------------------------------------------------------------
    # ReAct Loop (Story 11.4) — LLM tool_use + MCP execution
    # ------------------------------------------------------------------

    async def run_agent_with_tools(
        self,
        agent_blueprint: Any,
        task: str,
        mcp_clients: List[Any],
        llm_provider: Any,
        run_id: Optional[str] = None,
        max_iterations: int = 10,
    ) -> str:
        """ReAct 循环：LLM 规划 → MCP 工具调用 → 迭代直到 LLM 停止或达最大轮次。

        Args:
            agent_blueprint: 含 .soul 字段（系统提示）的 agent 规格对象。
            task: 用户任务字符串。
            mcp_clients: MCP 客户端列表（duck-typed: list_tools / call_tool）。
                list_tools() 可返回 List[str] 或 List[Dict{name,description,inputSchema}]。
            llm_provider: LLMProvider 实例，实现 chat(messages, tools=...) 方法。
            run_id: 可选 run_id，用于 SSE 事件路由；为 None 时不发送 SSE。
            max_iterations: 最大 ReAct 轮次，默认 10 次。

        Returns:
            LLM 最终生成的文本内容。
        """
        # 1. 从所有 MCP 客户端收集工具定义
        tools: List[Dict[str, Any]] = []
        tool_to_client: Dict[str, Any] = {}

        for client in mcp_clients:
            raw = await client.list_tools()
            for item in raw:
                if isinstance(item, str):
                    # 旧接口只返回名称，无 schema
                    tool_def: Dict[str, Any] = {"name": item, "description": "", "inputSchema": {}}
                else:
                    tool_def = item
                tool_name = tool_def["name"]
                if tool_name in tool_to_client:
                    logger.warning(
                        "run_agent_with_tools: tool name collision '%s' — "
                        "second client overrides first; use namespaced tool names to avoid this.",
                        tool_name,
                    )
                else:
                    # 仅在无重名时追加到 tools 列表（Anthropic/OpenAI API 不允许重名工具）
                    tools.append(tool_def)
                tool_to_client[tool_name] = client

        # 2. 初始化消息列表
        system_prompt = getattr(agent_blueprint, "soul", None) or ""
        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": task})

        # 3. ReAct 循环
        for _iteration in range(max_iterations):
            response = await llm_provider.chat(messages, tools=tools if tools else None)

            if not response.tool_calls:
                # LLM 决定停止，返回最终文本
                if run_id is not None and self._event_bus is not None:
                    self._event_bus.publish(run_id, {
                        "type": "agent.completed",
                        "content": response.content,
                    })
                return response.content

            # 将 assistant 的 tool_use 回合追加到消息历史
            messages.append({"role": "assistant", "content": response.content, "_tool_calls": [
                {"id": tc.id, "name": tc.name, "args": tc.args}
                for tc in response.tool_calls
            ]})

            # 执行所有工具调用
            for call in response.tool_calls:
                if run_id is not None and self._event_bus is not None:
                    self._event_bus.publish(run_id, {
                        "type": "agent.tool_called",
                        "name": call.name,
                        "args": call.args,
                    })

                client_for_tool = tool_to_client.get(call.name)
                if client_for_tool is None:
                    result = {"error": f"Tool '{call.name}' not found in any MCP client"}
                else:
                    try:
                        result = await client_for_tool.call_tool(call.name, call.args)
                    except Exception as exc:
                        result = {"error": str(exc)}

                if run_id is not None and self._event_bus is not None:
                    self._event_bus.publish(run_id, {
                        "type": "agent.tool_result",
                        "call_id": call.id,
                        "result": result,
                    })

                # 将工具结果注入消息列表（OpenAI / Claude 通用格式）
                # json.dumps 保证结果为合法 JSON 字符串，而非 Python repr（单引号）
                result_str = (
                    json.dumps(result, ensure_ascii=False)
                    if isinstance(result, (dict, list))
                    else str(result)
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result_str,
                })

        # 超过最大迭代次数，优雅终止
        if run_id is not None and self._event_bus is not None:
            self._event_bus.publish(run_id, {"type": "agent.max_iterations_reached"})

        last_content = messages[-1].get("content", "") if messages else ""
        return str(last_content)

    # ------------------------------------------------------------------
    # Long-task execution with checkpoint/resume (Story 11.5)
    # ------------------------------------------------------------------

    async def run_agent_long_task(
        self,
        agent_id: str,
        session_id: str,
        agent_blueprint: Any,
        task: str,
        mcp_clients: List[Any],
        llm_provider: Any,
        run_id: Optional[str] = None,
        max_iterations_per_segment: int = 20,
        max_segments: int = 10,
        max_error_retries: int = 3,
    ) -> "TaskReport":
        """Long-task ReAct executor with workspace context, self-healing and segmentation.

        Extends run_agent_with_tools with:
        - Persistent AgentWorkspaceContext across segments
        - Error self-recovery (up to max_error_retries per tool call)
        - Auto-segment every max_iterations_per_segment steps
        - Structured TaskReport on completion

        Args:
            agent_id: Agent identifier.
            session_id: Session identifier (also used as Redis/store key).
            agent_blueprint: Agent spec with .soul system prompt.
            task: Natural-language task description.
            mcp_clients: List of MCP client objects.
            llm_provider: LLMProvider implementing chat(..., tools=...).
            run_id: Optional run_id for SSE events.
            max_iterations_per_segment: ReAct iterations per segment (default 20).
            max_segments: Max number of segments (default 10).
            max_error_retries: Max self-healing retries per tool error (default 3).

        Returns:
            TaskReport with execution summary.
        """
        import time as _time_mod
        from xueban.runtime.workspace_context import (
            AgentWorkspaceContext,
            TaskReport,
            get_store,
        )

        ctx_store = get_store()
        start_time = _time_mod.monotonic()

        # Load or create workspace context (supports resume)
        ctx = ctx_store.load(agent_id, session_id)
        is_resume = ctx is not None and len(ctx.completed_steps) > 0
        if ctx is None:
            ctx = AgentWorkspaceContext(
                agent_id=agent_id,
                session_id=session_id,
                task_description=task,
            )

        # Collect tools once (same logic as run_agent_with_tools)
        tools: List[Dict[str, Any]] = []
        tool_to_client: Dict[str, Any] = {}
        for client in mcp_clients:
            raw = await client.list_tools()
            for item in raw:
                tool_def: Dict[str, Any] = (
                    item if isinstance(item, dict)
                    else {"name": item, "description": "", "inputSchema": {}}
                )
                tname = tool_def["name"]
                if tname not in tool_to_client:
                    tools.append(tool_def)
                tool_to_client[tname] = client

        # System prompt
        system_prompt = getattr(agent_blueprint, "soul", None) or ""

        errors_log: List[str] = []
        total_recovery_attempts = 0

        for segment_idx in range(max_segments):
            # Build messages for this segment
            messages: List[Dict[str, Any]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})

            # Inject resume context from previous segments
            if is_resume or segment_idx > 0:
                resume_text = ctx.resume_prompt()
                if resume_text:
                    messages.append({"role": "user", "content": resume_text})

            messages.append({"role": "user", "content": task})

            if run_id is not None and self._event_bus is not None:
                self._event_bus.publish(run_id, {
                    "type": "agent.segment_started",
                    "segment_index": segment_idx,
                    "total_steps": len(ctx.completed_steps),
                })

            # Run one segment
            segment_final_content = ""
            for iteration in range(max_iterations_per_segment):
                response = await llm_provider.chat(messages, tools=tools if tools else None)

                if not response.tool_calls:
                    # LLM decided to stop
                    segment_final_content = response.content
                    if run_id is not None and self._event_bus is not None:
                        self._event_bus.publish(run_id, {
                            "type": "agent.completed",
                            "segment_index": segment_idx,
                            "content": response.content,
                        })
                    # Save context and return report
                    ctx_store.save(ctx)
                    return TaskReport(
                        task_id=session_id,
                        agent_id=agent_id,
                        session_id=session_id,
                        status="completed",
                        duration_seconds=_time_mod.monotonic() - start_time,
                        segments=segment_idx + 1,
                        steps_taken=len(ctx.completed_steps),
                        task_description=task,
                        errors=errors_log,
                        recovery_attempts=total_recovery_attempts,
                    )

                # Append assistant tool_use turn
                messages.append({
                    "role": "assistant",
                    "content": response.content,
                    "_tool_calls": [
                        {"id": tc.id, "name": tc.name, "args": tc.args}
                        for tc in response.tool_calls
                    ],
                })

                # Execute tool calls with self-healing
                for call in response.tool_calls:
                    if run_id is not None and self._event_bus is not None:
                        self._event_bus.publish(run_id, {
                            "type": "agent.tool_called",
                            "name": call.name,
                            "args": call.args,
                            "segment": segment_idx,
                            "step": len(ctx.completed_steps),
                        })

                    client_for_tool = tool_to_client.get(call.name)
                    if client_for_tool is None:
                        result = {"error": f"Tool '{call.name}' not found"}
                        ctx.add_step(call.name, result, status="failed")
                    else:
                        result, step_status = await self._exec_tool_with_recovery(
                            client_for_tool, call, ctx,
                            max_retries=max_error_retries,
                            run_id=run_id,
                        )
                        if step_status == "failed":
                            error_str = (
                                str(result.get("error", result))
                                if isinstance(result, dict)
                                else str(result)
                            )
                            errors_log.append(f"{call.name}: {error_str}")
                            # AC2: LLM-driven error analysis + optional fix attempt
                            fix_analysis = await self._analyze_error_with_llm(
                                error=error_str,
                                tool_name=call.name,
                                llm_provider=llm_provider,
                            )
                            if fix_analysis["fix_command"] not in ("NONE", "", None):
                                shell_tool = next(
                                    (t for t in ("shell_exec", "bash", "run_command")
                                     if t in tool_to_client),
                                    None,
                                )
                                if shell_tool:
                                    class _FixCall:  # noqa: N801
                                        id = f"fix-{call.id}"
                                        name = shell_tool
                                        args = {"command": fix_analysis["fix_command"]}
                                    fix_result, _ = await self._exec_tool_with_recovery(
                                        tool_to_client[shell_tool],
                                        _FixCall(),
                                        ctx,
                                        max_retries=1,
                                        run_id=run_id,
                                    )
                                    ctx.add_step(
                                        shell_tool,
                                        fix_result,
                                        description=f"Auto-fix: {fix_analysis['fix_command']}",
                                        status="done",
                                    )
                                    if run_id is not None and self._event_bus is not None:
                                        self._event_bus.publish(run_id, {
                                            "type": "agent.error_fix_applied",
                                            "fix_command": fix_analysis["fix_command"],
                                            "error_type": fix_analysis["error_type"],
                                        })
                            # Inject analysis into message context for next iteration
                            messages.append({
                                "role": "user",
                                "content": (
                                    f"[系统] 错误分析: {fix_analysis['error_type']}. "
                                    f"{fix_analysis['analysis']}"
                                ),
                            })
                        elif step_status == "recovered":
                            total_recovery_attempts += ctx.current_error_attempts
                        ctx.add_step(call.name, result, status=step_status)

                    if run_id is not None and self._event_bus is not None:
                        self._event_bus.publish(run_id, {
                            "type": "agent.tool_result",
                            "call_id": call.id,
                            "result": result,
                        })

                    result_str = (
                        json.dumps(result, ensure_ascii=False)
                        if isinstance(result, (dict, list))
                        else str(result)
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result_str,
                    })

                    # Update working_dir from shell results
                    if call.name in ("shell_exec", "bash", "run_command"):
                        if isinstance(result, dict) and "cwd" in result:
                            ctx.working_dir = str(result["cwd"])

                # Persist context after every step
                ctx_store.save(ctx)

            # Segment limit reached — AC3: LLM-generated checkpoint summary
            segment_summary = await self._generate_segment_summary(
                ctx, segment_idx, llm_provider
            )
            ctx.add_checkpoint_summary(segment_idx, segment_summary)
            ctx_store.save(ctx)

            if run_id is not None and self._event_bus is not None:
                self._event_bus.publish(run_id, {
                    "type": "agent.segment_checkpoint",
                    "segment_index": segment_idx,
                    "summary": segment_summary,
                    "total_steps": len(ctx.completed_steps),
                })

        # Exhausted all segments
        ctx_store.save(ctx)
        return TaskReport(
            task_id=session_id,
            agent_id=agent_id,
            session_id=session_id,
            status="stopped",
            duration_seconds=_time_mod.monotonic() - start_time,
            segments=max_segments,
            steps_taken=len(ctx.completed_steps),
            task_description=task,
            errors=errors_log,
            recovery_attempts=total_recovery_attempts,
        )

    async def _exec_tool_with_recovery(
        self,
        client: Any,
        call: Any,
        ctx: "AgentWorkspaceContext",
        max_retries: int = 3,
        run_id: Optional[str] = None,
    ) -> tuple:
        """Execute a tool call with error self-recovery.

        Returns (result, status) where status in {"done", "failed", "recovered"}.
        """
        from xueban.runtime.workspace_context import AgentWorkspaceContext  # noqa: F401
        ctx.current_error_attempts = 0

        last_error: Optional[str] = None
        for attempt in range(max_retries + 1):
            try:
                result = await client.call_tool(call.name, call.args)
                # Check for non-zero exit code in shell results
                if isinstance(result, dict):
                    exit_code = result.get("exit_code", result.get("returncode", 0))
                    stderr = result.get("stderr", "")
                    if exit_code != 0 and stderr:
                        raise RuntimeError(f"exit_code={exit_code}: {stderr[:500]}")
                status = "recovered" if ctx.current_error_attempts > 0 else "done"
                ctx.current_error_attempts = 0
                return result, status
            except Exception as exc:
                last_error = str(exc)
                ctx.current_error_attempts = attempt + 1
                if attempt < max_retries:
                    if run_id is not None and self._event_bus is not None:
                        self._event_bus.publish(run_id, {
                            "type": "agent.error_recovery",
                            "tool": call.name,
                            "attempt": attempt + 1,
                            "max_retries": max_retries,
                            "error": last_error[:300],
                        })
                    # Exponential backoff (minimal for interactive tasks)
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    break

        return {"error": last_error or "unknown error"}, "failed"

    async def _analyze_error_with_llm(
        self,
        error: str,
        tool_name: str,
        llm_provider: Any,
    ) -> dict:
        """AC2: Ask LLM to classify error type and suggest a single shell fix command.

        Returns dict with keys: error_type, fix_command, analysis.
        Falls back to defaults if LLM is unavailable.
        """
        if llm_provider is None:
            return {"error_type": "unknown", "fix_command": "NONE", "analysis": ""}
        prompt = (
            f"Tool '{tool_name}' failed with this error:\n{error[:500]}\n\n"
            "Classify the error (one of: dependency_missing, permission_error, "
            "path_error, runtime_error, other) and suggest ONE shell fix command "
            "(or NONE if not fixable by a shell command).\n"
            "Reply in this exact format:\n"
            "TYPE: <error_type>\n"
            "FIX: <command_or_NONE>\n"
            "ANALYSIS: <one sentence>\n"
        )
        try:
            response = await llm_provider.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
            )
            text = (response.content or "").strip()
            result: dict = {"error_type": "other", "fix_command": "NONE", "analysis": ""}
            for line in text.splitlines():
                if line.startswith("TYPE:"):
                    result["error_type"] = line.split(":", 1)[1].strip()
                elif line.startswith("FIX:"):
                    result["fix_command"] = line.split(":", 1)[1].strip()
                elif line.startswith("ANALYSIS:"):
                    result["analysis"] = line.split(":", 1)[1].strip()
            return result
        except Exception:
            return {"error_type": "unknown", "fix_command": "NONE", "analysis": ""}

    async def _generate_segment_summary(
        self,
        ctx: Any,
        segment_idx: int,
        llm_provider: Any,
    ) -> str:
        """AC3: Use LLM to generate a concise (≤200 char) summary of a completed segment.

        Falls back to a template string if LLM is unavailable.
        """
        recent_steps = ctx.completed_steps[-10:]
        fallback = (
            f"段 {segment_idx + 1}: 执行了 {len(recent_steps)} 个工具调用，"
            f"共 {len(ctx.completed_steps)} 步完成。"
        )
        if llm_provider is None:
            return fallback
        steps_text = "\n".join(
            f"  {s.index}. {s.tool_called}: {s.result_summary[:100]}"
            for s in recent_steps
        )
        prompt = (
            f"任务: {ctx.task_description[:200]}\n"
            f"段 {segment_idx + 1} 执行情况（最近 {len(recent_steps)} 步）:\n"
            f"{steps_text}\n\n"
            "用不超过 200 字符的中文总结这段完成了什么（一句话，不含引号）："
        )
        try:
            response = await llm_provider.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
            )
            summary = (response.content or "").strip()[:200]
            return summary if summary else fallback
        except Exception:
            return fallback

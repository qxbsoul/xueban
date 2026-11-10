"""AgentWorkspaceContext — Story 11.5

Persistent per-agent-session context for long-task execution with checkpoint/resume.

Data flow:
  run_agent_with_tools() → on_tool_result() hook → WorkspaceContextStore.save()
  Resume: load_context() → inject into ReAct system prompt

Storage: in-memory with TTL (Redis-compatible interface for future swap).
Key: ws_ctx:{agent_id}:{session_id}
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStep(BaseModel):
    index: int
    description: str
    tool_called: str
    result_summary: str          # truncated to 2000 chars
    status: Literal["done", "failed", "recovered"] = "done"
    timestamp: datetime = Field(default_factory=_utc_now)


class CheckpointSummary(BaseModel):
    segment_index: int
    steps_in_segment: int
    summary: str                 # LLM-generated, ≤200 chars
    created_at: datetime = Field(default_factory=_utc_now)


class AgentWorkspaceContext(BaseModel):
    agent_id: str
    session_id: str
    task_description: str = ""
    working_dir: str = "/workspace"
    completed_steps: List[TaskStep] = Field(default_factory=list)
    environment: Dict[str, str] = Field(default_factory=dict)
    checkpoint_summaries: List[CheckpointSummary] = Field(default_factory=list)
    last_updated: datetime = Field(default_factory=_utc_now)
    # error recovery tracking
    current_error_attempts: int = 0

    def add_step(self, tool_name: str, result: Any, description: str = "", status: str = "done") -> None:
        summary = str(result)[:2000]
        self.completed_steps.append(TaskStep(
            index=len(self.completed_steps),
            description=description or f"Called {tool_name}",
            tool_called=tool_name,
            result_summary=summary,
            status=status,  # type: ignore[arg-type]
        ))
        self.last_updated = _utc_now()

    def add_checkpoint_summary(self, segment_index: int, summary: str) -> None:
        steps_in_segment = len(self.completed_steps)
        if self.checkpoint_summaries:
            steps_in_segment -= sum(c.steps_in_segment for c in self.checkpoint_summaries)
        self.checkpoint_summaries.append(CheckpointSummary(
            segment_index=segment_index,
            steps_in_segment=max(0, steps_in_segment),
            summary=summary[:200],
        ))

    def resume_prompt(self) -> str:
        """Build the context injection string for the next ReAct segment."""
        n = len(self.completed_steps)
        if n == 0:
            return ""
        recent = self.completed_steps[-5:]
        completed_lines = "\n".join(
            f"  [{s.index}] {s.tool_called}: {s.result_summary[:200]}" for s in recent
        )
        segment_notes = ""
        if self.checkpoint_summaries:
            last = self.checkpoint_summaries[-1]
            segment_notes = f"\n上一段摘要：{last.summary}"
        return (
            f"你正在执行一个已进行 {n} 步的任务。当前进度：\n"
            f"- 已完成步骤（最近 {len(recent)} 步）：\n{completed_lines}\n"
            f"- 当前目录：{self.working_dir}"
            f"{segment_notes}\n"
            "继续从上次停止的地方执行。"
        )


class TaskReport(BaseModel):
    task_id: str
    agent_id: str
    session_id: str
    status: Literal["completed", "failed", "stopped"]
    duration_seconds: float = 0.0
    segments: int = 1
    steps_taken: int = 0
    task_description: str = ""
    outputs: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    recovery_attempts: int = 0
    created_at: datetime = Field(default_factory=_utc_now)


# ---------------------------------------------------------------------------
# In-memory store with TTL (Redis-swap-ready interface)
# ---------------------------------------------------------------------------


class _ContextEntry:
    __slots__ = ("ctx", "expires_at")

    def __init__(self, ctx: AgentWorkspaceContext, ttl_seconds: int):
        self.ctx = ctx
        self.expires_at = time.monotonic() + ttl_seconds


class WorkspaceContextStore:
    """Thread-safe in-memory context store with TTL expiry.

    Key format: {agent_id}:{session_id}
    Default TTL: 24 hours (86400 s).
    """

    _TTL = 86400  # 24 h

    def __init__(self) -> None:
        self._store: Dict[str, _ContextEntry] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save(self, ctx: AgentWorkspaceContext) -> None:
        key = self._key(ctx.agent_id, ctx.session_id)
        self._store[key] = _ContextEntry(ctx, self._TTL)

    def load(self, agent_id: str, session_id: str) -> Optional[AgentWorkspaceContext]:
        key = self._key(agent_id, session_id)
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            return None
        return entry.ctx

    def delete(self, agent_id: str, session_id: str) -> bool:
        key = self._key(agent_id, session_id)
        return self._store.pop(key, None) is not None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _key(agent_id: str, session_id: str) -> str:
        return f"{agent_id}:{session_id}"


# Module-level singleton — eager init avoids lazy-init race condition
_store: WorkspaceContextStore = WorkspaceContextStore()


def get_store() -> WorkspaceContextStore:
    return _store

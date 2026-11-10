"""ACP Agent Registry — Story 2.10.

Stores AgentCapabilityManifest for both native and external agents.
Provides capability-aware task routing and health tracking.

In-memory by default.  When REDIS_URL env var is set, heartbeat TTL keys
are written to Redis so crash-detection survives API restarts (D1).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30  # seconds — expected heartbeat cadence
_HEARTBEAT_TTL = _HEARTBEAT_INTERVAL * 2  # 60s — 2-miss threshold
_ROUTING_LOG_MAXLEN = 1000


# ---------------------------------------------------------------------------
# Redis helper (optional — D1)
# ---------------------------------------------------------------------------


def _get_redis():  # type: ignore[return]
    """Return an aioredis client if REDIS_URL is set, else None."""
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis.asyncio as aioredis  # type: ignore[import]
        return aioredis.from_url(redis_url, decode_responses=True)
    except ImportError:
        logger.warning("REDIS_URL set but redis package not installed; falling back to in-memory")
        return None


# ---------------------------------------------------------------------------
# Custom exceptions (P7)
# ---------------------------------------------------------------------------


class NoAvailableAgentError(Exception):
    """Raised by route_task() when no suitable agent can be found."""

    def __init__(self, subtask: str, required_tools: Optional[List[str]] = None) -> None:
        self.subtask = subtask
        self.required_tools = required_tools or []
        super().__init__(
            f"No available agent for subtask={subtask!r} required_tools={self.required_tools}"
        )


# ---------------------------------------------------------------------------
# Manifest data models
# ---------------------------------------------------------------------------


class ToolCapability(BaseModel):
    name: str
    description: str
    input_schema: Optional[Dict[str, Any]] = None


class MemoryCapability(BaseModel):
    type: Literal["stateless", "stateful"] = "stateless"
    scope: Literal["request", "session", "persistent"] = "request"
    persistence: bool = False


class AgentCapabilityManifest(BaseModel):
    agent_id: str
    display_name: str
    version: str = "unknown"
    tools: List[ToolCapability] = Field(default_factory=list)
    max_concurrency: int = Field(default=1, ge=1)  # P12: lower bound
    streaming: bool = False
    memory: MemoryCapability = Field(default_factory=MemoryCapability)
    protocols: List[str] = Field(default_factory=lambda: ["acp-v1"])
    workspace_context: bool = False

    @property
    def tool_names(self) -> List[str]:
        return [t.name for t in self.tools]

    @classmethod
    def default_native(cls, agent_blueprint_id: str) -> "AgentCapabilityManifest":
        """Auto-generated manifest for native Xueban agents."""
        return cls(
            agent_id=f"native:{agent_blueprint_id}",
            display_name=f"Xueban Agent ({agent_blueprint_id})",
            tools=[
                ToolCapability(name="xueban-shell", description="Execute bash commands"),
                ToolCapability(name="xueban-fs", description="File system read/write"),
                ToolCapability(name="xueban-web", description="Fetch web pages"),
            ],
            max_concurrency=1,
            streaming=True,
            memory=MemoryCapability(type="stateful", scope="session", persistence=True),
            workspace_context=True,
        )

    @classmethod
    def default_legacy(cls, agent_id: str) -> "AgentCapabilityManifest":
        """Fallback manifest for agents that don't support capability declaration."""
        return cls(
            agent_id=agent_id,
            display_name=agent_id,
            tools=[ToolCapability(name="shell", description="Execute bash commands")],
            max_concurrency=1,
            streaming=False,
        )


class AgentRegistryEntry(BaseModel):
    model_config = ConfigDict(validate_assignment=True)  # P19: re-validate on field set

    manifest: AgentCapabilityManifest
    status: Literal["online", "offline", "busy"] = "offline"
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    active_tasks: int = 0
    team_memberships: List[str] = Field(default_factory=list)
    is_native: bool = False
    missed_heartbeats: int = 0  # P6: consecutive missed-heartbeat counter

    @property
    def available_slots(self) -> int:
        return max(0, self.manifest.max_concurrency - self.active_tasks)

    def update_heartbeat(self) -> None:
        self.last_heartbeat = datetime.now(timezone.utc)
        self.missed_heartbeats = 0
        if self.status == "offline":
            self.status = "online"


# ---------------------------------------------------------------------------
# Task routing log
# ---------------------------------------------------------------------------


class TaskRoutingLog(BaseModel):
    log_id: str = Field(default_factory=lambda: uuid4().hex[:16])  # P23: 16-char entropy
    task_id: str
    subtask: str
    routed_to: str
    reason: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Agent Registry service (singleton)
# ---------------------------------------------------------------------------


class AgentRegistry:
    """In-process agent registry with optional Redis heartbeat persistence."""

    def __init__(self) -> None:
        self._entries: Dict[str, AgentRegistryEntry] = {}
        self._routing_log: collections.deque = collections.deque(maxlen=_ROUTING_LOG_MAXLEN)  # P5
        self._lock = asyncio.Lock()
        self._redis = _get_redis()  # D1: optional Redis client
        self._monitor_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Lifecycle (P6: background heartbeat monitor)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background heartbeat monitor. Call from app startup."""
        self._monitor_task = asyncio.create_task(self._heartbeat_monitor())

    async def stop(self) -> None:
        """Cancel background monitor. Call from app shutdown."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def _heartbeat_monitor(self) -> None:
        """Periodically detect 2-consecutive-miss heartbeat timeouts (P6)."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            now = datetime.now(timezone.utc)
            async with self._lock:
                for agent_id, entry in list(self._entries.items()):
                    if entry.status == "offline":
                        continue
                    age = (now - entry.last_heartbeat).total_seconds()
                    if age > _HEARTBEAT_TTL:
                        entry.missed_heartbeats += 1
                        if entry.missed_heartbeats >= 2:
                            entry.status = "offline"
                            logger.warning(
                                "ACP registry: agent_id=%s offline after %d missed heartbeats",
                                agent_id, entry.missed_heartbeats,
                            )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(
        self,
        manifest: AgentCapabilityManifest,
        *,
        is_native: bool = False,
    ) -> AgentRegistryEntry:
        async with self._lock:
            entry = AgentRegistryEntry(
                manifest=manifest,
                status="online",
                connected_at=datetime.now(timezone.utc),
                last_heartbeat=datetime.now(timezone.utc),
                is_native=is_native,
            )
            self._entries[manifest.agent_id] = entry
            logger.info("ACP registry: registered agent_id=%s native=%s", manifest.agent_id, is_native)
        # D1: write heartbeat TTL key to Redis outside lock (I/O)
        if self._redis:
            try:
                await self._redis.setex(f"agent_heartbeat:{manifest.agent_id}", _HEARTBEAT_TTL, "1")
            except Exception as exc:
                logger.warning("ACP registry: Redis write failed agent_id=%s: %s", manifest.agent_id, exc)
        return entry

    async def unregister(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id in self._entries:
                self._entries[agent_id].status = "offline"
                logger.info("ACP registry: agent_id=%s marked offline", agent_id)

    async def mark_offline(self, agent_id: str) -> None:
        await self.unregister(agent_id)

    async def mark_online(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id in self._entries:
                entry = self._entries[agent_id]
                entry.update_heartbeat()
                # P17: preserve "busy" if tasks are running
                if entry.active_tasks > 0:
                    entry.status = "busy"
                else:
                    entry.status = "online"

    async def update_heartbeat(self, agent_id: str, reported_active_tasks: int = 0) -> None:
        async with self._lock:
            if agent_id in self._entries:
                entry = self._entries[agent_id]
                entry.update_heartbeat()
                # P11: server counter is authoritative; only accept client value if >= server's
                safe_tasks = max(0, reported_active_tasks)
                if safe_tasks < entry.active_tasks:
                    logger.warning(
                        "ACP registry: agent_id=%s heartbeat active_tasks=%d < server=%d; "
                        "keeping server value",
                        agent_id, safe_tasks, entry.active_tasks,
                    )
                else:
                    entry.active_tasks = safe_tasks
                entry.status = "busy" if entry.active_tasks > 0 else "online"
        # D1: refresh Redis TTL
        if self._redis:
            try:
                await self._redis.setex(f"agent_heartbeat:{agent_id}", _HEARTBEAT_TTL, "1")
            except Exception as exc:
                logger.warning("ACP registry: Redis heartbeat refresh failed agent_id=%s: %s", agent_id, exc)

    # ------------------------------------------------------------------
    # Queries (P4: all reads under lock)
    # ------------------------------------------------------------------

    async def get(self, agent_id: str) -> Optional[AgentRegistryEntry]:
        async with self._lock:
            return self._entries.get(agent_id)

    async def list_all(self) -> List[AgentRegistryEntry]:
        async with self._lock:
            return list(self._entries.values())

    async def list_online(self, agent_ids: Optional[List[str]] = None) -> List[AgentRegistryEntry]:
        async with self._lock:
            entries = list(self._entries.values())
            if agent_ids is not None:
                entries = [e for e in entries if e.manifest.agent_id in agent_ids]
            return [e for e in entries if e.status in ("online", "busy")]

    # ------------------------------------------------------------------
    # Capability-aware routing (P1 TOCTOU fix, P7 error, P8 streaming)
    # ------------------------------------------------------------------

    async def route_task(
        self,
        task_id: str,
        subtask_description: str,
        required_tools: Optional[List[str]] = None,
        team_agent_ids: Optional[List[str]] = None,
        prefer_streaming: bool = False,  # P8: streaming routing dimension
    ) -> AgentRegistryEntry:
        """Return best candidate agent. Raises NoAvailableAgentError if none found.

        P1: entire route+increment is atomic under _lock to prevent TOCTOU.
        """
        async with self._lock:
            entries = list(self._entries.values())
            if team_agent_ids is not None:
                entries = [e for e in entries if e.manifest.agent_id in team_agent_ids]
            candidates = [e for e in entries if e.status in ("online", "busy")]

            # 1. Tool-match filter
            if required_tools:
                candidates = [
                    a for a in candidates
                    if all(t in a.manifest.tool_names for t in required_tools)
                ]

            # 2. Concurrency-slot filter
            candidates = [a for a in candidates if a.available_slots > 0]

            if not candidates:
                raise NoAvailableAgentError(subtask_description, required_tools)  # P7

            # 3. Sort: native-first, streaming preference (P8), fewest active tasks
            def _sort_key(a: AgentRegistryEntry) -> tuple:
                streaming_penalty = 0 if (not prefer_streaming or a.manifest.streaming) else 1
                return (0 if a.is_native else 1, streaming_penalty, a.active_tasks)

            candidates.sort(key=_sort_key)
            winner = candidates[0]

            # P1: atomically claim a slot before releasing lock
            winner.active_tasks += 1
            winner.status = "busy"

            # P24: sanitize tool names — replace commas to avoid parsing ambiguity
            if required_tools:
                safe_tools = [t.replace(",", ";") for t in required_tools]
                reason = "tool_match:" + ",".join(safe_tools)
            else:
                reason = "default"
            if prefer_streaming and winner.manifest.streaming:
                reason += "+streaming"

            log = TaskRoutingLog(
                task_id=task_id,
                subtask=subtask_description,
                routed_to=winner.manifest.agent_id,
                reason=reason,
            )
            self._routing_log.append(log)  # P5: deque auto-trims at maxlen

        return winner

    # ------------------------------------------------------------------
    # Routing log queries (P22: under lock)
    # ------------------------------------------------------------------

    async def get_routing_log(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[TaskRoutingLog]:
        async with self._lock:  # P22: snapshot under lock
            snapshot = list(self._routing_log)
        return snapshot[offset: offset + limit]

    # ------------------------------------------------------------------
    # Task counters
    # ------------------------------------------------------------------

    async def increment_task(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id in self._entries:
                self._entries[agent_id].active_tasks += 1
                self._entries[agent_id].status = "busy"

    async def decrement_task(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id in self._entries:
                entry = self._entries[agent_id]
                entry.active_tasks = max(0, entry.active_tasks - 1)
                # P16: restore online for any non-offline status (not just "busy")
                if entry.active_tasks == 0 and entry.status != "offline":
                    entry.status = "online"

    # ------------------------------------------------------------------
    # Refresh (re-handshake trigger — P2: under lock)
    # ------------------------------------------------------------------

    async def refresh_agent(self, agent_id: str) -> bool:
        """Mark an agent for re-handshake. Returns False if not found."""
        async with self._lock:  # P2: acquire lock before any mutation
            entry = self._entries.get(agent_id)
            if not entry:
                return False
            entry.status = "offline"
            logger.info("ACP registry: agent_id=%s queued for re-handshake", agent_id)
            return True


# ---------------------------------------------------------------------------
# Module-level singleton (P3: eager init — no double-check race at import time)
# ---------------------------------------------------------------------------

_registry: AgentRegistry = AgentRegistry()


def get_registry() -> AgentRegistry:
    return _registry

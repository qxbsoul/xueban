from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from xueban.runtime.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentHandle,
    AgentTask,
)

from xueban.runtime.events import AgentEventType

logger = logging.getLogger(__name__)

_JSONL_TYPE_MAP: Dict[str, str] = {
    "assistant": AgentEventType.OUTPUT,
    "done": AgentEventType.COMPLETED,
    "error": AgentEventType.FAILED,
    "thinking": AgentEventType.THINKING,
    "tool_call": AgentEventType.TOOL_CALLED,
    "tool_result": AgentEventType.TOOL_RESULT,
}

DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


def compile_execution_prompt(payload: Dict[str, Any]) -> str:
    prompt = str(payload.get("prompt") or "").strip()
    step_input = payload.get("step_input", {})
    context = payload.get("context", {})

    prompt_sections: List[str] = []
    if prompt:
        prompt_sections.append(prompt)
    if step_input:
        prompt_sections.append(
            "Workflow input:\n" + json.dumps(step_input, ensure_ascii=False, indent=2, default=str)
        )
    if context:
        prompt_sections.append(
            "Runtime context:\n" + json.dumps(context, ensure_ascii=False, indent=2, default=str)
        )
    return "\n\n".join(section for section in prompt_sections if section).strip()


def extract_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in reversed(value):
            text = extract_text_content(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in (
            "output_text",
            "text",
            "message",
            "content",
            "result",
            "final",
            "response",
            "completion",
            "last_message",
        ):
            if key in value:
                text = extract_text_content(value[key])
                if text:
                    return text
        for key in ("item", "items", "output", "events", "data"):
            if key not in value:
                continue
            text = extract_text_content(value[key])
            if text:
                return text
        for key in ("delta",):
            if key not in value:
                continue
            text = extract_text_content(value[key])
            if text:
                return text
        for item in value.values():
            if not isinstance(item, (dict, list)):
                continue
            text = extract_text_content(item)
            if text:
                return text
        return ""
    return str(value)


class BaseExecutor(ABC):
    kind: str

    @abstractmethod
    async def execute(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class UnknownExecutorError(Exception):
    """Raised when resolve(kind, provider) finds no registered AgentExecutor."""

    def __init__(self, kind: str, provider: str, available: List[Tuple[str, str]]) -> None:
        self.kind = kind
        self.provider = provider
        self.available = available
        pairs = ", ".join(f"({k}, {p})" for k, p in sorted(available)) or "(none)"
        super().__init__(
            f"No AgentExecutor registered for kind={kind!r} provider={provider!r}. "
            f"Available: {pairs}"
        )


class AgentTeamsExecutor(BaseExecutor):
    """AgentTeams collaboration executor (LE-2 seam; design §6).

    Maps a loop-body dispatch to an AgentTeams Manager-Worker team. Real dispatch
    applies a Team CR to the agentteams-controller and bridges Matrix room events;
    that requires a Kubernetes cluster + Matrix client and is gated behind
    ``dry_run=False``. In dry_run (default) it returns a deterministic team plan so
    loop workflows are executable and testable without those backends.
    """

    kind = "agentteams"

    async def execute(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        provider = config.get("provider", "default")
        dry_run = bool(config.get("dry_run", True))
        step_input = (payload or {}).get("step_input", {}) if isinstance(payload, dict) else {}
        goal = (payload or {}).get("prompt") or step_input.get("goal") or step_input.get("message") or ""
        team_spec = {
            "provider": provider,
            "runtime": config.get("runtime", "hermes"),
            "workers": config.get("workers", []),
            "skills": config.get("skills", []),
        }
        if not dry_run:
            # Real dispatch: render Team CR + dispatch the goal to a Matrix room (the team's
            # collaboration hub). Requires MATRIX_HOMESERVER (+ MATRIX_USER/MATRIX_PASSWORD or
            # MATRIX_TOKEN); without it we fail fast rather than silently stub.
            from xueban.runtime.agentteams_bridge import MatrixClient, render_team_cr

            homeserver = os.environ.get("MATRIX_HOMESERVER")
            if not homeserver:
                raise RuntimeError(
                    "AgentTeams real dispatch requires MATRIX_HOMESERVER "
                    "(+ MATRIX_USER/MATRIX_PASSWORD or MATRIX_TOKEN). Set executor.dry_run=true "
                    "for the deterministic stub. See loop-engine-design.md §6."
                )
            team_cr = render_team_cr(team_spec)
            client = MatrixClient(
                homeserver,
                os.environ.get("MATRIX_USER", "xueban"),
                password=os.environ.get("MATRIX_PASSWORD"),
                token=os.environ.get("MATRIX_TOKEN"),
            )
            if client.token is None:
                await client.login()
            room_id = await client.create_room(name=f"xueban-{provider}", topic=str(goal)[:200])
            event_id = await client.send_message(room_id, str(goal))
            return {
                "message": f"[agentteams:{provider}] dispatched team to room {room_id}",
                "handled_by": f"agentteams:{provider}",
                "node_type": "agentteams",
                "team_spec": team_spec,
                "team_cr": team_cr,
                "matrix_room": room_id,
                "dispatched_event": event_id,
                "dry_run": False,
                "result": f"delegated goal to AgentTeams team [{provider}] in room {room_id}",
            }
        return {
            "message": f"[agentteams:{provider}] dry-run team plan for: {goal}",
            "handled_by": f"agentteams:{provider}",
            "node_type": "agentteams",
            "team_spec": team_spec,
            "matrix_room": f"!dryrun-{provider}:matrix.local",
            "dry_run": True,
            "result": f"(dry-run) delegated goal to AgentTeams team [{provider}]",
        }


class AgentExecutor(ABC):
    """Universal agent plugin contract (Story 2.1 / AR47).

    Parallel to BaseExecutor — existing CliExecutor/ApiExecutor paths are untouched.
    Story 2.2 will migrate CLI agents over; for now this is the pure ABC.
    """

    kind: str  # one of Kind = Literal["api", "cli", "mcp", "acp"]
    provider: str

    @abstractmethod
    async def dispatch(self, task: AgentTask) -> AgentHandle:
        """Dispatch a task and return a handle."""

    @abstractmethod
    def stream_events(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        """Stream AgentEvent objects for an active handle."""

    @abstractmethod
    def capabilities(self) -> AgentCapabilities:
        """Return AgentCapabilities describing what this executor supports."""


def _interpolate_args(args_template: List[str], context: Dict[str, str]) -> List[str]:
    """Interpolate {id}/{stdin}/{run_id} placeholders in args_template."""
    result = []
    for arg in args_template:
        for k, v in context.items():
            arg = arg.replace("{" + k + "}", v)
        result.append(arg)
    return result


class CliAgentExecutor(AgentExecutor):
    """Preset-driven CLI AgentExecutor (Story 2.2 / AR48).

    Replaces CliExecutor's hardcoded provider branches with provider_presets.yaml.
    Old CliExecutor is preserved as a compatibility shim.
    """

    kind = "cli"

    def __init__(self, provider: str, preset: Any) -> None:
        self.provider = provider
        self._preset = preset  # ProviderPreset instance

    async def dispatch(self, task: AgentTask) -> AgentHandle:
        preset = self._preset
        # Build interpolation context
        context: Dict[str, str] = {
            "id": task.agent_id,
            "run_id": task.run_id,
            "stdin": "",
        }
        # Prepare stdin payload + fill context["stdin"] for args_template interpolation.
        # stdin_format="none" still needs context["stdin"] populated so that presets like
        # claude (args_template has "{stdin}" but passes prompt via -p flag, not stdin)
        # get the prompt text into their args. Only the subprocess stdin channel differs.
        stdin_payload: Optional[str] = None
        if preset.stdin_format == "json":
            stdin_payload = json.dumps(task.payload, ensure_ascii=False)
            context["stdin"] = stdin_payload
        elif preset.stdin_format == "raw":
            stdin_payload = compile_execution_prompt(task.payload)
            context["stdin"] = stdin_payload
        else:  # "none" — prompt goes into args only, not subprocess stdin
            context["stdin"] = compile_execution_prompt(task.payload)

        args = _interpolate_args(preset.args_template, context)
        command = [*preset.command, *args]

        # Interpolate workspace_template ({id}/{run_id}) for downstream JSONL tail.
        workspace = preset.workspace_template
        if workspace:
            workspace = _interpolate_args([workspace], context)[0]

        # Merge env
        env = {**os.environ, **preset.env} if preset.env else None

        # Health-check: if the binary isn't found, degrade gracefully (Story 2.5).
        binary = command[0]
        if not shutil.which(binary):
            return AgentHandle(
                run_id=task.run_id,
                node_id=task.node_id,
                agent_id=task.agent_id,
                status="degraded",
                metadata={
                    "parse_format": preset.parse_format,
                    "workspace_template": workspace,
                    "_degraded": True,
                    "_degraded_reason": f"binary {binary!r} not found in PATH",
                    "_provider": self.provider,
                },
            )

        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                input=stdin_payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"cli agent executor: command not found for provider={self.provider}: {command[0]}"
            ) from exc

        handle_meta: Dict[str, Any] = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "parse_format": preset.parse_format,
            "workspace_template": workspace,
        }
        status = "done" if completed.returncode == 0 else "failed"
        return AgentHandle(
            run_id=task.run_id,
            node_id=task.node_id,
            agent_id=task.agent_id,
            status=status,
            metadata=handle_meta,
        )

    async def stream_events(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        # Degraded path: binary was missing at dispatch time (Story 2.5).
        # Check handle.status (first-class field) rather than metadata flags
        # so middleware scrubbing metadata cannot cause silent success.
        if handle.status == "degraded":
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.degraded",
                payload={
                    "reason": handle.metadata.get("_degraded_reason", "binary not found"),
                    "provider": handle.metadata.get("_provider", self.provider),
                    "fallback_chain": ["api:claude"],
                },
            )
            return

        parse_format = handle.metadata.get("parse_format", "stdout-text")
        stdout: str = handle.metadata.get("stdout", "")
        returncode: int = handle.metadata.get("returncode", 0)

        if returncode != 0:
            yield AgentEvent(
                run_id=handle.run_id,
                node_id=handle.node_id,
                agent_id=handle.agent_id,
                type="agent.failed",
                payload={"stderr": handle.metadata.get("stderr", ""), "returncode": returncode},
            )
            return

        if parse_format == "jsonl-tail":
            async for event in self._stream_stdout_jsonl(handle, stdout):
                yield event
        elif parse_format in ("stdout-json", "codex-jsonl"):
            async for event in self._stream_structured(handle, stdout, parse_format):
                yield event
        else:
            yield AgentEvent(
                run_id=handle.run_id,
                node_id=handle.node_id,
                agent_id=handle.agent_id,
                type="agent.output",
                payload={"text": stdout},
            )

    async def _stream_stdout_jsonl(self, handle: Any, stdout: str) -> AsyncIterator[Any]:
        """Phase 1 降级实现:按行 split 已捕获的 stdout JSONL。

        原 spec 为 `_stream_jsonl_tail(session_path)` 异步尾追文件
        (`~/.openclaw/agents/{id}/sessions/*.jsonl`),降级详情见
        Story 2.2 Review Findings (2026-04-22)。真实文件尾追推迟到 Phase 2。
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event_data = json.loads(line)
            except json.JSONDecodeError:
                event_data = {"text": line}
            raw_type = event_data.get("type", "output")
            canonical_type = _JSONL_TYPE_MAP.get(raw_type, AgentEventType.OUTPUT)
            yield AgentEvent(
                run_id=handle.run_id,
                node_id=handle.node_id,
                agent_id=handle.agent_id,
                type=canonical_type,
                payload=event_data,
            )

    async def _stream_structured(self, handle: AgentHandle, stdout: str, parse_format: str) -> AsyncIterator[AgentEvent]:
        text = stdout.strip()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {"text": text}
        yield AgentEvent(
            run_id=handle.run_id,
            node_id=handle.node_id,
            agent_id=handle.agent_id,
            type="agent.output",
            payload=data if isinstance(data, dict) else {"result": data},
        )

    def capabilities(self) -> AgentCapabilities:
        parse_format = getattr(self._preset, "parse_format", "stdout-text")
        return AgentCapabilities(
            streaming=parse_format == "jsonl-tail",
            approval_required=False,
            session_resume=False,
            tool_calls=False,
        )


class AcpAgentExecutor(AgentExecutor):
    """ACP (Agent Client Protocol) executor — host role via stdio JSON-RPC (Story 2.3 / AR56).

    Spawns the agent as a subprocess, performs ACP handshake, then streams events.
    """

    kind = "acp"

    def __init__(self, provider: str, command: List[str]) -> None:
        self.provider = provider
        self._command = command
        # Session registry: handle_id → (transport, client).
        # Live transport/client objects are NOT JSON-serializable so they must
        # NOT live in AgentHandle.metadata (which Pydantic model_dump_json'd for
        # SSE broadcast / checkpoint / audit log). Keep them here, addressed by
        # the handle_id that metadata carries.
        self._sessions: Dict[str, Any] = {}

    async def dispatch(self, task: AgentTask) -> AgentHandle:
        from xueban.runtime.acp.transport import AcpTransport
        from xueban.runtime.acp.client import AcpClient

        transport = AcpTransport(self._command)
        try:
            await transport.start()
            client = AcpClient(transport)
            await client.initialize()
            await client.start_session(
                run_id=task.run_id,
                node_id=task.node_id,
                agent_id=task.agent_id,
                provenance=(task.payload or {}).get("provenance")
                if isinstance(task.payload, dict)
                else None,
            )
            prompt = compile_execution_prompt(task.payload)
            await client.prompt(prompt, context=task.metadata)
        except Exception as exc:
            await transport.stop()
            raise ValueError(f"ACP dispatch failed for provider={self.provider}: {exc}") from exc

        handle = AgentHandle(
            run_id=task.run_id,
            node_id=task.node_id,
            agent_id=task.agent_id,
            status="running",
            metadata={
                "session_id": client.session_id,
                "provider": self.provider,
            },
        )
        self._sessions[handle.handle_id] = (transport, client)
        return handle

    async def stream_events(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        from xueban.runtime.acp.transport import AcpSessionTerminated

        session = self._sessions.get(handle.handle_id)
        if session is None:
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.failed",
                payload={"error": f"no ACP session in registry for handle {handle.handle_id}"},
            )
            return
        transport, client = session
        try:
            async for event in client.stream_events(handle):
                yield event
        except AcpSessionTerminated as exc:
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.failed",
                payload={"exit_code": exc.exit_code, "stderr": exc.stderr_tail},
            )
        finally:
            # Phase 5 patch: stream_events.finally unconditionally stops the
            # transport, so session_resume is NOT actually supported today.
            # capabilities() is aligned to return session_resume=False.
            self._sessions.pop(handle.handle_id, None)
            await transport.stop()

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            streaming=True,
            approval_required=True,
            # Aligned with real stream_events.finally behavior (stops transport).
            # True resume requires keeping the session alive across stream_events
            # invocations — tracked as future work in Story 2.3 Review Findings.
            session_resume=False,
            tool_calls=True,
        )


class McpAgentExecutor(AgentExecutor):
    """MCP tool-call executor — single-shot tool invocation via MCP SDK (Story 2.4 / AR53).

    Complements AcpAgentExecutor: ACP manages sessions, MCP handles one-off tool calls.
    """

    kind = "mcp"

    def __init__(
        self,
        provider: str,
        default_server: str = "",
        default_tool: str = "run_agent",
    ) -> None:
        self.provider = provider
        self._default_server = default_server
        self._default_tool = default_tool
        # Session registry: handle_id → client. See AcpAgentExecutor for rationale.
        self._sessions: Dict[str, Any] = {}

    # Allow injection of a custom client factory for testing
    def _make_client(self, config: Any) -> Any:
        from xueban.runtime.mcp.client import McpClient
        return McpClient(config)

    async def dispatch(self, task: AgentTask) -> AgentHandle:
        from xueban.runtime.mcp.transport import McpTransportConfig
        from xueban.runtime.errors import McpError

        server = str(task.metadata.get("server") or self._default_server)
        tool_name = str(task.metadata.get("tool") or self._default_tool)

        if not server:
            raise McpError(
                code="MCP_SERVER_UNAVAILABLE",
                detail="No MCP server specified; set executor.server or default_server",
            )

        try:
            config = McpTransportConfig.parse(server)
        except ValueError as exc:
            raise McpError(code="MCP_SERVER_UNAVAILABLE", detail=str(exc)) from exc

        client = self._make_client(config)
        try:
            await client.connect()
            tools = await client.list_tools()
        except McpError:
            await client.close()
            raise
        except Exception as exc:
            await client.close()
            raise McpError(code="MCP_SERVER_UNAVAILABLE", detail=str(exc)) from exc

        if tool_name not in tools:
            await client.close()
            raise McpError(
                code="MCP_TOOL_NOT_FOUND",
                detail=f"Tool {tool_name!r} not found",
                tool=tool_name,
                available=tools,
            )

        handle = AgentHandle(
            run_id=task.run_id,
            node_id=task.node_id,
            agent_id=task.agent_id,
            status="running",
            metadata={
                "tool_name": tool_name,
                "args": task.payload,
                "provider": self.provider,
            },
        )
        self._sessions[handle.handle_id] = client
        return handle

    async def stream_events(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        from xueban.runtime.errors import McpError

        client = self._sessions.get(handle.handle_id)
        tool_name = str(handle.metadata.get("tool_name", ""))
        args: Dict[str, Any] = handle.metadata.get("args") or {}

        if client is None:
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.failed",
                payload={"error": f"no MCP session in registry for handle {handle.handle_id}"},
            )
            return

        try:
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.tool_called", payload={"tool": tool_name, "args": args},
            )
            result = await client.call_tool(tool_name, args)
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.tool_result", payload={"result": result},
            )
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.completed", payload={"result": result},
            )
        except McpError as exc:
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.failed",
                payload={"code": exc.code, "detail": exc.detail},
            )
        except Exception as exc:
            yield AgentEvent(
                run_id=handle.run_id, node_id=handle.node_id, agent_id=handle.agent_id,
                type="agent.failed",
                payload={"code": "MCP_TOOL_ERROR", "detail": str(exc)},
            )
        finally:
            self._sessions.pop(handle.handle_id, None)
            await client.close()

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            streaming=False,
            approval_required=False,
            session_resume=False,
            tool_calls=True,
        )


def _build_acp_executors() -> List[AcpAgentExecutor]:
    """Build default ACP executors for hermes and shadowsoul."""
    return [
        AcpAgentExecutor(provider="hermes", command=["hermes", "acp"]),
        AcpAgentExecutor(provider="shadowsoul", command=["shadow", "acp", "serve"]),
    ]


def _build_mcp_executors() -> List[McpAgentExecutor]:
    """Build default MCP executors for generic and hermes providers."""
    return [
        McpAgentExecutor(provider="generic"),
        McpAgentExecutor(
            provider="hermes",
            default_server="stdio://hermes mcp serve",
            default_tool="run_agent",
        ),
    ]


def _build_preset_cli_executors() -> List[CliAgentExecutor]:
    """Load all presets and instantiate CliAgentExecutor for each.

    Failures are logged rather than raised — a broken provider_presets.yaml
    must not crash the runtime — but they MUST be visible to operators.
    """
    try:
        from xueban.runtime.preset_loader import load_presets
        presets = load_presets()
        return [CliAgentExecutor(provider=name, preset=preset) for name, preset in presets.items()]
    except Exception:
        logger.warning(
            "Failed to load CLI provider presets; no preset-driven CliAgentExecutors "
            "will be registered. Check xueban/runtime/provider_presets.yaml.",
            exc_info=True,
        )
        return []


class ExecutorRegistry:
    def __init__(self, executors: Optional[List[BaseExecutor]] = None) -> None:
        self._executors: Dict[str, BaseExecutor] = {}
        # (kind, provider) → AgentExecutor for the new plugin contract
        self._agent_executors: Dict[Tuple[str, str], AgentExecutor] = {}
        for executor in executors or [CliExecutor(), ApiExecutor(), AgentTeamsExecutor(), RagExecutor()]:
            self.register(executor)
        # Auto-register preset-driven CliAgentExecutors
        for agent_executor in _build_preset_cli_executors():
            self.register_agent(agent_executor)
        # Auto-register ACP executors (hermes + shadowsoul)
        for agent_executor in _build_acp_executors():
            self.register_agent(agent_executor)
        # Auto-register MCP executors (generic + hermes)
        for agent_executor in _build_mcp_executors():
            self.register_agent(agent_executor)

    def register(self, executor: BaseExecutor) -> None:
        # P2 seam: registering an executor also opens its kind at the workflow
        # schema level, so custom executors need no contracts.py edit.
        from xueban.runtime.contracts import register_executor_kind

        register_executor_kind(executor.kind)
        self._executors[executor.kind] = executor

    def register_agent(self, executor: AgentExecutor) -> None:
        """Register an AgentExecutor under (kind, provider) composite key.

        Emits a warning on override so silent replacement of default ACP/MCP/CLI
        executors by user presets is visible to operators.
        """
        key = (executor.kind, executor.provider)
        if key in self._agent_executors:
            logger.warning(
                "AgentExecutor overriding existing registration for kind=%r provider=%r "
                "(old=%s, new=%s)",
                key[0], key[1],
                type(self._agent_executors[key]).__name__,
                type(executor).__name__,
            )
        self._agent_executors[key] = executor

    def resolve(self, kind: str, provider: str) -> AgentExecutor:
        """Find a registered AgentExecutor or raise UnknownExecutorError."""
        agent_exec = self._agent_executors.get((kind, provider))
        if agent_exec is None:
            raise UnknownExecutorError(kind, provider, list(self._agent_executors.keys()))
        return agent_exec

    def list_agent_executors(self) -> List[Tuple[str, str]]:
        """Return all registered (kind, provider) pairs."""
        return list(self._agent_executors.keys())

    async def execute(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        kind = config.get("kind")
        if not isinstance(kind, str):
            raise ValueError("executor.kind must be a string")
        executor = self._executors.get(kind)
        if executor is None:
            raise ValueError(f"unsupported executor kind: {kind}")
        return await executor.execute(config=config, payload=payload)


class CliExecutor(BaseExecutor):
    kind = "cli"

    def __init__(self) -> None:
        self._provider_defaults = {
            "codex": self._resolve_codex_invocation,
            "claude": self._resolve_claude_invocation,
        }

    async def execute(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        provider = str(config.get("provider", "generic"))
        invocation = self._resolve_invocation(provider=provider, config=config, payload=payload)
        timeout_seconds = self._resolve_timeout_seconds(config)
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                invocation["command"],
                input=invocation.get("stdin_payload"),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                cwd=invocation.get("cwd"),
                env=invocation.get("env"),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"cli executor timed out for provider={provider} after {timeout_seconds} seconds"
            ) from exc
        if completed.returncode != 0:
            raise ValueError(
                f"cli executor failed for provider={provider} with exit code {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )

        return self._parse_cli_output(
            provider=provider,
            parse_mode=invocation["parse_mode"],
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=invocation["command"],
        )

    def _resolve_invocation(
        self,
        *,
        provider: str,
        config: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        provider_builder = self._provider_defaults.get(provider)
        if provider_builder is not None and "command" not in config:
            invocation = provider_builder(config=config, payload=payload)
        else:
            invocation = self._resolve_generic_invocation(config=config, payload=payload)

        raw_env = config.get("env")
        env = None
        if raw_env is not None:
            if not isinstance(raw_env, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
            ):
                raise ValueError("executor.env must be a string map")
            env = {**os.environ, **raw_env}

        # claude CLI refuses to run inside a Claude Code session unless CLAUDECODE is unset
        if provider == "claude" and "CLAUDECODE" in os.environ:
            env = {k: v for k, v in (env or os.environ).items() if k != "CLAUDECODE"}

        invocation["env"] = env
        invocation["cwd"] = config.get("cwd")
        return invocation

    def _resolve_generic_invocation(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        command = config.get("command")
        if isinstance(command, str):
            command_parts = [command]
        elif isinstance(command, list) and all(isinstance(item, str) for item in command):
            command_parts = list(command)
        else:
            raise ValueError("executor.command must be a string or string list for generic cli executor")

        args = config.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("executor.args must be a string list")

        stdin_mode = config.get("stdin", "json")
        if stdin_mode == "json":
            stdin_payload = json.dumps(payload, ensure_ascii=False)
        elif stdin_mode == "text":
            stdin_payload = compile_execution_prompt(payload)
        elif stdin_mode == "none":
            stdin_payload = None
        else:
            raise ValueError("executor.stdin must be json, text, or none")

        parse_mode = str(config.get("parse", "json"))
        return {
            "command": [*command_parts, *args],
            "stdin_payload": stdin_payload,
            "parse_mode": parse_mode,
        }

    def _resolve_codex_invocation(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = compile_execution_prompt(payload)
        command = [
            self._resolve_command_binary(["codex", "codex.cmd", "codex.ps1"]),
            "exec",
            "--skip-git-repo-check",
            "--json",
            "-",
        ]
        approval_mode = config.get("approval_mode", "full-auto")
        if approval_mode == "full-auto":
            command.append("--full-auto")
        model = config.get("model")
        if isinstance(model, str) and model:
            command.extend(["--model", model])
        working_directory = config.get("cwd")
        if isinstance(working_directory, str) and working_directory:
            command.extend(["--cd", working_directory])
        command.extend(self._resolve_extra_args(config))
        return {
            "command": command,
            "stdin_payload": prompt,
            "parse_mode": str(config.get("parse", "codex-jsonl")),
        }

    def _resolve_claude_invocation(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        prompt = compile_execution_prompt(payload)
        command = [self._resolve_command_binary(["claude", "claude.exe"]), "-p", prompt, "--output-format", "json"]
        model = config.get("model")
        if isinstance(model, str) and model:
            command.extend(["--model", model])
        system_prompt = config.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            command.extend(["--system-prompt", system_prompt])
        permission_mode = config.get("permission_mode")
        if isinstance(permission_mode, str) and permission_mode:
            command.extend(["--permission-mode", permission_mode])
        command.extend(self._resolve_extra_args(config))
        return {
            "command": command,
            "stdin_payload": None,
            "parse_mode": str(config.get("parse", "claude-json")),
        }

    def _parse_cli_output(
        self,
        *,
        provider: str,
        parse_mode: str,
        stdout: str,
        stderr: str,
        command: List[str],
    ) -> Dict[str, Any]:
        clean_stdout = stdout.strip()
        output: Dict[str, Any] = {
            "executor": {
                "kind": "cli",
                "provider": provider,
                "command": command,
                "parse_mode": parse_mode,
            }
        }
        if stderr.strip():
            output["stderr"] = stderr.strip()

        if parse_mode == "text":
            output["response_text"] = clean_stdout
            output["message"] = clean_stdout
            return output

        if parse_mode == "json":
            parsed = json.loads(clean_stdout)
            return self._merge_parsed_output(output, parsed)

        if parse_mode == "claude-json":
            parsed = json.loads(clean_stdout)
            output["raw_output"] = parsed
            text = extract_text_content(parsed)
            if isinstance(parsed, dict):
                output.update({key: value for key, value in parsed.items() if key not in {"message"}})
            output["response_text"] = text
            output["message"] = text or output.get("message", "")
            return output

        if parse_mode == "codex-jsonl":
            events: List[Dict[str, Any]] = []
            for line in clean_stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"type": "text", "text": line})
            output["raw_output"] = {"events": events}
            text = ""
            for event in reversed(events):
                text = extract_text_content(event)
                if text:
                    break
            output["response_text"] = text
            output["message"] = text or output.get("message", "")
            output["events"] = events
            return output

        raise ValueError(f"unsupported cli parse mode: {parse_mode}")

    def _merge_parsed_output(self, base: Dict[str, Any], parsed: Any) -> Dict[str, Any]:
        output = dict(base)
        output["raw_output"] = parsed
        if isinstance(parsed, dict):
            output.update(parsed)
            output.setdefault("response_text", extract_text_content(parsed))
            output.setdefault("message", output.get("response_text", ""))
        else:
            output["result"] = parsed
            output["response_text"] = extract_text_content(parsed)
            output["message"] = output["response_text"]
        return output

    def _resolve_command_binary(self, candidates: List[str]) -> str:
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return candidates[0]

    def _resolve_extra_args(self, config: Dict[str, Any]) -> List[str]:
        extra_args = config.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(isinstance(item, str) for item in extra_args):
            raise ValueError("executor.extra_args must be a string list")
        return list(extra_args)

    def _resolve_timeout_seconds(self, config: Dict[str, Any]) -> Optional[float]:
        timeout = config.get("timeout_seconds")
        if timeout is None:
            return None
        try:
            resolved = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("executor.timeout_seconds must be a positive number") from exc
        if resolved <= 0:
            raise ValueError("executor.timeout_seconds must be a positive number")
        return resolved


class ApiExecutor(BaseExecutor):
    kind = "api"

    async def execute(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        provider = str(config.get("provider", "")).strip().lower()
        if provider == "openai":
            return await self._execute_openai(config=config, payload=payload)
        if provider == "anthropic":
            return await self._execute_anthropic(config=config, payload=payload)
        if provider == "zhipu":
            return await self._execute_zhipu(config=config, payload=payload)
        raise ValueError(f"unsupported api provider: {provider}")

    async def _execute_zhipu(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        """Zhipu GLM (open.bigmodel.cn) — OpenAI-compatible chat-completions style.

        Any other OpenAI-compatible gateway works too: override base_url/api_key_env/model.
        Key resolution: config.api_key_env first (BYOK), default env ZHIPU_API_KEY.
        """
        api_key = self._get_api_key(config=config, default_env="ZHIPU_API_KEY")
        model = str(config.get("model", os.environ.get("ZHIPU_MODEL", "glm-4.5")))
        compiled_prompt = self._build_api_prompt(config=config, payload=payload, enforce_json=config.get("parse") == "json")
        body: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": compiled_prompt}],
        }
        system_prompt = config.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            body["messages"].insert(0, {"role": "system", "content": system_prompt})
        if config.get("temperature") is not None:
            body["temperature"] = config["temperature"]
        if config.get("max_tokens") is not None:
            body["max_tokens"] = config["max_tokens"]
        if config.get("parse") == "json":
            # 智谱兼容 response_format json_object（部分模型支持；不支持时靠 prompt 约束）
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = float(config.get("timeout_seconds", 120))
        url = str(config.get(
            "base_url",
            os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions"),
        ))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        text = ""
        choices = data.get("choices") or []
        if choices:
            text = str(choices[0].get("message", {}).get("content", "") or "")
        return self._normalize_api_output(
            provider="zhipu",
            config=config,
            data=data,
            text=text,
            model=model,
        )

    async def _execute_openai(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        api_key = self._get_api_key(config=config, default_env="OPENAI_API_KEY")
        model = str(config.get("model", DEFAULT_OPENAI_MODEL))
        compiled_prompt = self._build_api_prompt(config=config, payload=payload, enforce_json=config.get("parse") == "json")
        body: Dict[str, Any] = {
            "model": model,
            "input": compiled_prompt,
        }
        system_prompt = config.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            body["instructions"] = system_prompt
        if config.get("temperature") is not None:
            body["temperature"] = config["temperature"]
        if config.get("max_output_tokens") is not None:
            body["max_output_tokens"] = config["max_output_tokens"]
        if config.get("parse") == "json":
            body["text"] = {"format": {"type": "json_object"}}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = float(config.get("timeout_seconds", 120))
        url = str(config.get("base_url", "https://api.openai.com/v1/responses"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        return self._normalize_api_output(
            provider="openai",
            config=config,
            data=data,
            text=data.get("output_text") or extract_text_content(data),
            model=model,
        )

    async def _execute_anthropic(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        api_key = self._get_api_key(config=config, default_env="ANTHROPIC_API_KEY")
        model = str(config.get("model", DEFAULT_ANTHROPIC_MODEL))
        compiled_prompt = self._build_api_prompt(config=config, payload=payload, enforce_json=config.get("parse") == "json")
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": int(config.get("max_tokens", 1024)),
            "messages": [{"role": "user", "content": compiled_prompt}],
        }
        system_prompt = config.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt:
            body["system"] = system_prompt
        if config.get("temperature") is not None:
            body["temperature"] = config["temperature"]
        # LLM-as-Verifier: 原生 logprob 支持（Anthropic Claude）
        if config.get("logprobs", False):
            body["logprobs"] = True
            # 可选：指定 top_logprobs 数量（默认 0）
            top_k = config.get("top_logprobs", 5)
            if top_k > 0:
                body["top_logprobs"] = top_k

        headers = {
            "x-api-key": api_key,
            "anthropic-version": str(config.get("anthropic_version", "2023-06-01")),
            "content-type": "application/json",
        }
        timeout = float(config.get("timeout_seconds", 120))
        url = str(config.get("base_url", "https://api.anthropic.com/v1/messages"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        text = extract_text_content(data.get("content", []))
        return self._normalize_api_output(
            provider="anthropic",
            config=config,
            data=data,
            text=text,
            model=model,
        )

    def _get_api_key(self, *, config: Dict[str, Any], default_env: str) -> str:
        env_name = str(config.get("api_key_env", default_env))
        api_key = os.getenv(env_name)
        if not api_key:
            raise ValueError(f"missing API key environment variable: {env_name}")
        return api_key

    def _build_api_prompt(self, *, config: Dict[str, Any], payload: Dict[str, Any], enforce_json: bool) -> str:
        compiled = compile_execution_prompt(payload)
        extra_instruction = config.get("response_instruction")
        sections = [compiled] if compiled else []
        if isinstance(extra_instruction, str) and extra_instruction:
            sections.append(extra_instruction)
        if enforce_json:
            sections.append("Return valid JSON only.")
        return "\n\n".join(section for section in sections if section).strip()

    def _normalize_api_output(
        self,
        *,
        provider: str,
        config: Dict[str, Any],
        data: Dict[str, Any],
        text: str,
        model: str,
    ) -> Dict[str, Any]:
        output: Dict[str, Any] = {
            "executor": {
                "kind": "api",
                "provider": provider,
                "model": model,
            },
            "raw_output": data,
            "response_text": text,
        }

        # LLM-as-Verifier: 提取 logprob 数据
        if config.get("logprobs", False) and provider == "anthropic":
            logprobs_data = self._extract_anthropic_logprobs(data.get("content", []))
            if logprobs_data:
                output["logprobs"] = logprobs_data

        parse_mode = str(config.get("parse", "text"))
        if parse_mode == "json":
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                output.update(parsed)
                output.setdefault("message", extract_text_content(parsed))
            else:
                output["result"] = parsed
                output["message"] = extract_text_content(parsed)
            return output
        if parse_mode != "text":
            raise ValueError(f"unsupported api parse mode: {parse_mode}")
        output["message"] = text
        return output

    def _extract_anthropic_logprobs(self, content: list) -> Dict[str, Any]:
        """从 Anthropic API 响应中提取 logprob 数据。

        Returns:
            dict with:
            - tokens: List[str] — token 序列
            - logprobs: List[float] — 对应的对数概率
            - top_logprobs: List[Dict[str, float]] — 每个位置的 top-K token 概率
        """
        tokens: List[str] = []
        logprobs: List[float] = []
        top_logprobs: List[Dict[str, float]] = []

        for block in content:
            if block.get("type") == "text":
                # 提取 token 级 logprob（如果存在）
                for token_data in block.get("logprobs", []):
                    token = token_data.get("token", "")
                    if token:
                        tokens.append(token)
                        logprobs.append(token_data.get("logprob", 0.0))
                        # 提取 top_logprobs
                        top_logprobs.append({
                            t.get("token", ""): t.get("logprob", 0.0)
                            for t in token_data.get("top_logprobs", [])
                        })

        return {
            "tokens": tokens,
            "logprobs": logprobs,
            "top_logprobs": top_logprobs,
        } if tokens else {}


class RagExecutor(BaseExecutor):
    """Knowledge-grounded retrieval executor (kind="rag").

    Bridges the LightRAG KnowledgePack index into the workflow/loop execution
    flow: place a ``rag`` node before an LLM node and wire its
    ``raw_output.answer`` into the next node's prompt (state writeback works
    like any other node).

    Config:
        pack_id:   KnowledgePack id to query (or taken from payload.pack_id)
        query:     retrieval question (or payload.prompt / payload.query)
        mode:      naive | local | global | hybrid (default LIGHTRAG_QUERY_MODE)
        required:  when true, a disabled/unavailable RAG backend fails the node;
                   default false → degrade to empty context with a note.

    Degradation contract: LIGHTRAG_ENABLED≠true or lightrag-hku missing →
    returns ``{"rag_backend": "stub", "answer": "", "note": ...}`` so graphs
    stay runnable in dev/test environments.
    """

    kind = "rag"

    async def execute(self, config: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        required = bool(config.get("required", False))
        pack_id = str(config.get("pack_id") or (payload or {}).get("pack_id") or "").strip()
        query = (
            config.get("query")
            or (payload or {}).get("query")
            or (payload or {}).get("prompt")
            or ""
        )
        if not pack_id or not str(query).strip():
            raise ValueError("rag node requires pack_id and a non-empty query/prompt")

        from xueban.runtime import lightrag_adapter

        output: Dict[str, Any] = {
            "executor": {"kind": "rag", "pack_id": pack_id, "backend": lightrag_adapter.backend_label()},
        }

        if not lightrag_adapter.is_enabled():
            note = "LightRAG disabled (LIGHTRAG_ENABLED != true); returning empty context"
            if required:
                raise RuntimeError(note)
            logger.warning("RagExecutor: %s", note)
            output.update({"answer": "", "contexts": [], "note": note})
            return output

        try:
            answer = lightrag_adapter.query(pack_id, str(query), mode=config.get("mode"))
            output.update({"answer": answer, "contexts": [answer]})
        except (ImportError, lightrag_adapter.LightRagConfigError, lightrag_adapter.LightRagQueryError) as exc:
            if required:
                raise
            logger.warning("RagExecutor query failed for pack %s (%s); degrading", pack_id, exc)
            output.update({"answer": "", "contexts": [], "note": f"rag query failed: {exc}"})
        return output

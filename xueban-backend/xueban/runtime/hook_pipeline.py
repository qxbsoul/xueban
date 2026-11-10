"""Waterfall validation-hook pipeline (P3 / N1 Python-side runner).

Semantics follow the three-domain event model the dsh research identified:
validation hooks are *waterfall* listeners on the executor pipeline — each
one may pass (call next), rewrite the payload, or reject. Spec shape reuses
``xueban.runtime.validation_hooks.schema.ValidationHookSpec`` (the
canonical schema already shared with the Node-side runner):

- kind: shell | webhook | builtin
- on_fail: blocker (raise, node fails) | warn (log + mark) | retry (hook-only budget)
- max_retries: idempotent wait-and-check retry budget (default 0)
- expose_error_details: False → error.message/stack redacted to "[redacted]"

Wire-in point: RuntimeService._execute_executor_node runs node.config
.validation_hooks around executor.execute() (pre/post phases).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from xueban.runtime.validation_hooks import ValidationHookSpec

logger = logging.getLogger(__name__)


class HookRejected(Exception):
    """A blocker hook rejected the step — the node must fail."""

    def __init__(self, hook_id: str, reason: str) -> None:
        self.hook_id = hook_id
        self.reason = reason
        super().__init__(f"validation hook {hook_id!r} rejected: {reason}")


WaterfallFn = Callable[[Dict[str, Any], Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]], Awaitable[Dict[str, Any]]]


class WaterfallPipeline:
    """Ordered async waterfall: every listener must call ``next(payload)``.

    Mirrors dsh's waterfall events (tools/pre-execute → execute →
    post-execute): listeners may transform the payload or reject by raising;
    not calling ``next`` short-circuits the chain (treated as a reject of the
    remainder for hooks, which is the intended semantic).
    """

    def __init__(self) -> None:
        self._listeners: List[Tuple[int, str, WaterfallFn]] = []

    def add(self, name: str, fn: WaterfallFn, *, priority: int = 100) -> None:
        self._listeners.append((priority, name, fn))
        self._listeners.sort(key=lambda item: (item[0], item[1]))

    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        listeners = list(self._listeners)

        async def chain(index: int, current: Dict[str, Any]) -> Dict[str, Any]:
            if index >= len(listeners):
                return current
            _priority, name, fn = listeners[index]

            called_next = False

            async def next_payload(next_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
                nonlocal called_next
                called_next = True
                return await chain(index + 1, next_input if next_input is not None else current)

            try:
                return await fn(current, next_payload)
            finally:
                if not called_next:
                    logger.warning(
                        "waterfall listener %r did not call next(); chain short-circuited", name
                    )

        return await chain(0, payload)


# ---------------------------------------------------------------------------
# Hook execution (ValidationHookSpec) — shell / webhook / builtin
# ---------------------------------------------------------------------------

BUILTIN_VALIDATORS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Tuple[bool, str]]] = {}


def register_builtin_validator(
    name: str, fn: Callable[[Dict[str, Any], Dict[str, Any]], Tuple[bool, str]]
) -> None:
    """Register an in-process validator: fn(args, payload) → (passed, detail)."""
    BUILTIN_VALIDATORS[name] = fn


def _builtin_file_exists(args: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, str]:
    import os

    path = str(args.get("path", ""))
    return (os.path.exists(path), f"file-exists({path})")


def _builtin_output_contains(args: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, str]:
    key = str(args.get("key", ""))
    results = payload.get("results") if isinstance(payload.get("results"), dict) else {}
    return (key in results, f"output-contains({key})")


register_builtin_validator("file-exists", _builtin_file_exists)
register_builtin_validator("output-contains", _builtin_output_contains)


def _redact(payload: Dict[str, Any], spec: ValidationHookSpec) -> Dict[str, Any]:
    """PM Q12.3: default-redact upstream error details before templating."""
    if spec.expose_error_details:
        return payload
    cleaned = json.loads(json.dumps(payload, default=str))
    for container in (cleaned.get("results") or {},):
        if isinstance(container, dict) and isinstance(container.get("error"), dict):
            for field in ("message", "stack", "traceback"):
                if field in container["error"]:
                    container["error"][field] = "[redacted]"
    return cleaned


class HookOutcome:
    def __init__(self, passed: bool, detail: str, attempts: int) -> None:
        self.passed = passed
        self.detail = detail
        self.attempts = attempts


class ValidationHookRunner:
    """Executes ValidationHookSpec entries with on_fail/retry semantics."""

    async def run_hook(self, spec: ValidationHookSpec, payload: Dict[str, Any]) -> HookOutcome:
        safe_payload = _redact(payload, spec)
        attempts = 0
        last_detail = ""
        budget = spec.max_retries + 1
        while attempts < budget:
            attempts += 1
            try:
                if spec.kind == "shell" and spec.shell is not None:
                    passed, last_detail = await self._run_shell(spec, safe_payload)
                elif spec.kind == "webhook" and spec.webhook is not None:
                    passed, last_detail = await self._run_webhook(spec, safe_payload)
                elif spec.kind == "builtin" and spec.builtin is not None:
                    validator = BUILTIN_VALIDATORS.get(spec.builtin.name)
                    if validator is None:
                        passed, last_detail = False, f"unknown builtin validator {spec.builtin.name!r}"
                    else:
                        passed, last_detail = validator(spec.builtin.args, safe_payload)
                else:
                    passed, last_detail = False, f"hook {spec.id} has no usable config"
            except Exception as exc:  # noqa: BLE001 — hook failure is data, not a crash
                passed, last_detail = False, f"{type(exc).__name__}: {exc}"
            if passed:
                return HookOutcome(True, last_detail, attempts)
            if attempts < budget and spec.on_fail == "retry":
                # small backoff so idempotent wait-and-check hooks observe
                # side effects that land shortly after the node ran
                await asyncio.sleep(min(0.1, spec.timeout_ms / 1000 / 10))
                continue
            break

        if spec.on_fail == "blocker":
            raise HookRejected(spec.id, last_detail)
        # warn / exhausted retry-as-warn: log and continue the run
        logger.warning("validation hook %s failed (warn): %s", spec.id, last_detail)
        return HookOutcome(False, last_detail, attempts)

    async def run_all(
        self, specs: List[ValidationHookSpec], payload: Dict[str, Any]
    ) -> List[HookOutcome]:
        outcomes: List[HookOutcome] = []
        for spec in specs:
            if not spec.enabled:
                continue
            outcomes.append(await self.run_hook(spec, payload))
        return outcomes

    # -- transports --------------------------------------------------------

    async def _run_shell(self, spec: ValidationHookSpec, payload: Dict[str, Any]) -> Tuple[bool, str]:
        assert spec.shell is not None
        env = dict(spec.shell.env)
        env.setdefault("SF_HOOK_RESULTS_JSON", json.dumps(payload.get("results", {}), default=str))
        cwd = spec.shell.cwd.replace("${workspace}", ".")
        proc = await asyncio.create_subprocess_exec(
            *spec.shell.cmd,
            cwd=cwd or None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_ms / 1000)
        except asyncio.TimeoutError:
            proc.kill()
            return False, f"shell hook {spec.id} timed out after {spec.timeout_ms}ms"
        passed = proc.returncode == spec.shell.success_when.exit_code
        detail = (stdout or stderr or b"").decode("utf-8", "replace").strip()[:500]
        return passed, detail or f"exit_code={proc.returncode}"

    async def _run_webhook(self, spec: ValidationHookSpec, payload: Dict[str, Any]) -> Tuple[bool, str]:
        assert spec.webhook is not None
        hook = spec.webhook
        body = None
        headers = dict(hook.headers)
        if hook.body_template is not None:
            rendered = hook.body_template.replace(
                "${results_json}", json.dumps(payload.get("results", {}), default=str)
            )
            body = rendered.encode("utf-8")
            headers.setdefault("Content-Type", "application/json")

        def _call() -> Tuple[int, str]:
            req = urllib.request.Request(hook.url, data=body, method=hook.method, headers=headers)
            with urllib.request.urlopen(req, timeout=spec.timeout_ms / 1000) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")[:1000]

        status, text = await asyncio.to_thread(_call)
        passed = status == hook.success_when.status_code
        if passed and hook.success_when.json_path is not None:
            try:
                node: Any = json.loads(text)
                for part in hook.success_when.json_path.lstrip("$.").split("."):
                    node = node[part]
                passed = node == hook.success_when.equals
            except Exception:  # noqa: BLE001 — path miss means not passed
                passed = False
        return passed, f"status={status} body={text[:200]}"


def parse_inline_hooks(raw: Any) -> List[ValidationHookSpec]:
    """Parse node.config.validation_hooks (list of hook dicts) into specs."""
    if not isinstance(raw, list):
        return []
    specs = []
    for entry in raw:
        if isinstance(entry, dict):
            specs.append(ValidationHookSpec.model_validate(entry))
    return specs


__all__ = [
    "HookRejected",
    "HookOutcome",
    "ValidationHookRunner",
    "WaterfallPipeline",
    "parse_inline_hooks",
    "register_builtin_validator",
]

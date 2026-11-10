"""Validation-hook waterfall (N1 Python-side) + .sfagent bundle (M9) tests.

Deterministic: shell hooks use the repo's own python binary; webhook hooks
hit a local asyncio socket server; bundles build from fixture specs.
"""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from io import BytesIO

import pytest

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.agent_bundle import build_sfagent_files, export_sfagent_zip
from xueban.runtime.executors import BaseExecutor, ExecutorRegistry
from xueban.runtime.hook_pipeline import (
    HookRejected,
    ValidationHookRunner,
    WaterfallPipeline,
    parse_inline_hooks,
)
from xueban.runtime.validation_hooks import ValidationHookSpec


# ---------------------------------------------------------------------------
# Waterfall pipeline semantics
# ---------------------------------------------------------------------------


def test_waterfall_transforms_flow_through_next():
    pipe = WaterfallPipeline()

    async def first(payload, next_):
        payload["trace"] = ["first"]
        return await next_({**payload, "trace": payload["trace"] + ["first-next"]})

    async def second(payload, next_):
        return await next_({**payload, "trace": payload["trace"] + ["second"]})

    pipe.add("first", first, priority=10)
    pipe.add("second", second, priority=20)

    result = asyncio.run(pipe.run({"x": 1}))
    assert result["trace"] == ["first", "first-next", "second"]


def test_waterfall_rejection_short_circuits():
    pipe = WaterfallPipeline()
    reached = {"second": False}

    async def rejector(payload, next_):
        raise HookRejected("rejector", "not allowed")

    async def second(payload, next_):
        reached["second"] = True
        return await next_(payload)

    pipe.add("rejector", rejector, priority=10)
    pipe.add("second", second, priority=20)

    with pytest.raises(HookRejected):
        asyncio.run(pipe.run({}))
    assert reached["second"] is False


# ---------------------------------------------------------------------------
# Hook runner: builtin / shell / on_fail semantics
# ---------------------------------------------------------------------------


def test_builtin_file_exists_hook(tmp_path):
    runner = ValidationHookRunner()
    target = tmp_path / "out.txt"
    target.write_text("hi", encoding="utf-8")

    ok_spec = ValidationHookSpec.model_validate(
        {"id": "h1", "kind": "builtin", "builtin": {"name": "file-exists", "args": {"path": str(target)}}}
    )
    outcome = asyncio.run(runner.run_hook(ok_spec, {}))
    assert outcome.passed is True

    bad_spec = ValidationHookSpec.model_validate(
        {"id": "h2", "kind": "builtin", "builtin": {"name": "file-exists", "args": {"path": str(tmp_path / "nope")}},
         "on_fail": "blocker"}
    )
    with pytest.raises(HookRejected, match="h2"):
        asyncio.run(runner.run_hook(bad_spec, {}))


def test_shell_hook_success_and_blocker():
    runner = ValidationHookRunner()
    ok = ValidationHookSpec.model_validate(
        {"id": "sh-ok", "kind": "shell",
         "shell": {"cmd": [sys.executable, "-c", "print('hook ran')"]}}
    )
    outcome = asyncio.run(runner.run_hook(ok, {}))
    assert outcome.passed is True and outcome.attempts == 1

    blocker = ValidationHookSpec.model_validate(
        {"id": "sh-bad", "kind": "shell", "on_fail": "blocker",
         "shell": {"cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}}
    )
    with pytest.raises(HookRejected, match="sh-bad"):
        asyncio.run(runner.run_hook(blocker, {}))


def test_warn_hook_fails_soft():
    runner = ValidationHookRunner()
    spec = ValidationHookSpec.model_validate(
        {"id": "w", "kind": "shell", "on_fail": "warn",
         "shell": {"cmd": [sys.executable, "-c", "import sys; sys.exit(1)"]}}
    )
    outcome = asyncio.run(runner.run_hook(spec, {}))
    assert outcome.passed is False  # but no exception


def test_retry_budget_reruns_hook(tmp_path):
    # fail the first time (marker missing), pass the second (marker created by a warmup)
    runner = ValidationHookRunner()
    marker = tmp_path / "ready.flag"

    async def scenario():
        async def warmup():
            await asyncio.sleep(0.05)
            marker.write_text("1", encoding="utf-8")

        asyncio.create_task(warmup())
        spec = ValidationHookSpec.model_validate(
            {"id": "r", "kind": "builtin", "on_fail": "retry", "max_retries": 5,
             "builtin": {"name": "file-exists", "args": {"path": str(marker)}}}
        )
        return await runner.run_hook(spec, {})

    outcome = asyncio.run(scenario())
    assert outcome.passed is True
    assert outcome.attempts >= 2


# ---------------------------------------------------------------------------
# Inline node hooks wired into the executor pipeline
# ---------------------------------------------------------------------------


class EchoExecutor(BaseExecutor):
    kind = "echo_demo"

    async def execute(self, config, payload):
        return {"message": "echo", "branch_result": "ok", "emit": config.get("emit")}


def _hooked_workflow(hooks: list) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "hooked",
            "version": "0.1",
            "name": "Hooked",
            "entrypoint": "n",
            "nodes": [
                {"id": "n", "kind": "agent", "type": "agent.execute",
                 "config": {"executor": {"kind": "echo_demo"}, "validation_hooks": hooks}},
            ],
            "edges": [{"from": "n", "to": "END", "type": "final"}],
        }
    )


def test_inline_builtin_hook_pass_marks_output(tmp_path):
    marker = tmp_path / "m.txt"
    marker.write_text("x", encoding="utf-8")
    service = RuntimeService(executor_registry=ExecutorRegistry(executors=[EchoExecutor()]))
    result = asyncio.run(service.run(RuntimeRequest(workflow=_hooked_workflow([
        {"id": "check", "kind": "builtin",
         "builtin": {"name": "file-exists", "args": {"path": str(marker)}}},
    ]), input={})))
    assert result.run.status == "succeeded"
    hooks = result.steps[0].output["validation_hooks"]
    assert hooks[0]["passed"] is True


def test_inline_blocker_hook_fails_the_run():
    service = RuntimeService(executor_registry=ExecutorRegistry(executors=[EchoExecutor()]))
    with pytest.raises(HookRejected, match="stop"):
        asyncio.run(service.run(RuntimeRequest(workflow=_hooked_workflow([
            {"id": "stop", "kind": "shell", "on_fail": "blocker",
             "shell": {"cmd": [sys.executable, "-c", "import sys; sys.exit(9)"]}},
        ]), input={})))


def test_parse_inline_hooks_ignores_garbage():
    assert parse_inline_hooks(None) == []
    assert parse_inline_hooks("nope") == []
    assert parse_inline_hooks([{"id": "x", "kind": "builtin", "builtin": {"name": "output-contains", "args": {}}}]) != []


# ---------------------------------------------------------------------------
# .sfagent bundle (M9)
# ---------------------------------------------------------------------------

_AGENT = {
    "id": "planner-alpha",
    "name": "Planner Alpha",
    "role": "Planner",
    "kind": "agent",
    "capabilities": ["task-decomposition", "goal-refinement"],
    "inputs": ["goal"],
    "outputs": ["plan"],
    "decision_boundary": "may only propose; cannot execute tools",
    "skills": [
        {
            "name": "decompose",
            "purpose": "split goal into steps",
            "input": "goal text",
            "output": "step list",
            "invoke_when": "planning phase",
            "safety_boundary": "read-only",
        }
    ],
}


def test_bundle_files_and_manifest():
    files = build_sfagent_files(_AGENT)
    assert set(files) == {"sfagent.json", "identity.yaml", "skills.yaml", "trajectory.json", "README.md"}

    manifest = json.loads(files["sfagent.json"])
    assert manifest["format"] == "sfagent"
    assert manifest["agent_id"] == "planner-alpha"
    assert manifest["content_id"].startswith("sha256:")
    assert manifest["evidence"]["run_count"] == 0

    import yaml

    identity = yaml.safe_load(files["identity.yaml"])
    assert identity["Name"] == "Planner Alpha"
    assert "task-decomposition" in identity["Capabilities"]
    skills = yaml.safe_load(files["skills.yaml"])["skills"]
    assert skills[0]["name"] == "decompose"


def test_bundle_zip_is_deterministic_and_opens():
    z1 = export_sfagent_zip(_AGENT)
    z2 = export_sfagent_zip(_AGENT)
    assert z1 == z2  # fixed generated_at → byte-identical

    with zipfile.ZipFile(BytesIO(z1)) as zf:
        assert sorted(zf.namelist()) == [
            "README.md", "identity.yaml", "sfagent.json", "skills.yaml", "trajectory.json"
        ]
        manifest = json.loads(zf.read("sfagent.json"))
    assert manifest["content_id"].startswith("sha256:")


def test_bundle_embeds_run_evidence():
    wf = WorkflowDefinition.model_validate(
        {
            "workflow_id": "ev", "version": "0.1", "name": "Ev", "entrypoint": "loop",
            "nodes": [
                {"id": "loop", "kind": "node", "type": "control.loop",
                 "config": {"kind": "fixed_count", "iteration_budget": 1, "subgraph": ["do"]}},
                {"id": "do", "kind": "agent", "type": "agent.execute", "config": {"role": "Do"}},
            ],
            "edges": [{"from": "loop", "to": "END", "type": "final"}],
        }
    )
    result = asyncio.run(RuntimeService().run(RuntimeRequest(workflow=wf, input={})))
    files = build_sfagent_files(_AGENT, run_results=[result])
    trajectory = json.loads(files["trajectory.json"])
    assert trajectory["evidence"]["run_count"] == 1
    assert trajectory["evidence"]["step_count"] == 1
    assert trajectory["evidence"]["loops"][0]["iterations"] == 1
    manifest = json.loads(files["sfagent.json"])
    assert manifest["evidence"]["loop_count"] == 1

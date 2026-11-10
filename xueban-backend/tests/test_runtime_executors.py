import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from xueban.runtime import RuntimeRequest, RuntimeService, WorkflowDefinition
from xueban.runtime.executors import ApiExecutor, CliExecutor


def test_cli_executor_runs_generic_subprocess_and_maps_result():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "cli-generic-exec",
            "version": "0.1",
            "name": "CLI Generic Exec",
            "entrypoint": "collector",
            "nodes": [
                {
                    "id": "collector",
                    "kind": "agent",
                    "type": "agent.execute",
                    "config": {
                        "role": "collector",
                        "prompt": "Summarize the incoming goal",
                        "executor": {
                            "kind": "cli",
                            "provider": "generic",
                            "command": sys.executable,
                            "args": [
                                "-c",
                                (
                                    "import json,sys;"
                                    "payload=json.load(sys.stdin);"
                                    "print(json.dumps({"
                                    "'message':'[collector] generic cli ok',"
                                    "'summary':'goal=' + payload['step_input'].get('goal',''),"
                                    "'artifact':{'kind':'report','name':'collector.md','content':'# ok'}}))"
                                ),
                            ],
                            "stdin": "json",
                            "parse": "json",
                        },
                    },
                }
            ],
            "edges": [{"from": "collector", "to": "END", "type": "final"}],
        }
    )

    result = asyncio.run(
        RuntimeService().run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "process-cli-input"},
                metadata={"source_system": "pytest"},
            )
        )
    )

    assert result.run.status == "succeeded"
    assert result.final_output["summary"] == "goal=process-cli-input"
    assert result.final_output["executor"]["provider"] == "generic"
    assert result.artifacts[0].name == "collector.md"


def test_executor_node_preserves_static_node_config_outputs():
    workflow = WorkflowDefinition.model_validate(
        {
            "workflow_id": "executor-static-config",
            "version": "0.1",
            "name": "Executor Static Config",
            "entrypoint": "planner",
            "nodes": [
                {
                    "id": "planner",
                    "kind": "agent",
                    "type": "agent.execute",
                    "config": {
                        "role": "planner",
                        "prompt": "Plan the task",
                        "emit": {"channel": "cli"},
                        "set_state": {"phase": "planned"},
                        "copy_input": ["goal"],
                        "artifact": {"kind": "report", "name": "static-executor.md", "content": "# static"},
                        "executor": {
                            "kind": "cli",
                            "provider": "generic",
                            "command": sys.executable,
                            "args": [
                                "-c",
                                (
                                    "import json,sys;"
                                    "payload=json.load(sys.stdin);"
                                    "print(json.dumps({'message':'executor ok','summary':payload['step_input']['goal']}))"
                                ),
                            ],
                            "stdin": "json",
                            "parse": "json",
                        },
                    },
                }
            ],
            "edges": [{"from": "planner", "to": "END", "type": "final"}],
        }
    )

    result = asyncio.run(
        RuntimeService().run(
            RuntimeRequest(
                workflow=workflow,
                input={"goal": "stabilize-runtime"},
                metadata={"source_system": "pytest"},
            )
        )
    )

    assert result.final_output["message"] == "executor ok"
    assert result.final_output["summary"] == "stabilize-runtime"
    assert result.final_output["channel"] == "cli"
    assert result.final_output["state"]["phase"] == "planned"
    assert result.final_output["copied_input"] == {"goal": "stabilize-runtime"}
    assert result.final_output["prompt"] == "Plan the task"
    assert result.artifacts[0].name == "static-executor.md"


def test_claude_cli_provider_wrapper_uses_non_interactive_json(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = json.dumps({"type": "result", "result": "Claude wrapper ok"})

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("xueban.runtime.executors.subprocess.run", fake_run)

    executor = CliExecutor()
    result = asyncio.run(
        executor.execute(
            {
                "kind": "cli",
                "provider": "claude",
                "model": "sonnet",
                "system_prompt": "You are a precise planner.",
                "extra_args": ["--verbose"],
                "timeout_seconds": 15,
            },
            {
                "prompt": "Return one line",
                "step_input": {"goal": "验证 Claude CLI 封装"},
                "context": {},
                "state": {},
                "node": {"id": "planner", "type": "agent.execute", "role": "planner"},
            },
        )
    )

    assert captured["command"][0].lower().endswith(("claude", "claude.exe"))
    assert "-p" in captured["command"]
    assert "--output-format" in captured["command"]
    assert "json" in captured["command"]
    assert "--model" in captured["command"]
    assert "--verbose" in captured["command"]
    assert captured["kwargs"]["timeout"] == 15
    assert result["message"] == "Claude wrapper ok"
    assert result["executor"]["provider"] == "claude"


def test_codex_cli_provider_wrapper_uses_exec_jsonl(monkeypatch):
    captured = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = "\n".join(
            [
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Codex wrapper ok"}}),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 5}}),
            ]
        )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("xueban.runtime.executors.subprocess.run", fake_run)

    executor = CliExecutor()
    result = asyncio.run(
        executor.execute(
            {
                "kind": "cli",
                "provider": "codex",
                "model": "gpt-5-codex",
                "extra_args": ["--full-auto"],
            },
            {
                "prompt": "Return one line",
                "step_input": {"goal": "验证 Codex CLI 封装"},
                "context": {},
                "state": {},
                "node": {"id": "coder", "type": "agent.execute", "role": "coder"},
            },
        )
    )

    assert captured["command"][0].lower().endswith(("codex", "codex.cmd", "codex.exe"))
    assert captured["command"][1:3] == ["exec", "--skip-git-repo-check"]
    assert "--json" in captured["command"]
    assert "--full-auto" in captured["command"]
    assert captured["kwargs"]["input"]
    assert result["message"] == "Codex wrapper ok"
    assert result["executor"]["provider"] == "codex"


def test_api_executor_calls_openai_responses(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "resp_openai_123",
                "output_text": "OpenAI executor ok",
                "model": "gpt-5.2",
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr("xueban.runtime.executors.httpx.AsyncClient", FakeAsyncClient)

    executor = ApiExecutor()
    result = asyncio.run(
        executor.execute(
            {
                "kind": "api",
                "provider": "openai",
                "model": "gpt-5.2",
                "parse": "text",
                "system_prompt": "You are concise.",
            },
            {
                "prompt": "Summarize the goal",
                "step_input": {"goal": "验证 OpenAI API executor"},
                "context": {"source_system": "pytest"},
                "state": {},
                "node": {"id": "planner", "type": "agent.execute", "role": "planner"},
            },
        )
    )

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer test-openai-key"
    assert captured["json"]["model"] == "gpt-5.2"
    assert "验证 OpenAI API executor" in captured["json"]["input"]
    assert result["message"] == "OpenAI executor ok"
    assert result["executor"]["provider"] == "openai"
    assert result["executor"]["model"] == "gpt-5.2"


def test_api_executor_calls_anthropic_messages(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "msg_123",
                "content": [{"type": "text", "text": "{\"message\":\"Anthropic executor ok\"}"}],
                "model": "claude-sonnet-4-20250514",
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr("xueban.runtime.executors.httpx.AsyncClient", FakeAsyncClient)

    executor = ApiExecutor()
    result = asyncio.run(
        executor.execute(
            {
                "kind": "api",
                "provider": "anthropic",
                "parse": "json",
                "system_prompt": "You are structured.",
            },
            {
                "prompt": "Respond with JSON",
                "step_input": {"goal": "验证 Anthropic API executor"},
                "context": {},
                "state": {},
                "node": {"id": "planner", "type": "agent.execute", "role": "planner"},
            },
        )
    )

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "test-anthropic-key"
    assert captured["json"]["model"] == "claude-sonnet-4-20250514"
    assert result["message"] == "Anthropic executor ok"
    assert result["executor"]["provider"] == "anthropic"
    assert result["executor"]["model"] == "claude-sonnet-4-20250514"


def test_cli_executor_rejects_invalid_timeout():
    executor = CliExecutor()

    with pytest.raises(ValueError, match="timeout_seconds"):
        asyncio.run(
            executor.execute(
                {
                    "kind": "cli",
                    "provider": "generic",
                    "command": "python",
                    "args": ["-c", "print('ok')"],
                    "stdin": "none",
                    "parse": "text",
                    "timeout_seconds": 0,
                },
                {
                    "prompt": "",
                    "step_input": {},
                    "context": {},
                    "state": {},
                    "node": {"id": "x", "type": "agent.execute", "role": "x"},
                },
            )
        )


@pytest.mark.parametrize(
    "workflow_path",
    [
        "examples/runtime-contract/cli-agent-execution.yaml",
        "examples/runtime-contract/cli-claude-execution.yaml",
        "examples/runtime-contract/api-agent-execution.yaml",
        "examples/runtime-contract/api-anthropic-execution.yaml",
    ],
)
def test_executor_example_workflows_validate(workflow_path):
    example_path = Path(workflow_path)
    payload = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    workflow = WorkflowDefinition.model_validate(payload)
    assert workflow.workflow_id


def test_workflow_validation_rejects_invalid_executor_kind():
    with pytest.raises(Exception, match="executor.kind"):
        WorkflowDefinition.model_validate(
            {
                "workflow_id": "bad-executor",
                "version": "0.1",
                "name": "Bad Executor",
                "entrypoint": "start",
                "nodes": [
                    {
                        "id": "start",
                        "kind": "agent",
                        "type": "agent.execute",
                        "config": {"executor": {"kind": "unknown"}},
                    }
                ],
                "edges": [{"from": "start", "to": "END", "type": "final"}],
            }
        )



def test_api_executor_calls_zhipu_chat_completions(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "chatcmpl-zhipu-1",
                "choices": [
                    {"message": {"content": "{\"score\": 0.92, \"confidence\": 0.88, \"reasoning\": \"ok\"}"}}
                ],
                "model": "glm-4.5",
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None):
            captured.update({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setenv("ZHIPU_API_KEY", "test-zhipu-key")
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)
    monkeypatch.delenv("ZHIPU_MODEL", raising=False)
    monkeypatch.setattr("xueban.runtime.executors.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        ApiExecutor().execute(
            {
                "kind": "api",
                "provider": "zhipu",
                "parse": "json",
                "temperature": 0,
            },
            {"prompt": "Evaluate quality"},
        )
    )

    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-zhipu-key"
    body = captured["json"]
    assert body["model"] == "glm-4.5"
    assert body["messages"][0] == {"role": "user", "content": "Evaluate quality\n\nReturn valid JSON only."}
    assert body["response_format"] == {"type": "json_object"}
    # chat-completions 解析：message.content → JSON 合并进输出
    assert result["score"] == 0.92
    assert result["executor"] == {"kind": "api", "provider": "zhipu", "model": "glm-4.5"}


def test_api_executor_zhipu_byok_overrides(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "plain answer"}}], "model": "glm-4-flash"}

    class FakeAsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None):
            captured.update({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setenv("MY_ZHIPU_GATEWAY_KEY", "gateway-key")
    monkeypatch.setattr("xueban.runtime.executors.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        ApiExecutor().execute(
            {
                "kind": "api",
                "provider": "zhipu",
                "model": "glm-4-flash",
                "api_key_env": "MY_ZHIPU_GATEWAY_KEY",
                "base_url": "https://gateway.example/v4/chat/completions",
                "parse": "text",
            },
            {"prompt": "hi"},
        )
    )
    assert captured["url"] == "https://gateway.example/v4/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer gateway-key"
    assert result["message"] == "plain answer"

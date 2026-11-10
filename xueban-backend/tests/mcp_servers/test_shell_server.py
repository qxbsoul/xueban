"""Story 11.1 — xueban-shell MCP Server 测试。

覆盖范围：
- AC1  工具列表（list_tools 返回 run / ssh_run / tmux_run）
- AC2  run() 本地 bash 执行（success / error / timeout）
- AC3  ssh_run() 远程执行（mocked paramiko）
- AC4  tmux_run() tmux 持久会话（mocked subprocess）
- AC5  McpClient 集成测试（subprocess 启动真实 server）
- Round-2 patches：KeyError 处理 / timeout 校验 / 空命令 / tmux FileNotFoundError / session 正则 / agent close
"""
from __future__ import annotations

import asyncio
import json
import sys
import time as time_module
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# AC1 — 工具列表
# ---------------------------------------------------------------------------


class TestToolList:
    def test_server_exposes_three_tools(self):
        from xueban.mcp_servers import shell_server

        names = [t.name for t in shell_server.TOOLS]
        assert set(names) == {"run", "ssh_run", "tmux_run"}

    def test_each_tool_has_input_schema(self):
        from xueban.mcp_servers import shell_server

        for tool in shell_server.TOOLS:
            assert tool.inputSchema, f"{tool.name} missing inputSchema"
            assert "properties" in tool.inputSchema

    def test_run_tool_command_required(self):
        from xueban.mcp_servers import shell_server

        run_tool = next(t for t in shell_server.TOOLS if t.name == "run")
        assert "command" in run_tool.inputSchema.get("required", [])

    def test_ssh_run_tool_host_command_required(self):
        from xueban.mcp_servers import shell_server

        ssh_tool = next(t for t in shell_server.TOOLS if t.name == "ssh_run")
        required = ssh_tool.inputSchema.get("required", [])
        assert "host" in required
        assert "command" in required

    def test_tmux_run_tool_session_command_required(self):
        from xueban.mcp_servers import shell_server

        tmux_tool = next(t for t in shell_server.TOOLS if t.name == "tmux_run")
        required = tmux_tool.inputSchema.get("required", [])
        assert "session" in required
        assert "command" in required


# ---------------------------------------------------------------------------
# AC2 — run() 本地执行
# ---------------------------------------------------------------------------


class TestRunTool:
    @pytest.mark.asyncio
    async def test_run_success_echo(self):
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "echo hello"})
        assert result["status"] == "success"
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0
        assert result["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_run_nonzero_exit_is_error(self):
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "python -c \"import sys; sys.exit(42)\""})
        assert result["status"] == "error"
        assert result["exit_code"] == 42

    @pytest.mark.asyncio
    async def test_run_captures_stderr(self):
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "python -c \"import sys; sys.stderr.write('err_msg');\""})
        assert "err_msg" in result["stderr"]

    @pytest.mark.asyncio
    async def test_run_timeout_returns_timeout_status(self):
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "python -c \"import time; time.sleep(10)\"", "timeout": 1})
        assert result["status"] == "timeout"
        assert result["exit_code"] == -1

    @pytest.mark.asyncio
    async def test_run_with_cwd(self):
        import tempfile
        from xueban.mcp_servers.shell_server import _run

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await _run({"command": "python -c \"import os; print(os.getcwd())\"", "cwd": tmpdir})
        assert result["status"] == "success"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_run_result_keys_present(self):
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "echo ok"})
        for key in ("status", "stdout", "stderr", "exit_code", "duration_ms"):
            assert key in result, f"missing key: {key}"

    # --- Round-2 patch coverage ---

    @pytest.mark.asyncio
    async def test_run_empty_command_returns_error(self):
        """P9: 空命令应返回 error，不应静默 exit_code=0。"""
        from xueban.mcp_servers.shell_server import _run

        for bad in ["", "   ", "\t"]:
            result = await _run({"command": bad})
            assert result["status"] == "error", f"Expected error for command={bad!r}"

    @pytest.mark.asyncio
    async def test_run_timeout_zero_returns_error(self):
        """P8: timeout=0 应返回 error 而非立即 TimeoutExpired。"""
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "echo hi", "timeout": 0})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_run_timeout_negative_returns_error(self):
        """P8: 负 timeout 应返回 error。"""
        from xueban.mcp_servers.shell_server import _run

        result = await _run({"command": "echo hi", "timeout": -5})
        assert result["status"] == "error"


class TestHandleCallToolKeyError:
    @pytest.mark.asyncio
    async def test_missing_command_returns_error_not_exception(self):
        """P2: 缺少必需 key 时 _handle_call_tool 应返回 error JSON，不抛 KeyError。"""
        from xueban.mcp_servers.shell_server import _handle_call_tool

        results = await _handle_call_tool("run", {})  # missing "command"
        assert len(results) == 1
        payload = json.loads(results[0].text)
        assert payload["status"] == "error"
        assert "command" in payload["stderr"].lower() or "missing" in payload["stderr"].lower()

    @pytest.mark.asyncio
    async def test_missing_host_returns_error_not_exception(self):
        """P2: ssh_run 缺少 host 时应返回 error JSON。"""
        from xueban.mcp_servers.shell_server import _handle_call_tool

        results = await _handle_call_tool("ssh_run", {"command": "ls"})
        payload = json.loads(results[0].text)
        assert payload["status"] == "error"


# ---------------------------------------------------------------------------
# AC3 — ssh_run() 远程执行（mocked paramiko）
# ---------------------------------------------------------------------------


class TestSshRunTool:
    @pytest.mark.asyncio
    async def test_ssh_run_success_mocked(self):
        from xueban.mcp_servers import shell_server

        # Build mock channel + streams
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"remote output\n"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client = MagicMock()
        mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)

        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []  # no agent keys → fall through to key_path

        with patch.dict(sys.modules, {}):
            with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
                mock_paramiko.SSHClient.return_value = mock_client
                mock_paramiko.RejectPolicy.return_value = MagicMock()
                mock_paramiko.Agent.return_value = mock_agent

                result = await shell_server._ssh_run(
                    {"host": "example.com", "command": "echo hi", "key_path": "/fake/key"}
                )

        assert result["status"] == "success"
        assert "remote output" in result["stdout"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_ssh_run_uses_reject_policy_not_autoadd(self):
        """Review patch: 必须使用 RejectPolicy 防止 MITM，禁止 AutoAddPolicy。"""
        from xueban.mcp_servers import shell_server

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"ok"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client = MagicMock()
        mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)
        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            await shell_server._ssh_run({"host": "host", "command": "pwd"})

        # RejectPolicy must be instantiated; AutoAddPolicy must NOT be called
        mock_paramiko.RejectPolicy.assert_called()
        mock_paramiko.AutoAddPolicy.assert_not_called()

    @pytest.mark.asyncio
    async def test_ssh_run_closes_client_on_exception(self):
        """Review patch: SSH client.close() 例外時も必ず呼ぶ。"""
        from xueban.mcp_servers import shell_server

        mock_client = MagicMock()
        mock_client.connect.side_effect = Exception("connection refused")
        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            result = await shell_server._ssh_run({"host": "host", "command": "ls"})

        assert result["status"] == "error"
        mock_client.close.assert_called()  # must be called even on exception

    @pytest.mark.asyncio
    async def test_ssh_run_closes_agent_on_success(self):
        """P6: paramiko.Agent socket 在成功路径下也必须 close()。"""
        from xueban.mcp_servers import shell_server

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"ok"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client = MagicMock()
        mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)
        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            await shell_server._ssh_run({"host": "host", "command": "ls"})

        mock_agent.close.assert_called()

    @pytest.mark.asyncio
    async def test_ssh_run_closes_agent_on_exception(self):
        """P6: paramiko.Agent socket 在异常路径下也必须 close()。"""
        from xueban.mcp_servers import shell_server

        mock_client = MagicMock()
        mock_client.connect.side_effect = Exception("refused")
        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            await shell_server._ssh_run({"host": "host", "command": "ls"})

        mock_agent.close.assert_called()

    @pytest.mark.asyncio
    async def test_ssh_run_uses_agent_key_when_available(self):
        from xueban.mcp_servers import shell_server

        fake_key = MagicMock()
        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"ok"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client = MagicMock()
        mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)

        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = [fake_key]

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            result = await shell_server._ssh_run({"host": "host", "command": "pwd"})

        # pkey 应该是 agent key，不是 key_path
        call_kwargs = mock_client.connect.call_args[1]
        assert call_kwargs.get("pkey") == fake_key
        assert "key_filename" not in call_kwargs

    @pytest.mark.asyncio
    async def test_ssh_run_no_key_in_result(self):
        """SSH 密钥不得出现在响应字段中（AC3 安全要求）。"""
        from xueban.mcp_servers import shell_server

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"data"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client = MagicMock()
        mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)
        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            result = await shell_server._ssh_run(
                {"host": "host", "command": "echo", "key_path": "SECRET_KEY_PATH"}
            )

        # key_path 不应出现在 stdout/stderr 中
        assert "SECRET_KEY_PATH" not in result.get("stdout", "")
        assert "SECRET_KEY_PATH" not in result.get("stderr", "")

    @pytest.mark.asyncio
    async def test_ssh_run_paramiko_missing_returns_error(self):
        from xueban.mcp_servers import shell_server

        with patch.dict(sys.modules, {"paramiko": None}):  # type: ignore[dict-item]
            # Force reimport to detect missing module
            original = shell_server.paramiko
            shell_server.paramiko = None  # type: ignore[assignment]
            try:
                result = await shell_server._ssh_run({"host": "h", "command": "ls"})
                assert result["status"] == "error"
                assert "paramiko" in result["stderr"].lower()
            finally:
                shell_server.paramiko = original  # type: ignore[assignment]

    @pytest.mark.asyncio
    async def test_ssh_run_connect_timeout_set(self):
        """P7: connect() 必须携带 timeout 参数，防止永久阻塞。"""
        from xueban.mcp_servers import shell_server

        mock_stdout = MagicMock()
        mock_stdout.read.return_value = b"ok"
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = b""

        mock_client = MagicMock()
        mock_client.exec_command.return_value = (None, mock_stdout, mock_stderr)
        mock_agent = MagicMock()
        mock_agent.get_keys.return_value = []

        with patch("xueban.mcp_servers.shell_server.paramiko") as mock_paramiko:
            mock_paramiko.SSHClient.return_value = mock_client
            mock_paramiko.RejectPolicy.return_value = MagicMock()
            mock_paramiko.Agent.return_value = mock_agent

            await shell_server._ssh_run({"host": "host", "command": "ls"})

        call_kwargs = mock_client.connect.call_args[1]
        assert "timeout" in call_kwargs, "connect() must have a timeout parameter"
        assert call_kwargs["timeout"] > 0


# ---------------------------------------------------------------------------
# AC4 — tmux_run() tmux 持久会话（mocked subprocess）
# ---------------------------------------------------------------------------


class TestTmuxRunTool:
    @pytest.mark.asyncio
    async def test_tmux_run_existing_session(self):
        from xueban.mcp_servers.shell_server import _tmux_run

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.side_effect = [
                MagicMock(returncode=0),  # has-session → exists
                MagicMock(returncode=0),  # send-keys
                MagicMock(returncode=0, stdout=b"output line\n"),  # capture-pane
            ]
            result = await _tmux_run({"session": "sf-build", "command": "npm run build"})

        assert result["status"] == "success"
        assert "output line" in result["stdout"]

    @pytest.mark.asyncio
    async def test_tmux_run_creates_session_if_missing(self):
        from xueban.mcp_servers.shell_server import _tmux_run

        calls = []

        def track_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["tmux", "has-session", "-t", "new-sess"]:
                return MagicMock(returncode=1)  # not found
            return MagicMock(returncode=0, stdout=b"pane output")

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.side_effect = track_run
            await _tmux_run({"session": "new-sess", "command": "echo hi"})

        # new-session must be called
        new_sess_calls = [c for c in calls if "new-session" in c]
        assert new_sess_calls, "new-session not called for missing session"

    @pytest.mark.asyncio
    async def test_tmux_run_send_keys_failure_returns_error(self):
        from xueban.mcp_servers.shell_server import _tmux_run

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.side_effect = [
                MagicMock(returncode=0),  # has-session
                MagicMock(returncode=1, stderr=b"tmux error"),  # send-keys fails
            ]
            result = await _tmux_run({"session": "s", "command": "bad cmd"})

        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_tmux_run_rejects_invalid_session_name(self):
        """Review patch: tmux session 名称含空格/换行时应返回 error，不能传递到子命令。"""
        from xueban.mcp_servers.shell_server import _tmux_run

        for bad_name in ["session with space", "session\nnewline", "session;inject", "$(evil)"]:
            result = await _tmux_run({"session": bad_name, "command": "echo hi"})
            assert result["status"] == "error", f"Expected error for session name: {bad_name!r}"
            assert "invalid" in result["stderr"].lower() or "session" in result["stderr"].lower()

    @pytest.mark.asyncio
    async def test_tmux_run_accepts_valid_session_names(self):
        """合法 session 名称（字母数字 hyphen underscore dot）应正常通过校验。"""
        from xueban.mcp_servers.shell_server import _tmux_run

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.return_value = MagicMock(returncode=0, stdout=b"out", stderr=b"")
            for good_name in ["sf-build", "session_1", "my.session", "Session123"]:
                result = await _tmux_run({"session": good_name, "command": "echo hi"})
                assert result["status"] == "success", f"Expected success for session name: {good_name!r}"

    @pytest.mark.asyncio
    async def test_tmux_run_rejects_leading_dot_in_session_name(self):
        """P11: session 名称不允许以点开头（tmux 拒绝 .session）。"""
        from xueban.mcp_servers.shell_server import _tmux_run

        result = await _tmux_run({"session": ".hidden", "command": "echo hi"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_tmux_run_new_session_failure_returns_clear_error(self):
        """P5: new-session 失败时应返回明确错误，而不是继续执行 send-keys。"""
        from xueban.mcp_servers.shell_server import _tmux_run

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.side_effect = [
                MagicMock(returncode=1),  # has-session → not found
                MagicMock(returncode=1, stderr=b"tmux: session creation failed"),  # new-session fails
            ]
            result = await _tmux_run({"session": "s", "command": "echo hi"})

        assert result["status"] == "error"
        assert "session" in result["stderr"].lower() or "failed" in result["stderr"].lower()

    @pytest.mark.asyncio
    async def test_tmux_run_subprocess_use_devnull_stdin(self):
        """P4: 所有 subprocess.run 调用必须传 stdin=subprocess.DEVNULL。"""
        from xueban.mcp_servers import shell_server
        import subprocess as sp_module

        calls_kwargs: list[dict] = []

        def capture_call(cmd, **kwargs):
            calls_kwargs.append(kwargs)
            return MagicMock(returncode=0, stdout=b"out", stderr=b"")

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.side_effect = capture_call
            mock_sub.DEVNULL = sp_module.DEVNULL
            await shell_server._tmux_run({"session": "s", "command": "echo hi"})

        for i, kw in enumerate(calls_kwargs):
            assert kw.get("stdin") == sp_module.DEVNULL, (
                f"subprocess.run call #{i} missing stdin=DEVNULL"
            )

    @pytest.mark.asyncio
    async def test_tmux_run_sanitizes_newline_in_command(self):
        """P12: command 中的 \\n 应被替换为空格，不能传递到 send-keys。"""
        from xueban.mcp_servers.shell_server import _tmux_run

        sent_commands: list[str] = []

        def capture_run(cmd, **kwargs):
            if "send-keys" in cmd:
                # cmd is ["tmux", "send-keys", "-t", session, COMMAND, "Enter"]
                sent_commands.append(cmd[4])
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub, \
             patch("xueban.mcp_servers.shell_server._TMUX_CAPTURE_DELAY_S", 0):
            mock_sub.run.side_effect = capture_run
            await _tmux_run({"session": "s", "command": "echo first\necho second"})

        assert len(sent_commands) == 1
        assert "\n" not in sent_commands[0], "newline must be sanitized before send-keys"

    @pytest.mark.asyncio
    async def test_tmux_run_file_not_found_returns_friendly_error(self):
        """P10: tmux 不存在时应返回友好错误，不抛 FileNotFoundError。"""
        from xueban.mcp_servers.shell_server import _tmux_run

        with patch("xueban.mcp_servers.shell_server.subprocess") as mock_sub:
            mock_sub.run.side_effect = FileNotFoundError("tmux not found")
            result = await _tmux_run({"session": "s", "command": "echo hi"})

        assert result["status"] == "error"
        assert "tmux" in result["stderr"].lower()


# ---------------------------------------------------------------------------
# AC5 — McpClient 集成测试（启动真实 subprocess server）
# ---------------------------------------------------------------------------


class TestMcpClientIntegration:
    @pytest.mark.asyncio
    async def test_list_tools_via_client(self):
        """McpClient 连接 shell_server，list_tools 返回三个工具名（且类型为 list）。"""
        from xueban.runtime.mcp.client import McpClient
        from xueban.runtime.mcp.transport import McpTransportConfig

        cfg = McpTransportConfig(
            kind="stdio",
            command=[sys.executable, "-m", "xueban.mcp_servers.shell_server"],
        )
        client = McpClient(cfg)
        try:
            await client.connect()
            tools = await client.list_tools()
            # P13: 验证返回类型为 list，不仅仅做 set 比较
            assert isinstance(tools, list), f"list_tools() must return a list, got {type(tools)}"
            assert set(tools) == {"run", "ssh_run", "tmux_run"}
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_call_run_via_client(self):
        """McpClient 调用 run 工具，返回包含当前工作目录路径的 ToolResult。"""
        import os
        from xueban.runtime.mcp.client import McpClient
        from xueban.runtime.mcp.transport import McpTransportConfig

        cfg = McpTransportConfig(
            kind="stdio",
            command=[sys.executable, "-m", "xueban.mcp_servers.shell_server"],
        )
        client = McpClient(cfg)
        try:
            await client.connect()
            raw = await client.call_tool("run", {"command": "python -c \"import os; print(os.getcwd())\""})
            # raw is a CallToolResult; content[0].text is the JSON string
            content_text = raw.content[0].text
            result = json.loads(content_text)
            assert result["status"] == "success"
            assert result["exit_code"] == 0
            # P14: 验证 stdout 实际包含路径（而不只是非空）
            stdout = result["stdout"].strip()
            assert len(stdout) > 0, "stdout must not be empty"
            assert os.sep in stdout or "/" in stdout, (
                f"stdout should contain a directory path separator, got: {stdout!r}"
            )
        finally:
            await client.close()

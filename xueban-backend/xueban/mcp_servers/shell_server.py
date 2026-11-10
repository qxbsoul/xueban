"""xueban-shell MCP Server — bash / SSH / tmux 执行工具。

Story 11.1：暴露三个工具给 LLM Agent：
  run(command, cwd?, timeout?)      — 本地 shell 执行
  ssh_run(host, command, key_path?) — 远程 SSH 执行
  tmux_run(session, command)        — tmux 持久会话执行

启动方式：
  python -m xueban.mcp_servers.shell_server
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from typing import Any

# subprocess 模块单独引用，便于在 _run 线程中使用而不被 mock 覆盖
import subprocess as _subprocess

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# 懒导入 paramiko：未安装时保持 None，在 _ssh_run 中给出友好错误
try:
    import paramiko  # type: ignore[import]
except ImportError:
    paramiko = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Server 实例 & 工具定义
# ---------------------------------------------------------------------------

app = Server("xueban-shell")

TOOLS: list[types.Tool] = [
    types.Tool(
        name="run",
        description="在本地环境执行 shell 命令，返回 stdout / stderr / exit_code。",
        inputSchema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "cwd": {"type": "string", "description": "工作目录（可选）"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 30，最小 1",
                    "default": 30,
                    "minimum": 1,
                },
            },
            "required": ["command"],
        },
    ),
    types.Tool(
        name="ssh_run",
        description="通过 SSH 在远程主机执行命令，返回与 run 相同结构的结果。",
        inputSchema={
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "远程主机地址"},
                "command": {"type": "string", "description": "要执行的命令"},
                "key_path": {
                    "type": "string",
                    "description": "SSH 私钥路径（可选，优先使用 SSH Agent）",
                },
            },
            "required": ["host", "command"],
        },
    ),
    types.Tool(
        name="tmux_run",
        description=(
            "在指定 tmux session 发送命令并捕获输出，session 不存在时自动创建。"
            " 注意：exit_code 始终为 0（tmux send-keys 架构限制，无法捕获真实退出码）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "tmux session 名称（仅字母数字 hyphen underscore dot，且以字母数字开头）"},
                "command": {"type": "string", "description": "要发送的命令"},
            },
            "required": ["session", "command"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP 协议处理器
# ---------------------------------------------------------------------------


@app.list_tools()
async def _handle_list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def _handle_call_tool(
    name: str, arguments: dict[str, Any]
) -> list[types.TextContent]:
    # P2: 捕获缺少必需 key 时的 KeyError，返回友好错误而不是崩溃
    try:
        if name == "run":
            result = await _run(arguments)
        elif name == "ssh_run":
            result = await _ssh_run(arguments)
        elif name == "tmux_run":
            result = await _tmux_run(arguments)
        else:
            result = {
                "status": "error",
                "stdout": "",
                "stderr": f"Unknown tool: {name}",
                "exit_code": -1,
                "duration_ms": 0,
            }
    except KeyError as exc:
        result = {
            "status": "error",
            "stdout": "",
            "stderr": f"Missing required argument: {exc}",
            "exit_code": -1,
            "duration_ms": 0,
        }
    return [types.TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


async def _run(args: dict[str, Any]) -> dict[str, Any]:
    """AC2：本地 shell 命令执行。

    使用 run_in_executor + subprocess.run 避免 Windows 上
    asyncio.create_subprocess_shell 的 pipe handle 继承问题。
    """
    command: str = args["command"]
    # P9: 拒绝空命令（shell=True 对空字符串静默返回 exit_code=0）
    if not command.strip():
        return {
            "status": "error",
            "stdout": "",
            "stderr": "command must not be empty",
            "exit_code": -1,
            "duration_ms": 0,
        }

    cwd: str | None = args.get("cwd")
    timeout = args.get("timeout", 30)
    # P8: 校验 timeout 为正数，防止 timeout=0/负值导致立即 TimeoutExpired 或未定义行为
    try:
        timeout_f = float(timeout)
    except (TypeError, ValueError):
        timeout_f = 30.0
    if timeout_f <= 0:
        return {
            "status": "error",
            "stdout": "",
            "stderr": "timeout must be a positive number (seconds)",
            "exit_code": -1,
            "duration_ms": 0,
        }

    def _run_sync() -> dict[str, Any]:
        # 在线程内计时，排除线程池排队等待时间
        start_ms = time.monotonic() * 1000
        try:
            result = _subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                stdout=_subprocess.PIPE,
                stderr=_subprocess.PIPE,
                stdin=_subprocess.DEVNULL,  # 不继承 MCP server 的 stdin
                timeout=timeout_f,
            )
            exit_code = result.returncode if result.returncode is not None else -1
            return {
                "status": "success" if exit_code == 0 else "error",
                "stdout": result.stdout.decode(errors="replace"),
                "stderr": result.stderr.decode(errors="replace"),
                "exit_code": exit_code,
                "duration_ms": int(time.monotonic() * 1000 - start_ms),
            }
        except _subprocess.TimeoutExpired as exc:
            return {
                "status": "timeout",
                "stdout": (exc.stdout or b"").decode(errors="replace"),
                "stderr": (exc.stderr or b"").decode(errors="replace"),
                "exit_code": -1,
                "duration_ms": int(time.monotonic() * 1000 - start_ms),
            }
        except Exception as exc:
            return {
                "status": "error",
                "stdout": "",
                "stderr": str(exc),
                "exit_code": -1,
                "duration_ms": int(time.monotonic() * 1000 - start_ms),
            }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_sync)


async def _ssh_run(args: dict[str, Any]) -> dict[str, Any]:
    """AC3：远程 SSH 执行。凭据优先从 SSH Agent 加载，其次 key_path。"""
    if paramiko is None:
        return {
            "status": "error",
            "stdout": "",
            "stderr": "paramiko not installed; run: pip install paramiko",
            "exit_code": -1,
            "duration_ms": 0,
        }

    host: str = args["host"]
    command: str = args["command"]
    key_path: str | None = args.get("key_path")

    start_ms = time.monotonic() * 1000
    client = paramiko.SSHClient()
    # P6: agent 需在 finally 中 close()，初始化为 None 防止 finally 中 AttributeError
    agent: Any = None
    try:
        # Use system known_hosts and reject unknown keys to prevent MITM attacks.
        # The target host must already be in ~/.ssh/known_hosts.
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        # P7: 为 connect() 添加 30s 超时，防止网络不可达时永久阻塞线程池
        connect_kwargs: dict[str, Any] = {"hostname": host, "timeout": 30}

        # 优先使用 SSH Agent
        agent = paramiko.Agent()
        agent_keys = agent.get_keys()
        if agent_keys:
            connect_kwargs["pkey"] = agent_keys[0]
        elif key_path:
            connect_kwargs["key_filename"] = key_path
        # 密钥信息不写入响应或日志

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: client.connect(**connect_kwargs))
        _, stdout_ch, stderr_ch = await loop.run_in_executor(
            None, lambda: client.exec_command(command)
        )

        # P1: recv_exit_status + reads 是阻塞 I/O，必须在 executor 中执行
        def _read_result() -> tuple[int, str, str]:
            ec = stdout_ch.channel.recv_exit_status()
            out = stdout_ch.read().decode(errors="replace")
            err = stderr_ch.read().decode(errors="replace")
            return ec, out, err

        exit_code, stdout_text, stderr_text = await loop.run_in_executor(
            None, _read_result
        )

        return {
            "status": "success" if exit_code == 0 else "error",
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "duration_ms": int(time.monotonic() * 1000 - start_ms),
        }
    except Exception as exc:
        return {
            "status": "error",
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "duration_ms": int(time.monotonic() * 1000 - start_ms),
        }
    finally:
        # P6: agent socket 必须在 finally 中关闭，防止 FD 泄漏
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
        client.close()


# P11: session 名称不允许前导点（tmux 拒绝），且只允许 alphanum/hyphen/underscore/dot
_TMUX_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

# 可在测试中 patch 为 0 以避免真实等待
_TMUX_CAPTURE_DELAY_S: float = 5.0


def _tmux_run_sync(session: str, command: str) -> dict[str, Any]:
    """P3: 所有 tmux subprocess 调用在此同步函数中执行，由 run_in_executor 调度。"""
    start_ms = time.monotonic() * 1000
    try:
        # session 不存在时自动创建
        has = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            stdin=subprocess.DEVNULL,  # P4: 不继承 MCP server stdin
        )
        if has.returncode != 0:
            new_sess = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session],
                capture_output=True,
                stdin=subprocess.DEVNULL,  # P4
            )
            # P5: 检查 new-session 是否成功，失败时返回明确错误而非继续执行
            if new_sess.returncode != 0:
                return {
                    "status": "error",
                    "stdout": "",
                    "stderr": (
                        f"Failed to create tmux session '{session}': "
                        + new_sess.stderr.decode(errors="replace")
                    ),
                    "exit_code": new_sess.returncode,
                    "duration_ms": int(time.monotonic() * 1000 - start_ms),
                }

        # 发送命令
        send = subprocess.run(
            ["tmux", "send-keys", "-t", session, command, "Enter"],
            capture_output=True,
            stdin=subprocess.DEVNULL,  # P4
        )
        if send.returncode != 0:
            return {
                "status": "error",
                "stdout": "",
                "stderr": send.stderr.decode(errors="replace"),
                "exit_code": send.returncode,
                "duration_ms": int(time.monotonic() * 1000 - start_ms),
            }

        # 等待命令执行（已知限制：固定等待无法保证命令完成）
        time.sleep(_TMUX_CAPTURE_DELAY_S)

        # 捕获 pane 输出
        capture = subprocess.run(
            ["tmux", "capture-pane", "-pt", session],
            capture_output=True,
            stdin=subprocess.DEVNULL,  # P4
        )
        return {
            "status": "success",
            "stdout": capture.stdout.decode(errors="replace"),
            "stderr": "",
            "exit_code": 0,  # Known limitation: send-keys 无法捕获真实退出码
            "duration_ms": int(time.monotonic() * 1000 - start_ms),
        }
    except FileNotFoundError:
        # P10: tmux 在 Windows 等平台不存在时给出友好错误
        return {
            "status": "error",
            "stdout": "",
            "stderr": "tmux not found; please install tmux (not available on Windows by default)",
            "exit_code": -1,
            "duration_ms": int(time.monotonic() * 1000 - start_ms),
        }


async def _tmux_run(args: dict[str, Any]) -> dict[str, Any]:
    """AC4：在 tmux session 发送命令，等待后捕获 pane 输出。"""
    session: str = args["session"]
    command: str = args["command"]

    # Validate session name to prevent argument injection via tmux sub-commands.
    if not _TMUX_SESSION_RE.match(session):
        return {
            "status": "error",
            "stdout": "",
            "stderr": (
                f"Invalid tmux session name '{session}': must start with alphanumeric, "
                "only alphanumeric, hyphen, underscore and dot allowed"
            ),
            "exit_code": -1,
            "duration_ms": 0,
        }

    # P12: 净化 command 中的换行符，防止 tmux send-keys 提前执行
    safe_command = command.replace("\n", " ").replace("\r", " ")

    # P3: 所有阻塞 subprocess 调用在 executor 线程中执行，不阻塞事件循环
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _tmux_run_sync(session, safe_command)
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

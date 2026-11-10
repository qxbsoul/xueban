"""Story 2.3 — ACP transport JSON-RPC framing 测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from xueban.runtime.acp.transport import AcpTransport, AcpSessionTerminated


# ---------------------------------------------------------------------------
# Helpers — mock process
# ---------------------------------------------------------------------------

def _make_frame(msg: Dict[str, Any]) -> bytes:
    """Build a Content-Length framed message."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _mock_process(frames: list[bytes]) -> MagicMock:
    """Create a mock asyncio subprocess with pre-loaded response frames."""
    proc = MagicMock()
    proc.returncode = 0
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()

    # Concatenate all frames into a single buffer
    buf = b"".join(frames)
    buf_view = [buf]  # mutable container so closure works

    async def readline():
        data = buf_view[0]
        idx = data.find(b"\n")
        if idx < 0:
            line, buf_view[0] = data, b""
        else:
            line, buf_view[0] = data[:idx + 1], data[idx + 1:]
        return line

    async def readexactly(n):
        chunk, buf_view[0] = buf_view[0][:n], buf_view[0][n:]
        return chunk

    proc.stdout = MagicMock()
    proc.stdout.readline = readline
    proc.stdout.readexactly = readexactly
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.terminate = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


# ---------------------------------------------------------------------------
# Content-Length framing
# ---------------------------------------------------------------------------

class TestAcpFraming:
    def test_make_frame_structure(self):
        msg = {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}}
        body = json.dumps(msg).encode("utf-8")
        frame = _make_frame(msg)
        assert frame.startswith(b"Content-Length:")
        assert b"\r\n\r\n" in frame
        assert frame.endswith(body)

    @pytest.mark.asyncio
    async def test_transport_send_writes_framed_message(self):
        transport = AcpTransport(["echo"])
        mock_proc = _mock_process([])
        transport._process = mock_proc

        msg = {"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": {}}
        await transport.send(msg)

        written = mock_proc.stdin.write.call_args[0][0]
        assert b"Content-Length:" in written
        assert b"initialize" in written

    @pytest.mark.asyncio
    async def test_transport_read_parses_framed_message(self):
        response = {"jsonrpc": "2.0", "id": "1", "result": {"capabilities": {}}}
        frame = _make_frame(response)
        transport = AcpTransport(["echo"])
        transport._process = _mock_process([frame])

        parsed = await transport._read_message()
        assert parsed is not None
        assert parsed["id"] == "1"
        assert parsed["result"] == {"capabilities": {}}

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_none(self):
        transport = AcpTransport(["echo"])
        transport._process = _mock_process([b""])
        result = await transport._read_message()
        assert result is None


# ---------------------------------------------------------------------------
# AcpSessionTerminated
# ---------------------------------------------------------------------------

class TestAcpSessionTerminated:
    def test_exception_carries_exit_code(self):
        exc = AcpSessionTerminated(exit_code=1, stderr_tail="fatal error")
        assert exc.exit_code == 1
        assert exc.stderr_tail == "fatal error"

    def test_str_includes_exit_code(self):
        exc = AcpSessionTerminated(exit_code=42)
        assert "42" in str(exc)


# ---------------------------------------------------------------------------
# Transport request / response matching
# ---------------------------------------------------------------------------

class TestTransportRequestResponse:
    @pytest.mark.asyncio
    async def test_request_resolves_on_matching_id(self):
        response = {"jsonrpc": "2.0", "id": "req-abc", "result": {"sessionId": "sess-1"}}
        frame = _make_frame(response)
        transport = AcpTransport(["echo"])
        mock_proc = _mock_process([frame])
        transport._process = mock_proc

        # Manually inject future
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        transport._pending["req-abc"] = future

        # Simulate reader resolving it
        msg = await transport._read_message()
        transport._pending.pop("req-abc", None)
        future.set_result(msg)

        result = await asyncio.wait_for(future, timeout=1.0)
        assert result["result"]["sessionId"] == "sess-1"

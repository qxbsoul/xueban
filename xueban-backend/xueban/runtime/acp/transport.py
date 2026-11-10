"""ACP stdio JSON-RPC transport layer (Story 2.3).

Uses LSP-style Content-Length framing over stdin/stdout:
    Content-Length: N\\r\\n\\r\\n<N bytes of JSON>

Windows note: subprocess pipes use asyncio.create_subprocess_exec with
PIPE for stdin/stdout. \\r\\n normalization handled by stripping \\r.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional


class AcpSessionTerminated(Exception):
    """Raised when the ACP subprocess terminates unexpectedly."""

    def __init__(self, exit_code: Optional[int], stderr_tail: str = "") -> None:
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        super().__init__(f"ACP session terminated: exit_code={exit_code}")


class AcpTransport:
    """Manages the stdio JSON-RPC channel to an ACP agent process.

    Usage:
        async with AcpTransport(["hermes", "acp"]) as transport:
            await transport.send(msg_dict)
            response = await transport.receive()
    """

    HEADER_CONTENT_LENGTH = b"Content-Length"

    def __init__(self, command: List[str]) -> None:
        self._command = command
        self._process: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._notifications: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                pass

    async def __aenter__(self) -> "AcpTransport":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    async def send(self, message: Dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("AcpTransport not started")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        await self._process.stdin.drain()

    async def request(self, message: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """Send a request and await its response by id."""
        msg_id = message.get("id")
        if msg_id is None:
            raise ValueError("request message must have an id")
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[str(msg_id)] = future
        await self.send(message)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(str(msg_id), None)
            raise TimeoutError(f"ACP request {msg_id} timed out after {timeout}s") from exc

    async def receive_notification(self, timeout: float = 60.0) -> Dict[str, Any]:
        """Get the next notification from the queue."""
        return await asyncio.wait_for(self._notifications.get(), timeout=timeout)

    async def notifications(self) -> AsyncIterator[Dict[str, Any]]:
        """Iterate notifications until the session ends."""
        while True:
            try:
                msg = await self._notifications.get()
                yield msg
                if msg.get("method") == "session.update":
                    params = msg.get("params", {})
                    if params.get("type") in ("completed", "failed", "error"):
                        return
            except asyncio.CancelledError:
                return

    async def _reader_loop(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        try:
            while True:
                msg = await self._read_message()
                if msg is None:
                    break
                msg_id = msg.get("id")
                # If it's a response to a pending request, resolve the future
                if msg_id is not None and "result" in msg or (msg_id is not None and "error" in msg):
                    future = self._pending.pop(str(msg_id), None)
                    if future is not None and not future.done():
                        if "error" in msg:
                            future.set_exception(RuntimeError(str(msg["error"])))
                        else:
                            future.set_result(msg)
                else:
                    # Notification
                    await self._notifications.put(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            # Resolve all pending futures with an error
            exit_code = None
            stderr_tail = ""
            if self._process is not None:
                exit_code = self._process.returncode
                if self._process.stderr is not None:
                    try:
                        raw = await asyncio.wait_for(self._process.stderr.read(8192), timeout=1.0)
                        stderr_tail = raw.decode("utf-8", errors="replace")
                    except Exception:
                        pass
            error = AcpSessionTerminated(exit_code, stderr_tail)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def _read_message(self) -> Optional[Dict[str, Any]]:
        if self._process is None or self._process.stdout is None:
            return None
        # Read headers until blank line
        content_length = 0
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return None
            line = line.rstrip(b"\r\n")
            if not line:
                break
            if line.startswith(b"Content-Length:"):
                try:
                    content_length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    pass
        if content_length <= 0:
            return None
        body = await self._process.stdout.readexactly(content_length)
        return json.loads(body.decode("utf-8"))

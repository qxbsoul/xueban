"""
Xueban MCP Server

Exposes Xueban workflow execution as an MCP tool so Claude Code,
Cursor, and other MCP-compatible hosts can run workflows natively.

Tool: run_workflow
  Input:
    workflow_path (str)  — path to .yaml workflow file
    input_text    (str)  — workflow input (text or JSON)
    store         (str)  — "file" | "memory"  (default "file")

  Output: streaming text — one line per node completion, then final JSON.

Run via:
    xueban mcp [--port 3002]
    # or stdio mode (default, required by most MCP hosts):
    xueban mcp --stdio
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from typing import Any, AsyncIterator, Dict

# ---------------------------------------------------------------------------
# MCP protocol types (minimal, no external dependency)
# ---------------------------------------------------------------------------

JSONRPC = "2.0"


def _ok(id_: Any, result: Any) -> Dict:
    return {"jsonrpc": JSONRPC, "id": id_, "result": result}


def _err(id_: Any, code: int, message: str) -> Dict:
    return {"jsonrpc": JSONRPC, "id": id_, "error": {"code": code, "message": message}}


TOOLS = [
    {
        "name": "run_workflow",
        "description": (
            "Run a Xueban YAML workflow end-to-end. "
            "Streams one progress line per node, then returns final JSON "
            "with run status, steps, and checkpoint IDs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_path": {
                    "type": "string",
                    "description": "Path to the .yaml workflow file",
                },
                "input_text": {
                    "type": "string",
                    "description": "Input text or JSON for the workflow",
                },
                "store": {
                    "type": "string",
                    "enum": ["file", "memory"],
                    "default": "file",
                    "description": "Checkpoint store backend",
                },
            },
            "required": ["workflow_path", "input_text"],
        },
    }
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _execute_run_workflow(args: Dict) -> str:
    """Run a workflow and return the result JSON as a string."""
    from xueban.cli import run_workflow

    workflow_path: str = args["workflow_path"]
    input_text: str = args["input_text"]
    store: str = args.get("store", "file")

    buf = io.StringIO()
    with redirect_stdout(buf):
        await run_workflow(
            workflow_path,
            input_text,
            "default",
            "reference",
            None,
            store=store,
        )
    return buf.getvalue().strip()


# ---------------------------------------------------------------------------
# Stdio transport (JSON-RPC over stdin/stdout)
# ---------------------------------------------------------------------------

async def _handle_request(request: Dict) -> Dict | None:
    """Handle one JSON-RPC request; return response dict or None for notifications."""
    method: str = request.get("method", "")
    id_ = request.get("id")
    params: Dict = request.get("params", {})

    if method == "initialize":
        return _ok(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "xueban", "version": "0.3.0"},
        })

    if method == "tools/list":
        return _ok(id_, {"tools": TOOLS})

    if method == "tools/call":
        tool_name: str = params.get("name", "")
        tool_args: Dict = params.get("arguments", {})

        if tool_name != "run_workflow":
            return _err(id_, -32601, f"Unknown tool: {tool_name}")

        try:
            result_text = await _execute_run_workflow(tool_args)
            return _ok(id_, {
                "content": [{"type": "text", "text": result_text}],
                "isError": False,
            })
        except Exception as exc:
            return _ok(id_, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })

    # Ignore notifications (no id)
    if id_ is None:
        return None

    return _err(id_, -32601, f"Method not found: {method}")


async def run_stdio() -> None:
    """Run MCP server in stdio mode (required by Claude Code / Cursor)."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.BaseProtocol, sys.stdout.buffer
    )

    def _write(obj: Dict) -> None:
        line = json.dumps(obj) + "\n"
        write_transport.write(line.encode("utf-8"))

    while True:
        try:
            line = await reader.readline()
        except Exception:
            break
        if not line:
            break
        try:
            request = json.loads(line.decode("utf-8").strip())
        except json.JSONDecodeError:
            continue
        response = await _handle_request(request)
        if response is not None:
            _write(response)


# ---------------------------------------------------------------------------
# HTTP transport (optional, for development / debugging)
# ---------------------------------------------------------------------------

async def run_http(host: str = "127.0.0.1", port: int = 3002) -> None:
    """Run a minimal HTTP wrapper for debugging (not standard MCP)."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                request = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"invalid JSON")
                return

            response = asyncio.run(_handle_request(request))
            out = json.dumps(response or {}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, fmt, *args):  # noqa: N802
            pass  # suppress default logging

    httpd = HTTPServer((host, port), Handler)
    print(f"[xueban mcp] HTTP debug server on http://{host}:{port}", file=sys.stderr)
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(stdio: bool = True, host: str = "127.0.0.1", port: int = 3002) -> None:
    if stdio:
        asyncio.run(run_stdio())
    else:
        asyncio.run(run_http(host=host, port=port))

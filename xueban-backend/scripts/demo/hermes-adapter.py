"""Hermes ACP Adapter — Hackathon Demo (Story 2-12).

Bridges Hermes CLI to Xueban's ACP WebSocket server.  When Hermes is not
installed (or --mock is passed), a built-in mock emits realistic streaming
output so the Demo can run without any external dependency.

Usage:
    python hermes-adapter.py [OPTIONS]

Options:
    --url URL               ACP server URL (default: ws://localhost:8765/acp)
    --api-key KEY           API key  (default: sf_demo_key)
    --workspace WORKSPACE   Workspace ID (default: 论文实验室)
    --agent-id ID           Override agent_id in manifest
    --display-name NAME     Override display_name shown in UI
    --mock                  Force mock mode even if hermes CLI is found
    --max-retries N         Max reconnect attempts (default: 3)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import time
from typing import Any, Dict, Optional

try:
    import websockets  # type: ignore
    from websockets.exceptions import ConnectionClosed  # type: ignore
except ImportError:
    print("ERROR: 'websockets' package not installed.  Run: pip install websockets")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Hermes-Adapter] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest sent during capability handshake
# ---------------------------------------------------------------------------

HERMES_MANIFEST: Dict[str, Any] = {
    "agent_id": "hermes-v2-local",
    "display_name": "Hermes Agent（代码理解者）",
    "version": "2.0.0",
    "tools": [
        {"name": "code_search", "description": "在代码仓库中语义搜索"},
        {"name": "semantic_analysis", "description": "理解代码结构和逻辑"},
        {"name": "shell", "description": "执行 bash 命令"},
    ],
    "max_concurrency": 2,
    "streaming": True,
    "memory": {"type": "stateless", "scope": "session", "persistence": False},
    "protocols": ["acp-v1"],
}

# ---------------------------------------------------------------------------
# Mock output generator (no Hermes CLI needed)
# ---------------------------------------------------------------------------

_MOCK_LINES = [
    "Analyzing repo structure...\n",
    "Found: run_classifier.py --task_name MRPC\n",
    "Scanning BERT config files...\n",
    "Identified required args: data_dir, bert_model, output_dir\n",
    "Verifying MRPC dataset path...\n",
    "Required flags: --do_train --do_eval --num_train_epochs 3\n",
    "Estimated runtime: ~4 min on GPU, ~12 min on CPU\n",
    "完成，已将执行参数传递给复现执行者\n",
]


async def _mock_hermes(instruction: str, ws: Any, task_id: str) -> None:
    """Stream pre-canned analysis output simulating Hermes execution."""
    logger.info("MOCK: executing task '%s'", instruction[:60])
    for line in _MOCK_LINES:
        await asyncio.sleep(0.4)
        await ws.send(json.dumps({
            "type": "task_stream",
            "task_id": task_id,
            "chunk": f"Hermes > {line}",
        }))
    await ws.send(json.dumps({
        "type": "task_complete",
        "task_id": task_id,
        "result": {"status": "success", "source": "mock"},
    }))
    logger.info("MOCK: task_id=%s completed", task_id)


# ---------------------------------------------------------------------------
# Real Hermes CLI execution
# ---------------------------------------------------------------------------

async def _run_hermes_cli(instruction: str, ws: Any, task_id: str) -> None:
    """Invoke `hermes run` and stream each stdout line back as task_stream."""
    logger.info("CLI: hermes run %r", instruction)
    proc = await asyncio.create_subprocess_exec(
        "hermes", "run", instruction,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if proc.stdout is None:
        raise RuntimeError("subprocess stdout is None")

    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace")
        await ws.send(json.dumps({
            "type": "task_stream",
            "task_id": task_id,
            "chunk": f"Hermes > {line}",
        }))

    returncode = await proc.wait()
    status = "success" if returncode == 0 else "error"
    await ws.send(json.dumps({
        "type": "task_complete",
        "task_id": task_id,
        "result": {"status": status, "returncode": returncode},
    }))
    logger.info("CLI: task_id=%s returncode=%d", task_id, returncode)


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

async def _do_auth(ws: Any, api_key: str, workspace: str, agent_hint: str) -> bool:
    await ws.send(json.dumps({
        "type": "auth",
        "api_key": api_key,
        "workspace_id": workspace,
        "agent_hint": agent_hint,
    }))
    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    ack = json.loads(raw)
    if ack.get("type") == "auth_error":
        logger.error("Auth rejected: %s", ack.get("message"))
        return False
    if ack.get("type") == "auth_ack":
        logger.info("✅ 连接成功: workspace=%s session=%s",
                    ack.get("workspace"), ack.get("session_id"))
        return True
    logger.warning("Unexpected auth response type=%s", ack.get("type"))
    return False


async def _do_handshake(ws: Any, manifest: Dict[str, Any]) -> None:
    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
    msg = json.loads(raw)
    if msg.get("type") != "capability_request":
        logger.warning("Expected capability_request, got type=%s", msg.get("type"))
        return
    await ws.send(json.dumps({"type": "capability_response", "manifest": manifest}))
    ack_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    ack = json.loads(ack_raw)
    logger.info("✅ 能力声明完成: agent_id=%s status=%s",
                ack.get("agent_id"), ack.get("status"))


async def _session_loop(ws: Any, use_mock: bool) -> None:
    """Main bidirectional loop: heartbeats in, tasks dispatched, streams out."""
    active: Dict[str, asyncio.Task] = {}

    async def _handle_task(msg: Dict[str, Any]) -> None:
        task_id = str(msg.get("task_id", ""))
        instruction = str(msg.get("instruction", ""))
        logger.info("TASK received: task_id=%s instruction='%s'", task_id, instruction[:60])
        if use_mock:
            coro = _mock_hermes(instruction, ws, task_id)
        else:
            coro = _run_hermes_cli(instruction, ws, task_id)
        t = asyncio.create_task(coro)
        active[task_id] = t
        t.add_done_callback(lambda _: active.pop(task_id, None))

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "task":
                await _handle_task(msg)

            elif msg_type == "task_cancel":
                task_id = str(msg.get("task_id", ""))
                t = active.pop(task_id, None)
                if t:
                    t.cancel()
                    logger.info("Cancelled task_id=%s", task_id)

            elif msg_type == "heartbeat":
                await ws.send(json.dumps({
                    "type": "heartbeat_ack",
                    "active_tasks": len(active),
                }))

            elif msg_type == "disconnect":
                logger.info("Server requested disconnect: %s", msg.get("reason"))
                break

            else:
                logger.debug("Unhandled message type=%s", msg_type)

    finally:
        for t in active.values():
            t.cancel()
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)


# ---------------------------------------------------------------------------
# Main with reconnect
# ---------------------------------------------------------------------------

async def run(
    url: str,
    api_key: str,
    workspace: str,
    manifest: Dict[str, Any],
    use_mock: bool,
    max_retries: int,
) -> None:
    attempt = 0
    backoff = 2.0
    agent_hint = manifest.get("agent_id", "hermes")

    while attempt <= max_retries:
        attempt += 1
        logger.info("Connecting to %s (attempt %d/%d)", url, attempt, max_retries + 1)
        try:
            async with websockets.connect(url, open_timeout=10) as ws:
                if not await _do_auth(ws, api_key, workspace, agent_hint):
                    logger.error("Authentication failed — check api_key and workspace.")
                    return
                await _do_handshake(ws, manifest)
                attempt = 0  # reset on successful connection
                backoff = 2.0
                await _session_loop(ws, use_mock)

        except ConnectionClosed as exc:
            logger.warning("Connection closed: %s", exc)
        except OSError as exc:
            logger.warning("Connection error: %s", exc)
        except asyncio.TimeoutError:
            logger.warning("Timeout during connect/handshake")

        if attempt <= max_retries:
            logger.info("Reconnecting in %.0fs…", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        else:
            logger.error("Max retries (%d) reached — exiting.", max_retries)
            raise RuntimeError(f"Max retries ({max_retries}) reached")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _build_manifest(args: argparse.Namespace) -> Dict[str, Any]:
    m = dict(HERMES_MANIFEST)
    if args.agent_id:
        m["agent_id"] = args.agent_id
    if args.display_name:
        m["display_name"] = args.display_name
    return m


def _detect_mock(args: argparse.Namespace) -> bool:
    if args.mock:
        return True
    if shutil.which("hermes") is None:
        logger.warning("hermes CLI not found — running in MOCK mode (demo output only)")
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Hermes → Xueban ACP Adapter")
    parser.add_argument("--url", default="ws://localhost:8765/acp")
    parser.add_argument("--api-key", default="sf_demo_key")
    parser.add_argument("--workspace", default="论文实验室")
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--mock", action="store_true",
                        help="Force mock mode (no real hermes CLI needed)")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    manifest = _build_manifest(args)
    use_mock = _detect_mock(args)

    if use_mock:
        logger.info("MOCK mode: streaming pre-canned Hermes output for demo")
    else:
        logger.info("REAL mode: piping tasks to hermes CLI")

    try:
        asyncio.run(run(
            url=args.url,
            api_key=args.api_key,
            workspace=args.workspace,
            manifest=manifest,
            use_mock=use_mock,
            max_retries=args.max_retries,
        ))
    except KeyboardInterrupt:
        logger.info("Adapter stopped by user.")
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

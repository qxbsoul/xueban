/**
 * skill-runners/mcp.ts — MCP (Model Context Protocol) runner (Story 15.23)
 *
 * Spec format: `mcp:<server>/<tool>` — e.g. `mcp:fs/edit_file`.
 *
 * Flow:
 *   1. resolve `<server>` to a spawn command via `.xueban/mcp.json`
 *   2. spawn stdio JSON-RPC, send `initialize` (5s timeout)
 *   3. send `tools/list`, find `<tool>` (else MCP_TOOL_NOT_FOUND)
 *   4. send `tools/call` with `{ goal, system }` arguments
 *   5. flatten `result.content[]` text items into a single delta + emit end
 *
 * Non-streaming: MCP `tools/call` is request/response (no notifications).
 * The runner therefore yields a single text delta then end.
 */

import { resolveMcpServer } from '../../acp-detector';
import { JsonRpcStdioTransport } from '../../protocols/jsonrpc-stdio';
import { parseAndExtract, type SseEvent } from '../../parser';
import type { RunnerInput } from './types';
import fs from 'node:fs';
import path from 'node:path';

const HANDSHAKE_TIMEOUT_MS = 5000;
const TOOL_CALL_TIMEOUT_MS = 600_000;

export async function* runMcpExecutor(
  input: RunnerInput,
  spec: string,
): AsyncGenerator<SseEvent> {
  const slash = spec.indexOf('/');
  if (slash <= 0 || slash === spec.length - 1) {
    yield {
      event: 'error',
      data: {
        code: 'MCP_INVALID_SPEC',
        spec,
        message: `expected "<server>/<tool>", got "${spec}"`,
      },
    };
    return;
  }
  const serverName = spec.slice(0, slash);
  const toolName = spec.slice(slash + 1);

  let cmd: { command: string; args: string[] };
  try {
    cmd = await resolveMcpServer(serverName);
  } catch (err) {
    const e = err as Error & { code?: string };
    yield {
      event: 'error',
      data: {
        code: e.code === 'MCP_SERVER_NOT_FOUND' ? 'MCP_SERVER_NOT_FOUND' : 'MCP_UNREACHABLE',
        server: serverName,
        message: e.message,
      },
    };
    return;
  }

  let transport: JsonRpcStdioTransport;
  try {
    transport = JsonRpcStdioTransport.fromCommand(cmd.command, cmd.args, process.env);
  } catch (err) {
    yield {
      event: 'error',
      data: { code: 'MCP_UNREACHABLE', server: serverName, message: (err as Error).message },
    };
    return;
  }

  // Per-session artifact dir.
  try { fs.mkdirSync(input.cwd, { recursive: true }); } catch { /* ignore */ }
  const artifactCallback = (filename: string, content: string, _type: string) => {
    const safeName = path.basename(filename);
    const filePath = path.join(input.cwd, safeName);
    try { fs.writeFileSync(filePath, content, 'utf-8'); } catch { /* ignore */ }
  };

  const onAbort = () => { void transport.close(false); };
  if (input.signal) {
    if (input.signal.aborted) onAbort();
    else input.signal.addEventListener('abort', onAbort, { once: true });
  }

  try {
    // 1. initialize
    await transport.request(
      'initialize',
      {
        protocolVersion: '2024-11-05',
        capabilities: {},
        clientInfo: { name: 'xueban-skill-studio', version: '0.1.0' },
      },
      HANDSHAKE_TIMEOUT_MS,
    );

    // 2. tools/list — verify tool exists.
    const tools = await transport.request<{ tools: Array<{ name: string; inputSchema?: unknown }> }>(
      'tools/list',
      {},
      HANDSHAKE_TIMEOUT_MS,
    );
    const found = (tools.tools ?? []).find((t) => t.name === toolName);
    if (!found) {
      yield {
        event: 'error',
        data: {
          code: 'MCP_TOOL_NOT_FOUND',
          server: serverName,
          tool: toolName,
          message: `tool "${toolName}" not in server "${serverName}". Available: ${(tools.tools ?? []).map((t) => t.name).join(', ') || '(none)'}`,
        },
      };
      return;
    }

    // 3. tools/call
    const result = await transport.request<{ content?: Array<{ type: string; text?: string }>; isError?: boolean }>(
      'tools/call',
      { name: toolName, arguments: { goal: input.prompt, system: input.system_prompt } },
      TOOL_CALL_TIMEOUT_MS,
    );

    const text = (result.content ?? [])
      .filter((c) => c.type === 'text' && typeof c.text === 'string')
      .map((c) => c.text!)
      .join('\n');

    if (text) {
      // Run through parser for <sf:*> tag extraction symmetry.
      const { events } = parseAndExtract(text, input.session_id, artifactCallback);
      for (const e of events) yield e;
      // Always also yield raw delta so plain text passes through.
      yield { event: 'delta', data: { text } };
    }

    yield { event: 'end', data: result.isError ? { tool_error: true } : {} };
  } catch (err) {
    const msg = (err as Error).message ?? String(err);
    const code = /timed out/i.test(msg)
      ? 'MCP_TIMEOUT'
      : /method not found|tools\/call|^-\d+:/i.test(msg)
        ? 'MCP_TOOL_ERROR'
        : 'MCP_ERROR';
    yield { event: 'error', data: { code, message: msg, server: serverName, tool: toolName } };
  } finally {
    if (input.signal) input.signal.removeEventListener('abort', onAbort);
    await transport.close(true);
  }
}

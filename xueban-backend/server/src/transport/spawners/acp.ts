/**
 * skill-runners/acp.ts — ACP (Agent Client Protocol) runner (Story 15.23)
 *
 * Spawns an ACP server as a stdio child process, runs the standard handshake
 * (`initialize` → `session/new` → `session/prompt`), and translates
 * `session/update` notifications into `RunnerChunk`s the rest of the pipeline
 * already understands.
 *
 * Notifications honored (others are ignored — out of scope for v1):
 *   - `agent_message_chunk`  → message text delta
 *   - `agent_thought_chunk`  → thinking text delta (treated as same flow)
 *
 * AbortController contract:
 *   - Send `session/cancel` notification (protocol-level cancel)
 *   - Wait 200ms grace
 *   - Then `child.kill('SIGTERM')` via transport.close()
 *
 * Errors are emitted as `{ event: 'error', data: { code, message, ... } }`
 * SSE events — never thrown out of the generator.
 */

import { parseAndExtract, type SseEvent } from '../../parser';
import { resolveAcpCommand } from '../../acp-detector';
import { JsonRpcStdioTransport, type JsonRpcNotification } from '../../protocols/jsonrpc-stdio';
import type { RunnerInput } from './types';
import fs from 'node:fs';
import path from 'node:path';

const ACP_PROTOCOL_VERSION = 1;
const HANDSHAKE_TIMEOUT_MS = 5000;
const PROMPT_TIMEOUT_MS = 600_000; // 10 min hard cap on a single prompt

/**
 * Build an ACP runner factory bound to a specific `target` (e.g. 'hermes' or
 * 'custom?cmd=node&arg=server.js'). The returned async-generator follows the
 * shared `RunnerChunk = SseEvent` shape so the dispatcher can splice it in
 * place of `runAnthropicDirect`.
 */
export async function* runAcpExecutor(
  input: RunnerInput,
  target: string,
): AsyncGenerator<SseEvent> {
  const { prompt: goal, system_prompt, session_id, signal } = input;

  // 1. resolve spawn command — emit ACP_UNREACHABLE on lookup failure.
  let cmd: { command: string; args: string[]; id: string };
  try {
    cmd = await resolveAcpCommand(target);
  } catch (err) {
    const e = err as Error & { code?: string; install_cmd?: string };
    yield {
      event: 'error',
      data: {
        code: e.code ?? 'ACP_UNREACHABLE',
        target,
        message: e.message,
        ...(e.install_cmd ? { install_cmd: e.install_cmd } : {}),
      },
    };
    return;
  }

  // 2. spawn transport — also ACP_UNREACHABLE on spawn failure.
  let transport: JsonRpcStdioTransport;
  try {
    transport = JsonRpcStdioTransport.fromCommand(cmd.command, cmd.args, process.env);
  } catch (err) {
    yield {
      event: 'error',
      data: {
        code: 'ACP_UNREACHABLE',
        target,
        message: `spawn failed: ${(err as Error).message}`,
      },
    };
    return;
  }

  // Per-session artifact dir (shared with anthropic.ts contract).
  try { fs.mkdirSync(input.cwd, { recursive: true }); } catch { /* ignore */ }
  const artifactCallback = (filename: string, content: string, _type: string) => {
    const safeName = path.basename(filename);
    const filePath = path.join(input.cwd, safeName);
    try {
      fs.writeFileSync(filePath, content, 'utf-8');
      console.log(`[acp] artifact written: ${filePath} (${content.length} bytes)`);
    } catch (err) {
      console.warn(`[acp] artifact write failed: ${(err as Error).message}`);
    }
  };

  // 3. queue notifications until we yield them out.
  const queue: SseEvent[] = [];
  let resolveNext: (() => void) | null = null;
  const wakeup = () => { const r = resolveNext; resolveNext = null; r?.(); };
  let parseBuffer = '';
  let promptDone = false;
  let cancelRequested = false;
  let errorSeen = false;

  transport.onNotification((n: JsonRpcNotification) => {
    if (n.method !== 'session/update') return;
    const params = n.params as { update?: { sessionUpdate?: string; content?: { type?: string; text?: string } } } | undefined;
    const update = params?.update;
    if (!update) return;
    if (update.sessionUpdate === 'agent_message_chunk' || update.sessionUpdate === 'agent_thought_chunk') {
      const text = update.content?.text;
      if (typeof text === 'string' && text.length > 0) {
        // Push raw delta + run the streaming parser to surface <sf:*> tags
        // exactly like anthropic.ts — symmetry is important so the SSE shape
        // is identical regardless of executor.
        parseBuffer += text;
        const { buffer: remaining, events } = parseAndExtract(parseBuffer, session_id, artifactCallback);
        parseBuffer = remaining;
        for (const e of events) queue.push(e);
        // Always also surface the raw delta so plain text shows up.
        queue.push({ event: 'delta', data: { text } });
        wakeup();
      }
    }
  });

  // Abort handler — protocol-level cancel + transport teardown.
  const onAbort = () => {
    cancelRequested = true;
    try { transport.notify('session/cancel', {}); } catch { /* ignore */ }
    setTimeout(() => { void transport.close(true); }, 200);
    queue.push({ event: 'end', data: { reason: 'aborted' } });
    wakeup();
  };
  if (signal) {
    if (signal.aborted) onAbort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }

  try {
    // 4. initialize — 5s hard timeout per AC6.
    await transport.request(
      'initialize',
      {
        protocolVersion: ACP_PROTOCOL_VERSION,
        clientInfo: { name: 'xueban-skill-studio', version: '0.1.0' },
        clientCapabilities: { fs: { readTextFile: false, writeTextFile: false } },
      },
      HANDSHAKE_TIMEOUT_MS,
    );

    // 5. session/new
    const newSession = await transport.request<{ sessionId: string }>(
      'session/new',
      { cwd: input.cwd, mcpServers: [] },
      HANDSHAKE_TIMEOUT_MS,
    );

    if (cancelRequested) return;

    // 6. session/prompt — fire and consume notifications until response.
    const promptPromise = transport.request(
      'session/prompt',
      {
        sessionId: newSession.sessionId,
        prompt: [{ type: 'text', text: `${system_prompt}\n\n---\n\n${goal}` }],
      },
      PROMPT_TIMEOUT_MS,
    );
    promptPromise
      .catch((err) => {
        errorSeen = true;
        queue.push({
          event: 'error',
          data: { code: 'ACP_PROMPT_FAILED', message: (err as Error).message },
        });
      })
      .finally(() => {
        promptDone = true;
        queue.push({ event: 'end', data: {} });
        wakeup();
      });

    // 7. drain queue — interleave notifications with terminal events.
    while (true) {
      if (queue.length === 0) {
        if (promptDone) break;
        await new Promise<void>((r) => { resolveNext = r; });
      }
      while (queue.length > 0) {
        const ev = queue.shift()!;
        yield ev;
        if (ev.event === 'end' || ev.event === 'error') return;
      }
    }
  } catch (err) {
    if (!errorSeen) {
      const msg = (err as Error).message ?? String(err);
      const code = /timed out/i.test(msg) ? 'ACP_TIMEOUT' : 'ACP_ERROR';
      yield { event: 'error', data: { code, message: msg } };
    }
  } finally {
    if (signal) signal.removeEventListener('abort', onAbort);
    await transport.close(true);
  }
}

#!/usr/bin/env node
/**
 * __mock_acp_server.js — Minimal stdio JSON-RPC ACP server for tests.
 *
 * Spoken from real `node` so test harness can `spawn()` it. We intentionally
 * use plain JS (not TS) so no tsx/ts-node dependency in the child process.
 *
 * Behavior:
 *   - On `initialize` → reply with { protocolVersion: 1, agentCapabilities: {...} }
 *   - On `session/new` → reply with { sessionId: 'sess-mock-1' }
 *   - On `session/prompt` → emit 3 `session/update` notifications with
 *     `agent_message_chunk` content, then reply with { stopReason: 'end_turn' }
 *   - On `session/cancel` notification → emit a final `session/update` with
 *     stopReason and reply nothing (notification, no id)
 *   - Unknown methods → reply error -32601 method not found
 */

'use strict';

const stdin = process.stdin;
const stdout = process.stdout;

let buffer = Buffer.alloc(0);

function send(obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  const header = Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, 'ascii');
  stdout.write(Buffer.concat([header, body]));
}

function notify(method, params) {
  send({ jsonrpc: '2.0', method, params });
}

let cancelled = false;

async function handleRequest(req) {
  const { id, method, params } = req;
  if (method === 'initialize') {
    send({
      jsonrpc: '2.0',
      id,
      result: {
        protocolVersion: 1,
        agentCapabilities: { tools: true, prompts: true },
        serverInfo: { name: 'mock-acp', version: '0.0.1' },
      },
    });
    return;
  }
  if (method === 'session/new') {
    send({ jsonrpc: '2.0', id, result: { sessionId: 'sess-mock-1' } });
    return;
  }
  if (method === 'session/prompt') {
    // Emit 3 chunks
    for (let i = 1; i <= 3; i++) {
      if (cancelled) break;
      notify('session/update', {
        sessionId: 'sess-mock-1',
        update: {
          sessionUpdate: 'agent_message_chunk',
          content: { type: 'text', text: `chunk-${i} ` },
        },
      });
      await new Promise((r) => setTimeout(r, 10));
    }
    send({
      jsonrpc: '2.0',
      id,
      result: { stopReason: cancelled ? 'cancelled' : 'end_turn' },
    });
    return;
  }
  // Notifications path doesn't reach handleRequest, but defensive:
  send({ jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${method}` } });
}

function handleNotification(note) {
  if (note.method === 'session/cancel') {
    cancelled = true;
  }
}

stdin.on('data', (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  while (true) {
    const headerEnd = buffer.indexOf('\r\n\r\n');
    if (headerEnd < 0) return;
    const header = buffer.slice(0, headerEnd).toString('ascii');
    const m = /Content-Length:\s*(\d+)/i.exec(header);
    if (!m) {
      buffer = buffer.slice(headerEnd + 4);
      continue;
    }
    const len = parseInt(m[1], 10);
    const start = headerEnd + 4;
    if (buffer.length < start + len) return;
    const body = buffer.slice(start, start + len).toString('utf8');
    buffer = buffer.slice(start + len);
    let msg;
    try { msg = JSON.parse(body); } catch { continue; }
    if (msg && typeof msg === 'object' && 'id' in msg && msg.method) {
      handleRequest(msg).catch((e) => {
        // best-effort error reply
        try {
          send({ jsonrpc: '2.0', id: msg.id, error: { code: -32000, message: String(e && e.message || e) } });
        } catch {}
      });
    } else if (msg && msg.method) {
      handleNotification(msg);
    }
  }
});

stdin.on('end', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));

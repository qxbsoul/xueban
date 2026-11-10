#!/usr/bin/env node
/**
 * __mock_mcp_server.js — Minimal stdio JSON-RPC MCP server for tests.
 *
 * Behavior:
 *   - initialize → { protocolVersion, capabilities: { tools: {} }, serverInfo }
 *   - tools/list → { tools: [{ name: 'echo_goal', inputSchema: {...} }, ...] }
 *   - tools/call name=echo_goal args={goal,system} → { content: [{type:'text',
 *     text: '<system>\n---\n<goal>'}] }
 *   - tools/call name=fail_tool → JSON-RPC error
 *   - unknown tool → JSON-RPC error -32602
 */

'use strict';

let buffer = Buffer.alloc(0);

function send(obj) {
  const body = Buffer.from(JSON.stringify(obj), 'utf8');
  const header = Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, 'ascii');
  process.stdout.write(Buffer.concat([header, body]));
}

const TOOLS = [
  {
    name: 'echo_goal',
    description: 'Echo back goal+system for testing',
    inputSchema: {
      type: 'object',
      properties: {
        goal: { type: 'string' },
        system: { type: 'string' },
      },
      required: ['goal'],
    },
  },
  {
    name: 'fail_tool',
    description: 'Always errors',
    inputSchema: { type: 'object' },
  },
];

function handle(req) {
  const { id, method, params } = req;
  if (method === 'initialize') {
    send({
      jsonrpc: '2.0', id,
      result: {
        protocolVersion: '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'mock-mcp', version: '0.0.1' },
      },
    });
    return;
  }
  if (method === 'tools/list') {
    send({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    return;
  }
  if (method === 'tools/call') {
    const name = params && params.name;
    const args = (params && params.arguments) || {};
    if (name === 'echo_goal') {
      const text = `${args.system || ''}\n---\n${args.goal || ''}`;
      send({
        jsonrpc: '2.0', id,
        result: { content: [{ type: 'text', text }], isError: false },
      });
      return;
    }
    if (name === 'fail_tool') {
      send({
        jsonrpc: '2.0', id,
        error: { code: -32000, message: 'fail_tool always errors' },
      });
      return;
    }
    send({
      jsonrpc: '2.0', id,
      error: { code: -32602, message: `unknown tool: ${name}` },
    });
    return;
  }
  send({ jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${method}` } });
}

process.stdin.on('data', (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  while (true) {
    const headerEnd = buffer.indexOf('\r\n\r\n');
    if (headerEnd < 0) return;
    const header = buffer.slice(0, headerEnd).toString('ascii');
    const m = /Content-Length:\s*(\d+)/i.exec(header);
    if (!m) { buffer = buffer.slice(headerEnd + 4); continue; }
    const len = parseInt(m[1], 10);
    const start = headerEnd + 4;
    if (buffer.length < start + len) return;
    const body = buffer.slice(start, start + len).toString('utf8');
    buffer = buffer.slice(start + len);
    let msg;
    try { msg = JSON.parse(body); } catch { continue; }
    if (msg && msg.method && 'id' in msg) handle(msg);
  }
});

process.stdin.on('end', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));

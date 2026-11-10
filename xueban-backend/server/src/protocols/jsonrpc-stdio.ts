/**
 * jsonrpc-stdio.ts — Minimal LSP-style JSON-RPC 2.0 stdio transport (Story 15.23)
 *
 * Wire format (`Content-Length` framing — same as LSP / ACP / MCP stdio):
 *
 *   Content-Length: 123\r\n
 *   \r\n
 *   {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
 *
 * This module is intentionally framework-free: it exposes a single class
 * `JsonRpcStdioTransport` that can either wrap a spawned child process
 * (`fromCommand(cmd, args, env)`) OR a pair of arbitrary streams
 * (`fromStreams(input, output)`) — the latter is what enables in-process
 * round-trip tests with PassThrough pairs.
 *
 * Cross-language compatibility: Python ACP servers (Epic 2 `transport.py`)
 * and Node MCP servers both speak this exact frame format, so a single
 * transport implementation drives both.
 */

import { spawn, type ChildProcess } from 'node:child_process';
import { Readable, Writable } from 'node:stream';

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: string | number;
  method: string;
  params?: unknown;
}

export interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: string | number;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: unknown;
}

export type JsonRpcMessage = JsonRpcRequest | JsonRpcResponse | JsonRpcNotification;
export type NotificationHandler = (n: JsonRpcNotification) => void;

interface PendingCall {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: NodeJS.Timeout;
}

export interface TransportOptions {
  /** Per-request default timeout in ms. Each .request() can override. */
  defaultTimeoutMs?: number;
  /** Stderr handler — by default, lines are forwarded to console.warn. */
  onStderr?: (chunk: Buffer) => void;
}

/**
 * Encode a JSON-RPC message into a `Content-Length`-framed Buffer.
 * Exported for tests.
 */
export function encodeFrame(msg: unknown): Buffer {
  const body = Buffer.from(JSON.stringify(msg), 'utf8');
  const header = Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, 'ascii');
  return Buffer.concat([header, body]);
}

/**
 * Stateful frame decoder. Feed buffers via `push(chunk)`; call `drain()` to
 * extract whatever complete frames are now available. Bytes that span chunks
 * are buffered internally.
 */
// 2026-05-11 review F6/F7: LSP 标准模式 16MB 单 frame 上限。
// 防恶意/runaway server 发 `Content-Length: 99999999999\r\n\r\n` 累 buffer
// 直到 OOM；同时给 JSON.parse 自然有界（cap frame 后 JSON 必 < 16MB）。
const MAX_FRAME_BYTES = 16 * 1024 * 1024;
// 等待 frame 时 buffer 也有上限，防 server 发巨大 header 卡 indexOf。
const MAX_PENDING_BYTES = MAX_FRAME_BYTES + 64 * 1024;

export class FrameDecoder {
  private buffer = Buffer.alloc(0);

  push(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > MAX_PENDING_BYTES) {
      // F6: hard reset on runaway buffer (lose pending frame is acceptable —
      // peer broke contract). Caller's drain() will return [] next call.
      console.warn(
        `[jsonrpc-stdio] buffer ${this.buffer.length}B > ${MAX_PENDING_BYTES}B — reset`,
      );
      this.buffer = Buffer.alloc(0);
    }
  }

  /** Drain all currently-complete frames; returns parsed JSON objects. */
  drain(): unknown[] {
    const out: unknown[] = [];
    while (true) {
      const headerEnd = this.buffer.indexOf('\r\n\r\n');
      if (headerEnd < 0) return out;
      const headerStr = this.buffer.slice(0, headerEnd).toString('ascii');
      const m = /Content-Length:\s*(\d+)/i.exec(headerStr);
      if (!m) {
        // Malformed header — drop up through the separator and try to recover.
        this.buffer = this.buffer.slice(headerEnd + 4);
        continue;
      }
      const len = parseInt(m[1], 10);
      // F6: reject oversize frame instead of waiting indefinitely for body.
      if (!Number.isFinite(len) || len < 0 || len > MAX_FRAME_BYTES) {
        console.warn(
          `[jsonrpc-stdio] frame Content-Length ${len} > ${MAX_FRAME_BYTES} — skipping frame`,
        );
        // Skip past the header; we cannot trust where body ends, so drop all
        // pending bytes (peer must re-sync). Safer than reading garbage.
        this.buffer = Buffer.alloc(0);
        return out;
      }
      const start = headerEnd + 4;
      if (this.buffer.length < start + len) return out; // need more bytes
      const body = this.buffer.slice(start, start + len).toString('utf8');
      this.buffer = this.buffer.slice(start + len);
      try {
        out.push(JSON.parse(body));
      } catch {
        // ignore parse errors — caller logs and moves on
      }
    }
  }

  bufferedBytes(): number {
    return this.buffer.length;
  }
}

export class JsonRpcStdioTransport {
  private decoder = new FrameDecoder();
  private pending = new Map<string | number, PendingCall>();
  private notificationHandlers: NotificationHandler[] = [];
  private closed = false;
  private exitCode: number | null = null;
  private exitListeners: Array<(code: number | null) => void> = [];

  private constructor(
    private readonly input: Writable,
    private readonly output: Readable,
    private readonly child: ChildProcess | null,
    private readonly opts: TransportOptions = {},
  ) {
    this.output.on('data', (chunk: Buffer) => this.handleChunk(chunk));
    this.output.on('end', () => this.handleClose(null));
    this.output.on('error', (err) => this.handleClose(err));
    if (child) {
      child.on('exit', (code) => {
        this.exitCode = code ?? null;
        this.handleClose(null);
        for (const fn of this.exitListeners) fn(code ?? null);
      });
      child.on('error', (err) => this.handleClose(err));
      child.stderr?.on('data', (b: Buffer) => {
        if (opts.onStderr) opts.onStderr(b);
        else process.stderr.write(`[jsonrpc-stdio stderr] ${b.toString()}`);
      });
    }
  }

  /** Spawn a child process and wrap its stdio. */
  static fromCommand(
    command: string,
    args: string[] = [],
    env?: NodeJS.ProcessEnv,
    opts: TransportOptions = {},
  ): JsonRpcStdioTransport {
    const child = spawn(command, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: env ?? process.env,
      shell: false,
    });
    if (!child.stdin || !child.stdout) {
      throw new Error(`spawn ${command}: stdio pipes not available`);
    }
    return new JsonRpcStdioTransport(child.stdin, child.stdout, child, opts);
  }

  /** Wrap a pre-existing pair of streams (used by tests). */
  static fromStreams(
    input: Writable,
    output: Readable,
    opts: TransportOptions = {},
  ): JsonRpcStdioTransport {
    return new JsonRpcStdioTransport(input, output, null, opts);
  }

  private handleChunk(chunk: Buffer): void {
    this.decoder.push(chunk);
    for (const msg of this.decoder.drain()) {
      this.dispatch(msg as JsonRpcMessage);
    }
  }

  private dispatch(msg: JsonRpcMessage): void {
    if ('id' in msg && msg.id !== undefined && msg.id !== null && (('result' in msg) || ('error' in msg))) {
      const resp = msg as JsonRpcResponse;
      const pend = this.pending.get(resp.id);
      if (!pend) return; // unsolicited / late response
      this.pending.delete(resp.id);
      clearTimeout(pend.timer);
      if (resp.error) {
        pend.reject(new Error(`${resp.error.code}: ${resp.error.message}`));
      } else {
        pend.resolve(resp.result);
      }
      return;
    }
    if ('method' in msg) {
      // Either a request or a notification.  We don't currently serve incoming
      // requests (no handler set), so ignore those; treat method-only messages
      // (no id, or id but caller subscribed) as notifications.
      const note = msg as JsonRpcNotification;
      for (const h of this.notificationHandlers) {
        try { h(note); } catch (err) {
          console.warn('[jsonrpc-stdio] notification handler threw:', (err as Error).message);
        }
      }
    }
  }

  private handleClose(err: Error | null): void {
    if (this.closed) return;
    this.closed = true;
    const reason = err
      ? new Error(`transport closed: ${err.message}`)
      : new Error(`transport closed (exit code=${this.exitCode})`);
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(reason);
    }
    this.pending.clear();
  }

  /** Send a request and await response. Returns parsed result or throws. */
  request<T = unknown>(method: string, params?: unknown, timeoutMs?: number): Promise<T> {
    if (this.closed) return Promise.reject(new Error('transport already closed'));
    const id = `req-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
    const req: JsonRpcRequest = { jsonrpc: '2.0', id, method, ...(params !== undefined ? { params } : {}) };
    const effective = timeoutMs ?? this.opts.defaultTimeoutMs ?? 30_000;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`request "${method}" timed out after ${effective}ms`));
        }
      }, effective);
      // Allow node to exit even if a timer is pending (test ergonomics).
      if (typeof timer.unref === 'function') timer.unref();
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
      try {
        this.write(req);
      } catch (err) {
        this.pending.delete(id);
        clearTimeout(timer);
        reject(err as Error);
      }
    });
  }

  /** Fire-and-forget JSON-RPC notification (no response expected). */
  notify(method: string, params?: unknown): void {
    if (this.closed) return;
    const note: JsonRpcNotification = { jsonrpc: '2.0', method, ...(params !== undefined ? { params } : {}) };
    try { this.write(note); } catch { /* ignore */ }
  }

  onNotification(h: NotificationHandler): void {
    this.notificationHandlers.push(h);
  }

  onExit(fn: (code: number | null) => void): void {
    if (this.exitCode !== null) fn(this.exitCode);
    else this.exitListeners.push(fn);
  }

  isClosed(): boolean {
    return this.closed;
  }

  private write(msg: unknown): void {
    const frame = encodeFrame(msg);
    this.input.write(frame);
  }

  /** Gracefully close; if a child process is attached, SIGTERM after grace. */
  async close(graceful = true): Promise<void> {
    if (this.closed) {
      // still ensure the child is gone if it survived
      if (this.child && !this.child.killed) {
        try { this.child.kill('SIGTERM'); } catch { /* ignore */ }
      }
      return;
    }
    try {
      if (graceful && this.input.writable) {
        try { this.input.end(); } catch { /* ignore */ }
        await new Promise<void>((r) => setTimeout(r, 200));
      }
    } finally {
      if (this.child && !this.child.killed && this.exitCode === null) {
        try { this.child.kill('SIGTERM'); } catch { /* ignore */ }
      }
      this.closed = true;
      for (const [, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error('transport closed by caller'));
      }
      this.pending.clear();
    }
  }
}

/**
 * transport/dispatcher.test.ts — resolveCallable() factory contract
 *
 * Per Phase 2 decision A6 (O1 unified path) and the doc §"call-phase error
 * model" (CL3/E3), the dispatcher is the single entry point that maps an
 * executor string to a concrete LlmCallable. Failures here must throw a typed
 * LlmCallError BEFORE any turn-time work begins, so the assembler can surface
 * a structured `error` SSE chunk without poisoning the stream.
 *
 * This file covers:
 *   - happy path for all five scheme prefixes (anthropic-direct / cli /
 *     acp / mcp / byok) — each returns a callable with the expected `.id`
 *   - missing-id failures: `cli:`, `acp:`, `mcp:`, `byok:` (trailing empty)
 *     all raise LlmCallError('provider-error') at resolve time
 *   - unknown scheme `foo:bar` raises LlmCallError('provider-error')
 *   - empty / non-string executor raises LlmCallError('provider-error')
 *   - whitespace tolerance: leading/trailing space is trimmed
 */

import { describe, it, expect } from 'vitest';
import { resolveCallable } from './dispatcher';
import { LlmCallError } from '../workflow/types';

describe('resolveCallable — happy path (5 schemes)', () => {
  it('anthropic-direct → callable with id "anthropic-direct"', () => {
    const c = resolveCallable('anthropic-direct', { apiKey: 'sk-test' });
    expect(c).toBeDefined();
    expect((c as { id?: string }).id).toBe('anthropic-direct');
    expect(typeof c.turn).toBe('function');
  });

  it('cli:claude → callable with id "cli:claude"', () => {
    const c = resolveCallable('cli:claude', { workspace: '/tmp/x' });
    expect((c as { id?: string }).id).toBe('cli:claude');
  });

  it('acp:bmad → callable with id starting "acp:"', () => {
    const c = resolveCallable('acp:bmad', { workspace: '/tmp/x' });
    const id = (c as { id?: string }).id ?? '';
    expect(id.startsWith('acp:')).toBe(true);
  });

  it('mcp:server/tool → callable with id starting "mcp:"', () => {
    const c = resolveCallable('mcp:server/tool', { workspace: '/tmp/x' });
    const id = (c as { id?: string }).id ?? '';
    expect(id.startsWith('mcp:')).toBe(true);
  });

  it('byok:anthropic → ApiClientCallable("anthropic") under the hood', () => {
    const c = resolveCallable('byok:anthropic', { apiKey: 'sk-test' });
    // ApiClientCallable sets id = "anthropic-direct" for the anthropic
    // provider (provider-name canonicalisation lives there, not here).
    const id = (c as { id?: string }).id ?? '';
    expect(['anthropic-direct', 'byok:anthropic']).toContain(id);
  });
});

// 2026-11-07 Day1 打通修复 — BYOK 自定义 API 地址透传回归。
// 背景：此前 resolveCallable('byok:<provider>') 只传 key 不传 baseUrl，
// OpenAiCompatApiClient 回退到内置默认地址（如 https://api.openai.com/v1），
// 自定义网关/本地端点场景请求打偏后表现为 SDK "Request timed out."。
describe('resolveCallable — BYOK baseUrl passthrough (2026-11-07 fix)', () => {
  const asApiClient = (c: unknown) =>
    (
      c as {
        getApiClient: () => {
          opts: { baseURL?: string; providerId?: string };
        };
      }
    ).getApiClient();

  it('byok:openai forwards opts.baseUrl into the OpenAI-compat client', () => {
    const c = resolveCallable('byok:openai', {
      apiKey: 'sk-test',
      baseUrl: 'http://127.0.0.1:9999/v1',
    });
    const client = asApiClient(c);
    expect(client.opts.providerId).toBe('openai');
    expect(client.opts.baseURL).toBe('http://127.0.0.1:9999/v1');
  });

  it('byok:deepseek without baseUrl keeps the built-in default (no override)', () => {
    const c = resolveCallable('byok:deepseek', { apiKey: 'sk-test' });
    const client = asApiClient(c);
    expect(client.opts.baseURL).toBeUndefined();
  });
});

describe('resolveCallable — call-phase errors (CL3/E3)', () => {
  it('empty string → LlmCallError(provider-error)', () => {
    expect(() => resolveCallable('')).toThrow(LlmCallError);
    try {
      resolveCallable('');
    } catch (e) {
      expect((e as LlmCallError).kind).toBe('provider-error');
    }
  });

  it('non-string → LlmCallError(provider-error)', () => {
    // @ts-expect-error — runtime guard
    expect(() => resolveCallable(null)).toThrow(LlmCallError);
    // @ts-expect-error — runtime guard
    expect(() => resolveCallable(undefined)).toThrow(LlmCallError);
    // @ts-expect-error — runtime guard
    expect(() => resolveCallable(123)).toThrow(LlmCallError);
  });

  it('cli: prefix with empty id → LlmCallError', () => {
    expect(() => resolveCallable('cli:')).toThrowError(/cli: prefix requires/i);
  });

  it('acp: prefix with empty id → LlmCallError', () => {
    expect(() => resolveCallable('acp:')).toThrowError(/acp: prefix requires/i);
  });

  it('mcp: prefix with empty id → LlmCallError', () => {
    expect(() => resolveCallable('mcp:')).toThrowError(/mcp: prefix requires/i);
  });

  it('byok: prefix with empty id → LlmCallError', () => {
    expect(() => resolveCallable('byok:')).toThrowError(/byok: prefix requires/i);
  });

  it('unknown scheme "foo:bar" → LlmCallError(provider-error)', () => {
    expect(() => resolveCallable('foo:bar')).toThrowError(/unknown executor/i);
  });

  it('byok:<unknown-provider> → LlmCallError surfaced from ApiClientCallable ctor', () => {
    // ApiClientCallable construction validates the provider id and throws on
    // unknown. The dispatcher does not swallow — the typed error propagates
    // out of resolveCallable, which is exactly the CL3/E3 contract.
    expect(() => resolveCallable('byok:nonexistent-provider-zzz')).toThrow(LlmCallError);
  });
});

describe('resolveCallable — whitespace tolerance', () => {
  it('trims leading/trailing whitespace', () => {
    const c = resolveCallable('  cli:claude  ', { workspace: '/tmp/x' });
    expect((c as { id?: string }).id).toBe('cli:claude');
  });
});

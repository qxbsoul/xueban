/**
 * provider-http.ts — shared HTTP helpers for provider-backed builtin tools.
 *
 * The provider tools (send_email / im_read / im_send / github_* / *_generate)
 * all follow the web-search.ts contract:
 *   - provider selected via env vars at runtime, nothing bundled;
 *   - unconfigured → structured stub so the LLM knows no real call happened;
 *   - 30s timeout combined with the runtime's AbortSignal;
 *   - network errors surfaced as `isError: true`, never thrown at the loop.
 *
 * This module holds the two bits of shared plumbing: the combined abort
 * signal and a minimal JSON fetch that keeps every tool's input type free of
 * DOM lib dependencies (server tsconfig may not include RequestInit).
 */

import type { BuiltinToolContext } from './types';

export const DEFAULT_PROVIDER_TIMEOUT_MS = 30_000;

/** Combine the runtime signal with a hard timeout. */
export function providerSignal(
  ctx: BuiltinToolContext,
  timeoutMs: number = DEFAULT_PROVIDER_TIMEOUT_MS,
): AbortSignal {
  return AbortSignal.any([ctx.signal, AbortSignal.timeout(timeoutMs)]);
}

export interface ProviderRequestInit {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
}

export interface ProviderJsonResponse {
  status: number;
  /** Parsed JSON body, or the raw text when the response is not JSON. */
  data: unknown;
  /** True when the response body was not valid JSON. */
  nonJson: boolean;
}

/**
 * Fetch `url` and parse the response. Does NOT throw on HTTP error status —
 * the caller decides how to frame non-2xx. Throws only on network failure /
 * abort (callers wrap in try/catch like web-search.ts does).
 */
export async function providerFetchJson(
  url: string,
  init: ProviderRequestInit,
  ctx: BuiltinToolContext,
  timeoutMs?: number,
): Promise<ProviderJsonResponse> {
  const signal = providerSignal(ctx, timeoutMs);
  const res = await fetch(url, { ...init, signal });
  const text = await res.text();
  try {
    return { status: res.status, data: JSON.parse(text), nonJson: false };
  } catch {
    return { status: res.status, data: text, nonJson: true };
  }
}

/** Read the env override from ctx when present (tests), else process.env. */
export function providerEnv(
  ctx: BuiltinToolContext,
): Readonly<Record<string, string>> {
  return ctx.env ?? (process.env as Record<string, string>);
}

/** Standard "no provider configured" stub shared by every provider tool. */
export function providerStub(tool: string, note: string, extra?: Record<string, unknown>) {
  return {
    output: {
      provider: 'stub',
      tool,
      ok: false,
      note,
      ...extra,
    },
  };
}

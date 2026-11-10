/**
 * fetch-url.ts — builtin `fetch_url` tool.
 *
 * Fetch a public URL over HTTPS. Default mode ALLOW.
 *
 * Constraints:
 *   - HTTPS only — `http://` is rejected up front (no plaintext leaks).
 *   - Block private / loopback addresses by host inspection BEFORE the fetch.
 *     We can't perfectly SSRF-proof without a DNS resolve hook, but blocking
 *     `localhost`, `127.0.0.0/8`, `10.*`, `192.168.*`, `172.16-31.*`,
 *     `169.254.*` and `::1` covers the obvious cases.
 *   - Response size cap MAX_FETCH_BYTES (5 MiB) enforced via streaming with
 *     `for await chunks`; bytes beyond the cap are dropped and `truncated: true`
 *     is flagged. We do NOT throw — partial content is more useful than no
 *     content.
 *   - 30s timeout via AbortSignal.timeout combined with ctx.signal.
 *   - Content-type sniffed; binary payloads are returned base64-encoded
 *     with a `binary: true` flag (LLMs handle this gracefully).
 */

import type { ToolSpec } from '../../tool-spec';
import { MAX_FETCH_BYTES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const fetchUrlTool: ToolSpec = {
  name: 'fetch_url',
  description:
    'Fetch a public HTTPS URL. Response is capped at 5 MiB. Private/loopback addresses are blocked. ' +
    'Returns content as text when possible, base64 otherwise.',
  input_schema: {
    type: 'object',
    properties: {
      url: { type: 'string', description: 'HTTPS URL to fetch.' },
      method: { type: 'string', enum: ['GET', 'HEAD'], description: 'HTTP method (default GET).' },
    },
    required: ['url'],
    additionalProperties: false,
  },
  source: 'base',
};

interface FetchUrlInput {
  url: string;
  method?: 'GET' | 'HEAD';
}

function isFetchUrlInput(x: unknown): x is FetchUrlInput {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as { url: unknown }).url === 'string'
  );
}

const PRIVATE_HOST_RE = [
  /^localhost$/i,
  /^127\./,
  /^10\./,
  /^192\.168\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
  /^169\.254\./,
  /^0\./,
  /^::1$/,
  /^fe80:/i,
  /^fc00:/i,
  /^fd00:/i,
];

function isBlockedHost(host: string): boolean {
  const bare = host.replace(/^\[|\]$/g, '').toLowerCase();
  return PRIVATE_HOST_RE.some((re) => re.test(bare));
}

export const fetchUrlExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isFetchUrlInput(input)) {
    return { output: { error: 'fetch_url: input must be { url: string, method? }' }, isError: true };
  }

  let url: URL;
  try {
    url = new URL(input.url);
  } catch {
    return { output: { error: `fetch_url: invalid URL: ${input.url}` }, isError: true };
  }

  if (url.protocol !== 'https:') {
    return { output: { error: `fetch_url: only https:// is allowed (got ${url.protocol})` }, isError: true };
  }
  if (isBlockedHost(url.hostname)) {
    return { output: { error: `fetch_url: host ${url.hostname} is private/loopback and blocked` }, isError: true };
  }

  const timeout = AbortSignal.timeout(30_000);
  const combined = AbortSignal.any([ctx.signal, timeout]);

  try {
    const res = await fetch(url, { method: input.method ?? 'GET', signal: combined });
    const contentType = res.headers.get('content-type') ?? '';
    const isText = /^(text\/|application\/(json|xml|.*\+json|.*\+xml|javascript|x-www-form-urlencoded))/i.test(contentType)
      || contentType.includes('charset');

    if (input.method === 'HEAD' || !res.body) {
      return {
        output: {
          url: input.url,
          status: res.status,
          contentType,
          headers: Object.fromEntries(res.headers.entries()),
          body: null,
        },
      };
    }

    // Streaming read with byte cap.
    const reader = res.body.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    let truncated = false;
    while (true) {
      if (ctx.signal.aborted) {
        await reader.cancel().catch(() => undefined);
        return { output: { error: 'fetch_url aborted' }, isError: true };
      }
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      const remaining = MAX_FETCH_BYTES - total;
      if (value.length > remaining) {
        chunks.push(value.subarray(0, remaining));
        total += remaining;
        truncated = true;
        await reader.cancel().catch(() => undefined);
        break;
      }
      chunks.push(value);
      total += value.length;
    }

    const buf = Buffer.concat(chunks.map((c) => Buffer.from(c.buffer, c.byteOffset, c.byteLength)));
    const body = isText ? buf.toString('utf8') : buf.toString('base64');

    return {
      output: {
        url: input.url,
        status: res.status,
        contentType,
        bytes: total,
        truncated,
        binary: !isText,
        body,
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `fetch_url failed: ${msg}` }, isError: true };
  }
};

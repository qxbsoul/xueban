/**
 * classify-error.ts — Map raw provider / runner errors to a 6-bucket enum
 * shared with the frontend.
 *
 * Buckets (mirrored in `src/core/hooks/useRunSession.ts` `ErrorCode`):
 *   'auth' | 'rate_limit' | 'context_too_long' | 'network' | 'server' | 'unknown'
 *
 * Inference inputs (any may be undefined; classifier OR's the signals):
 *   - server-side `code` field (e.g. NO_API_KEY, CLI_EXIT_NONZERO)
 *   - human `message` field
 *   - `stderr_tail` from CLI runners
 *   - upstream HTTP `status` from provider responses
 *
 * Output is purely the bucket id; the caller decides what hint / CTA to
 * surface. Routing belongs in the UI, not the daemon.
 */
export type ClassifiedErrorCode =
  | 'auth'
  | 'rate_limit'
  | 'context_too_long'
  | 'network'
  | 'server'
  | 'unknown';

export interface ClassifyInput {
  /** Server-side existing code (NO_API_KEY, CLI_EXIT_NONZERO, ACP_TIMEOUT, …). */
  code?: string | null;
  /** Human-readable error message — may carry provider tail. */
  message?: string | null;
  /** CLI stderr tail (typically last 2KB). */
  stderr_tail?: string | null;
  /** Upstream HTTP status when known (provider proxy / fetch). */
  status?: number | null;
}

const AUTH_CODES = new Set([
  'NO_API_KEY',
  'INVALID_API_KEY',
  'UNAUTHORIZED',
  'AUTH_FAILED',
]);

const RATE_LIMIT_CODES = new Set([
  'RATE_LIMITED',
  'TOO_MANY_REQUESTS',
  'OVERLOADED',
]);

const CONTEXT_CODES = new Set([
  'CONTEXT_TOO_LONG',
  'CONTEXT_LENGTH_EXCEEDED',
  'PROMPT_TOO_LONG',
]);

const NETWORK_CODES = new Set([
  'NETWORK_ERROR',
  'CONNECTION_REFUSED',
  'ECONNREFUSED',
  'ETIMEDOUT',
  'ENOTFOUND',
  'ACP_UNREACHABLE',
  'ACP_TIMEOUT',
]);

const SERVER_CODES = new Set([
  'INTERNAL_ERROR',
  'PROVIDER_ERROR',
  'CLI_NO_STDOUT',
  'CLI_SPAWN_FAILED',
  'CLI_UNKNOWN',
  'CLI_EXIT_NONZERO',
  'PROJECT_DIR_FAILED',
  'ARTIFACT_WRITE_FAILED',
  'SKILL_NOT_CONFIGURED',
  'ACP_ERROR',
  'ACP_PROMPT_FAILED',
  'CRITIQUE_FAILED',
]);

const RX_AUTH = /\b(?:401|403)\b|Failed to authenticate|invalid[_\s-]?api[_\s-]?key|authentication|unauthori[sz]ed|no api key|missing api key/i;
const RX_RATE_LIMIT = /\b429\b|rate ?limit|too many requests|overloaded|temporarily unable to serve/i;
const RX_CONTEXT = /context[_ -]?length[_ -]?exceed|tokens? exceeds?|maximum context|prompt is too long|input length .* exceed/i;
const RX_NETWORK = /ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|getaddrinfo|ENOTFOUND|EHOSTUNREACH|ENETUNREACH|fetch failed|socket hang up|network ?(?:error|unreachable)/i;
const RX_SERVER_5XX = /\b5\d{2}\b|internal server error|bad gateway|service unavailable|gateway timeout/i;

/**
 * Returns the bucket id for a given error signal mix. Always falls back to
 * 'unknown' rather than throwing — caller can rely on a string.
 */
export function classifyErrorCode(input: ClassifyInput): ClassifiedErrorCode {
  const { code, message, stderr_tail, status } = input;

  // 1. Explicit server-side codes win — they're the most precise signal.
  if (code) {
    const c = code.toUpperCase();
    if (AUTH_CODES.has(c)) return 'auth';
    if (RATE_LIMIT_CODES.has(c)) return 'rate_limit';
    if (CONTEXT_CODES.has(c)) return 'context_too_long';
    if (NETWORK_CODES.has(c)) return 'network';
    // SERVER_CODES are kept for downstream classification only — we still
    // peek at the message below in case the runtime knows it's actually a
    // 401 / 429 / context overflow that surfaced as CLI_EXIT_NONZERO.
  }

  // 2. HTTP status code (provider proxy / fetch).
  if (typeof status === 'number') {
    if (status === 401 || status === 403) return 'auth';
    if (status === 429) return 'rate_limit';
    if (status >= 500 && status < 600) return 'server';
  }

  // 3. Message + stderr regex sweep. Order matters: auth before server,
  //    context before generic 5xx, rate_limit before server.
  const haystack = [message, stderr_tail].filter(Boolean).join('\n');
  if (haystack) {
    if (RX_AUTH.test(haystack)) return 'auth';
    if (RX_RATE_LIMIT.test(haystack)) return 'rate_limit';
    if (RX_CONTEXT.test(haystack)) return 'context_too_long';
    if (RX_NETWORK.test(haystack)) return 'network';
    if (RX_SERVER_5XX.test(haystack)) return 'server';
  }

  // 4. Explicit server-bucket codes when nothing more specific matched.
  if (code && SERVER_CODES.has(code.toUpperCase())) return 'server';

  return 'unknown';
}

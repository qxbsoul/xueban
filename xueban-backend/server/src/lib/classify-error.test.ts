/**
 * classify-error.test.ts — standalone smoke test for classifyErrorCode().
 *
 * Run with:  npx tsx src/lib/classify-error.test.ts   (from server/)
 *
 * Mirrors the assembler.test.ts no-framework pattern (vitest not installed
 * in the server package yet). Covers every bucket plus the regression cases
 * called out in the task spec:
 *   - "Failed to authenticate. API Error: 403" → auth
 *   - "429 Too Many Requests" → rate_limit
 *   - "context_length_exceeded" / "tokens exceeds" → context_too_long
 *   - ECONNREFUSED / ETIMEDOUT / getaddrinfo → network
 *   - 5xx → server
 */

import { classifyErrorCode } from './classify-error';

let pass = 0;
let fail = 0;

function check(label: string, expected: string, actual: string) {
  if (expected === actual) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}  expected=${expected} actual=${actual}`);
  }
}

// ── auth ────────────────────────────────────────────────────────────────────
check(
  'NO_API_KEY code → auth',
  'auth',
  classifyErrorCode({ code: 'NO_API_KEY', message: 'missing key' }),
);
check(
  'message "Failed to authenticate. API Error: 403" → auth',
  'auth',
  classifyErrorCode({ code: 'CLI_EXIT_NONZERO', message: 'Failed to authenticate. API Error: 403' }),
);
check(
  'HTTP 401 → auth',
  'auth',
  classifyErrorCode({ status: 401, message: 'whatever' }),
);
check(
  'HTTP 403 → auth',
  'auth',
  classifyErrorCode({ status: 403 }),
);

// ── rate_limit ──────────────────────────────────────────────────────────────
check(
  'message "429 Too Many Requests" → rate_limit',
  'rate_limit',
  classifyErrorCode({ message: '429 Too Many Requests' }),
);
check(
  'HTTP 429 → rate_limit',
  'rate_limit',
  classifyErrorCode({ status: 429 }),
);
check(
  'OVERLOADED code → rate_limit',
  'rate_limit',
  classifyErrorCode({ code: 'OVERLOADED', message: 'busy' }),
);

// ── context_too_long ────────────────────────────────────────────────────────
check(
  'message "context_length_exceeded" → context_too_long',
  'context_too_long',
  classifyErrorCode({ code: 'PROVIDER_ERROR', message: 'context_length_exceeded: prompt is too long' }),
);
check(
  'message "tokens exceeds" → context_too_long',
  'context_too_long',
  classifyErrorCode({ message: 'input tokens exceeds 200000' }),
);

// ── network ─────────────────────────────────────────────────────────────────
check(
  'stderr "ECONNREFUSED" → network',
  'network',
  classifyErrorCode({ code: 'CLI_EXIT_NONZERO', message: 'crashed', stderr_tail: 'connect ECONNREFUSED 127.0.0.1:8080' }),
);
check(
  'message "ETIMEDOUT" → network',
  'network',
  classifyErrorCode({ message: 'ETIMEDOUT' }),
);
check(
  'message "getaddrinfo ENOTFOUND" → network',
  'network',
  classifyErrorCode({ message: 'getaddrinfo ENOTFOUND api.anthropic.com' }),
);
check(
  'ACP_TIMEOUT code → network',
  'network',
  classifyErrorCode({ code: 'ACP_TIMEOUT', message: 'timed out' }),
);

// ── server ──────────────────────────────────────────────────────────────────
check(
  'HTTP 502 → server',
  'server',
  classifyErrorCode({ status: 502 }),
);
check(
  'HTTP 503 → server',
  'server',
  classifyErrorCode({ status: 503 }),
);
check(
  'message "Internal server error" → server',
  'server',
  classifyErrorCode({ message: 'Internal server error during assembly: boom' }),
);
check(
  'CLI_EXIT_NONZERO with generic message → server',
  'server',
  classifyErrorCode({ code: 'CLI_EXIT_NONZERO', message: 'claude 退出码 2' }),
);

// ── unknown fallback ────────────────────────────────────────────────────────
check(
  'empty input → unknown',
  'unknown',
  classifyErrorCode({}),
);
check(
  'unrecognized message → unknown',
  'unknown',
  classifyErrorCode({ message: 'something weird happened' }),
);

// ── precedence — code > status > message ────────────────────────────────────
check(
  'CONTEXT_TOO_LONG code wins over 500 status',
  'context_too_long',
  classifyErrorCode({ code: 'CONTEXT_TOO_LONG', status: 500, message: 'oops' }),
);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);

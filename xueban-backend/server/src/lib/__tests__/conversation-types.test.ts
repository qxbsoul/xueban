/**
 * conversation-types.test.ts — S1 (skill-team-conversion-design-v1.md §5)
 * smoke test for ContentBlock / ConversationMessage shape + the D8 v0 → v1
 * SessionRecord migration baked into createSessionStore({ migrate }).
 *
 * Run with:  npx tsx src/lib/__tests__/conversation-types.test.ts   (from server/)
 *
 * Standalone tsx pattern — no vitest in this package (mirrors
 * intent-router.test.ts / classify-error.test.ts convention).
 *
 * Coverage:
 *   1. ContentBlock discriminated union compiles + narrows on `kind`.
 *   2. ConversationMessage carries blocks + optional usage.
 *   3. Migrator injects `messages: []` and bumps `version` 0 → 1 for old records.
 *   4. Migrator preserves all other fields untouched.
 *   5. Migrator is a no-op (idempotent) for already-v1 records.
 *   6. loadAll() actually invokes the migrator on disk — write a stripped-down
 *      JSON file directly into .xueban/sessions/ and read it back through
 *      a fresh store, asserting the in-memory record has both fields.
 */

import { promises as fs } from 'fs';
import * as path from 'path';
import {
  SESSION_SCHEMA_VERSION,
  type ContentBlock,
  type ConversationMessage,
} from '../conversation-types';
import { createSessionStore } from '../session-store';

let pass = 0;
let fail = 0;

function check(label: string, expected: unknown, actual: unknown) {
  const eq = JSON.stringify(expected) === JSON.stringify(actual);
  if (eq) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(
      `  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`,
    );
  }
}

// ── 1. ContentBlock discriminated-union narrowing ────────────────────────────
{
  const text: ContentBlock = { kind: 'text', text: 'hello' };
  const toolUse: ContentBlock = {
    kind: 'tool_use',
    id: 'tu_1',
    name: 'read_file',
    input: { path: 'a.txt' },
  };
  const toolResult: ContentBlock = {
    kind: 'tool_result',
    tool_use_id: 'tu_1',
    tool_name: 'read_file',
    output: 'file contents',
    is_error: false,
  };

  // Narrowing check — TS infers `text.text`, `toolUse.id`, `toolResult.output`.
  const probe = (b: ContentBlock): string => {
    switch (b.kind) {
      case 'text':
        return `text:${b.text}`;
      case 'image':
      case 'video':
        return `${b.kind}:${b.data_url}`;
      case 'tool_use':
        return `use:${b.id}/${b.name}`;
      case 'tool_result':
        return `res:${b.tool_use_id}/${b.is_error}`;
    }
  };
  check('ContentBlock text narrowing', 'text:hello', probe(text));
  check('ContentBlock tool_use narrowing', 'use:tu_1/read_file', probe(toolUse));
  check('ContentBlock tool_result narrowing', 'res:tu_1/false', probe(toolResult));
}

// ── 2. ConversationMessage carries blocks + optional usage ───────────────────
{
  const msg: ConversationMessage = {
    role: 'assistant',
    blocks: [{ kind: 'text', text: 'ok' }],
    usage: { input_tokens: 42, output_tokens: 7 },
  };
  check('ConversationMessage role', 'assistant', msg.role);
  check('ConversationMessage block count', 1, msg.blocks.length);
  check('ConversationMessage usage.input_tokens', 42, msg.usage?.input_tokens);

  const noUsage: ConversationMessage = {
    role: 'user',
    blocks: [{ kind: 'text', text: 'hi' }],
  };
  check('ConversationMessage usage optional', undefined, noUsage.usage);
}

// ── 3-5. Migrator behavior (synthetic raw → SessionRecord shape) ─────────────
// We test the migrator function directly by constructing a store and reaching
// into its private behavior via a single round-trip through loadAll(). For
// fast unit coverage of the function in isolation we inline the same logic the
// route uses (kept in lockstep with run-sessions.ts).
type FakeSessionRecord = {
  goal: string;
  created_at: number;
  messages?: ConversationMessage[];
  version?: number;
  // extra opaque field the migrator must NOT touch
  api_key?: string;
};

function migrate(raw: unknown): FakeSessionRecord | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const rec = raw as Partial<FakeSessionRecord> & Record<string, unknown>;
  if (!Array.isArray(rec.messages)) rec.messages = [];
  if (typeof rec.version !== 'number' || rec.version < SESSION_SCHEMA_VERSION) {
    rec.version = SESSION_SCHEMA_VERSION;
  }
  return rec as FakeSessionRecord;
}

{
  // 3. Old record — no messages, no version → both injected.
  const oldRec = { goal: 'g1', created_at: 1, api_key: 'sk-x' };
  const m = migrate(oldRec)!;
  check('migrate v0: messages injected', [], m.messages);
  check('migrate v0: version bumped to 1', SESSION_SCHEMA_VERSION, m.version);
  check('migrate v0: api_key preserved', 'sk-x', m.api_key);
  check('migrate v0: goal preserved', 'g1', m.goal);
}

{
  // 4. Half-old record — has messages but no version (or vice versa).
  const halfRec: FakeSessionRecord = {
    goal: 'g2',
    created_at: 2,
    messages: [{ role: 'user', blocks: [{ kind: 'text', text: 'x' }] }],
  };
  const m = migrate(halfRec)!;
  check('migrate half: messages NOT clobbered', 1, m.messages!.length);
  check('migrate half: version still bumped', SESSION_SCHEMA_VERSION, m.version);
}

{
  // 5. Already-v1 record — idempotent (no spurious change).
  const v1Rec: FakeSessionRecord = {
    goal: 'g3',
    created_at: 3,
    messages: [],
    version: SESSION_SCHEMA_VERSION,
  };
  const m = migrate(v1Rec)!;
  check('migrate v1: idempotent version', SESSION_SCHEMA_VERSION, m.version);
  check('migrate v1: idempotent messages', [], m.messages);
}

{
  // Migrator drops non-object input.
  check('migrate null → undefined', undefined, migrate(null));
  check('migrate array → undefined', undefined, migrate([1, 2]));
  check('migrate string → undefined', undefined, migrate('oops'));
}

// ── 6. End-to-end: write dirty JSON, loadAll(), assert migration applied ─────
// Uses the real SESSIONS_DIR (cwd-relative) — same as production. We pick a
// uuid-shaped id with a `test_` prefix and remove the file on the way out.
async function endToEnd() {
  const sessionsDir = path.resolve(process.cwd(), '.xueban', 'sessions');
  await fs.mkdir(sessionsDir, { recursive: true });
  const testId = `test_s1_${Date.now()}`;
  const fp = path.join(sessionsDir, `${testId}.json`);

  // Pre-S1 shape: no `messages`, no `version`.
  await fs.writeFile(
    fp,
    JSON.stringify({ goal: 'legacy-goal', created_at: 42, api_key: 'sk-y' }, null, 2),
    'utf8',
  );

  try {
    const store = createSessionStore<FakeSessionRecord>({ migrate });
    await store.loadAll();
    const got = store.get(testId);
    check('e2e loadAll loaded the record', true, got !== undefined);
    check('e2e migrator injected empty messages', [], got?.messages);
    check('e2e migrator bumped version 0 → 1', SESSION_SCHEMA_VERSION, got?.version);
    check('e2e migrator preserved goal', 'legacy-goal', got?.goal);
    check('e2e migrator preserved api_key', 'sk-y', got?.api_key);
  } finally {
    try {
      await fs.unlink(fp);
    } catch {
      /* best-effort cleanup */
    }
  }
}

endToEnd().then(() => {
  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
});

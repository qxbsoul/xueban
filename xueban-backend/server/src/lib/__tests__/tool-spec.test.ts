/**
 * tool-spec.test.ts — S2 (skill-team-conversion-design-v1.md §5) smoke test
 * for ToolSpec + ToolRegistry.
 *
 * Run with:  npx tsx src/lib/__tests__/tool-spec.test.ts   (from server/)
 *
 * Standalone tsx pattern (no vitest in this package — mirrors
 * intent-router.test.ts convention).
 *
 * Coverage:
 *   1. constructor seeds initial specs in order
 *   2. register() adds new specs at the tail
 *   3. register() overwrites in place (same position, new content) for
 *      re-registered names
 *   4. get() returns the latest spec; missing name → undefined
 *   5. has() reflects presence
 *   6. list() returns insertion order, immutable to caller mutation
 *   7. toAnthropicTools() strips `source` and preserves order
 */

import { ToolRegistry, type ToolSpec } from '../tool-spec';

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

const bash: ToolSpec = {
  name: 'bash',
  description: 'Run a shell command.',
  input_schema: {
    type: 'object',
    properties: { command: { type: 'string' } },
    required: ['command'],
    additionalProperties: false,
  },
  source: 'base',
};

const readFile: ToolSpec = {
  name: 'read_file',
  description: 'Read a workspace file.',
  input_schema: {
    type: 'object',
    properties: { path: { type: 'string' } },
    required: ['path'],
    additionalProperties: false,
  },
  source: 'base',
};

const skillAnchor: ToolSpec = {
  name: 'skill_anchor',
  description: 'Activate a skill anchor.',
  input_schema: {
    type: 'object',
    properties: { id: { type: 'string' } },
    required: ['id'],
    additionalProperties: false,
  },
  source: 'conditional',
};

// ── 1. Constructor seeds initial specs in order ──────────────────────────────
{
  const r = new ToolRegistry([bash, readFile]);
  check('initial: list length', 2, r.list().length);
  check('initial: first name', 'bash', r.list()[0].name);
  check('initial: second name', 'read_file', r.list()[1].name);
}

// ── 2. register() appends new spec ───────────────────────────────────────────
{
  const r = new ToolRegistry([bash]);
  r.register(readFile);
  r.register(skillAnchor);
  check(
    'register: order preserved',
    ['bash', 'read_file', 'skill_anchor'],
    r.list().map((s) => s.name),
  );
  check('register: source on conditional', 'conditional', r.get('skill_anchor')?.source);
}

// ── 3. register() overwrites in place ────────────────────────────────────────
{
  const r = new ToolRegistry([bash, readFile, skillAnchor]);
  const updatedBash: ToolSpec = { ...bash, description: 'updated desc' };
  r.register(updatedBash);
  check('overwrite: list length unchanged', 3, r.list().length);
  check(
    'overwrite: position unchanged',
    ['bash', 'read_file', 'skill_anchor'],
    r.list().map((s) => s.name),
  );
  check('overwrite: description updated', 'updated desc', r.get('bash')?.description);
}

// ── 4. get() lookup ──────────────────────────────────────────────────────────
{
  const r = new ToolRegistry([bash]);
  check('get: hit', 'bash', r.get('bash')?.name);
  check('get: miss', undefined, r.get('nope'));
}

// ── 5. has() membership ──────────────────────────────────────────────────────
{
  const r = new ToolRegistry([bash]);
  check('has: hit', true, r.has('bash'));
  check('has: miss', false, r.has('nope'));
}

// ── 6. list() mutation isolation ─────────────────────────────────────────────
{
  const r = new ToolRegistry([bash, readFile]);
  const snapshot = r.list();
  snapshot.pop();
  // Caller mutation MUST NOT shrink the registry.
  check('list: external mutation does not affect registry', 2, r.list().length);
}

// ── 7. toAnthropicTools() shape + order ──────────────────────────────────────
{
  const r = new ToolRegistry([bash, readFile, skillAnchor]);
  const tools = r.toAnthropicTools();
  check('toAnthropicTools: length', 3, tools.length);
  check(
    'toAnthropicTools: order matches list()',
    ['bash', 'read_file', 'skill_anchor'],
    tools.map((t) => t.name),
  );
  // No `source` leak.
  check(
    'toAnthropicTools: source stripped',
    false,
    Object.prototype.hasOwnProperty.call(tools[0], 'source'),
  );
  check(
    'toAnthropicTools: input_schema preserved',
    bash.input_schema,
    tools[0].input_schema,
  );
  check(
    'toAnthropicTools: description preserved',
    'Read a workspace file.',
    tools[1].description,
  );
}

// ── 8. Last-write-wins includes the `source` field ───────────────────────────
// P1 review fix: JSDoc on register() now states that re-registering replaces
// every field including `source`. Lock that semantics down with a test so a
// future "merge instead of replace" refactor breaks loudly.
{
  const r = new ToolRegistry([bash]);  // bash starts as source: 'base'
  const shadowed: ToolSpec = { ...bash, source: 'conditional', description: 'per-skill override' };
  r.register(shadowed);
  check('last-write-wins: source overwritten to conditional', 'conditional', r.get('bash')?.source);
  check('last-write-wins: description overwritten', 'per-skill override', r.get('bash')?.description);
  check('last-write-wins: still single entry, position kept', ['bash'], r.list().map((s) => s.name));
}

console.log(`\n${pass} pass, ${fail} fail`);
if (fail > 0) process.exit(1);

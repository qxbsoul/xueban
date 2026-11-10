/**
 * workflow/condition.test.ts — expr-eval sandbox contract
 *
 * doc §7 Risk row "expr-eval sandbox 逃逸" was listed without a backing test.
 * This file proves the lock-down: assignment / fndef are off, no access to
 * `process` / `global` / `require`, malformed expressions return false rather
 * than throwing, and the documented variable surface (prev.<id>.{status,
 * artifacts, duration, error} + node.{id,team}) resolves correctly.
 */

import { describe, it, expect } from 'vitest';
import { evaluate, validateExpression } from './condition';
import type { NodeContext, RunResult, TeamDefV1 } from './types';

function mkTeam(): TeamDefV1 {
  return {
    name: 'test',
    mode: 'dag',
    policy: 'permissive',
    retry: 3,
    agents: [],
    edges: [],
    loaded_at: 0,
    source_dir: '/fake',
    team_id: 'test-team',
    version: 1,
    policy_obj: { retry: 3 },
    members_ids: [],
    edges_v1: [],
  };
}

function mkCtx(
  priorResults: Record<string, Partial<RunResult>> = {},
  node_id = 'current',
): NodeContext {
  const m = new Map<string, RunResult>();
  for (const [id, r] of Object.entries(priorResults)) {
    m.set(id, {
      node_id: id,
      status: 'done',
      artifacts: [],
      durationMs: 0,
      ...r,
    });
  }
  return { team: mkTeam(), node_id, workspace: '/tmp/ws', priorResults: m };
}

describe('evaluate — literals', () => {
  it('true → true', () => {
    expect(evaluate('true', mkCtx())).toBe(true);
  });
  it('false → false', () => {
    expect(evaluate('false', mkCtx())).toBe(false);
  });
  it('numeric truthy → true', () => {
    expect(evaluate('1', mkCtx())).toBe(true);
    expect(evaluate('0', mkCtx())).toBe(false);
  });
});

describe('evaluate — empty / malformed → false', () => {
  it('empty string → false', () => {
    expect(evaluate('', mkCtx())).toBe(false);
  });
  it('whitespace-only → false', () => {
    expect(evaluate('   \n\t', mkCtx())).toBe(false);
  });
  it('null-ish input → false', () => {
    // @ts-expect-error — runtime guard
    expect(evaluate(null, mkCtx())).toBe(false);
    // @ts-expect-error — runtime guard
    expect(evaluate(undefined, mkCtx())).toBe(false);
  });
  it('syntactically invalid → false (swallows parse error)', () => {
    expect(evaluate('(((', mkCtx())).toBe(false);
    expect(evaluate('1 + + +', mkCtx())).toBe(false);
  });
});

describe('evaluate — prev.<id> variable surface', () => {
  it('prev.<id>.status string compare works', () => {
    const ctx = mkCtx({ analyst: { status: 'done' } });
    expect(evaluate('prev.analyst.status == "done"', ctx)).toBe(true);
    expect(evaluate('prev.analyst.status == "failed"', ctx)).toBe(false);
  });

  it('prev.<id>.duration numeric compare works', () => {
    const ctx = mkCtx({ analyst: { durationMs: 1500 } });
    expect(evaluate('prev.analyst.duration > 1000', ctx)).toBe(true);
    expect(evaluate('prev.analyst.duration > 5000', ctx)).toBe(false);
  });

  it('artifacts string is non-empty when node produced files', () => {
    // expr-eval's `in` operator does NOT do JS-style substring search on
    // strings (despite an old comment in condition.ts suggesting otherwise).
    // The realistic gating pattern is to check `length()` on the joined
    // artifact string, or to compare `status == "done"`.
    const ctx = mkCtx({
      reviewer: { artifacts: ['/ws/approved.md', '/ws/notes.md'] },
    });
    expect(evaluate('length(prev.reviewer.artifacts) > 0', ctx)).toBe(true);
    expect(evaluate('prev.reviewer.status == "done"', ctx)).toBe(true);
  });

  it('access to non-existent prev.<id> → false (swallows runtime error)', () => {
    expect(evaluate('prev.ghost.status == "done"', mkCtx())).toBe(false);
  });

  it('node.id and node.team are exposed', () => {
    const ctx = mkCtx({}, 'current');
    expect(evaluate('node.id == "current"', ctx)).toBe(true);
    expect(evaluate('node.team == "test-team"', ctx)).toBe(true);
  });
});

describe('evaluate — sandbox hardening (doc §7 risk)', () => {
  it('assignment operator is disabled — `x = 1` does not mutate', () => {
    // expr-eval with `assignment: false` parses `x = 1` as a comparison or
    // throws — either way, evaluate() must NOT return true and MUST NOT
    // mutate ctx.priorResults.
    const ctx = mkCtx({ a: { status: 'done' } });
    const result = evaluate('a = 1', ctx);
    expect(result).toBe(false); // either parse-error swallowed or value falsy
    expect(ctx.priorResults.get('a')?.status).toBe('done'); // unchanged
  });

  it('inline function definitions are disabled — `f(x) = x + 1`', () => {
    expect(evaluate('f(x) = x + 1', mkCtx())).toBe(false);
  });

  it('cannot access JS globals via identifiers', () => {
    // `process` / `global` / `require` aren't in the variable surface →
    // expr-eval raises an undefined-symbol error → swallowed → false.
    expect(evaluate('process', mkCtx())).toBe(false);
    expect(evaluate('global', mkCtx())).toBe(false);
    expect(evaluate('require', mkCtx())).toBe(false);
  });

  it('cannot call constructed Function (expr-eval has no such literal)', () => {
    // expr-eval has no `new` keyword and no Function constructor surface,
    // so this is purely a parse error → false. Documenting the negative.
    expect(evaluate('Function("return process")()', mkCtx())).toBe(false);
  });

  it('cannot reach prototype chain on injected objects', () => {
    const ctx = mkCtx({ a: { status: 'done' } });
    // `__proto__` would access Object.prototype if traversal were allowed.
    // Expect false (either undefined identifier or no-op member access).
    expect(evaluate('prev.a.__proto__ == "done"', ctx)).toBe(false);
    expect(evaluate('prev.a.constructor', ctx)).toBe(false);
  });
});

describe('validateExpression — load-time parse check', () => {
  it('returns null for syntactically valid expressions', () => {
    expect(validateExpression('prev.a.status == "done"')).toBeNull();
    expect(validateExpression('1 + 1')).toBeNull();
  });

  it('returns an error message for invalid expressions', () => {
    expect(validateExpression('(((')).not.toBeNull();
    expect(validateExpression('1 + + +')).not.toBeNull();
  });

  it('returns "empty condition" for empty/whitespace', () => {
    expect(validateExpression('')).toBe('empty condition');
    expect(validateExpression('   ')).toBe('empty condition');
  });
});

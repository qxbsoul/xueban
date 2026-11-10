/**
 * run-sessions.token-parse.test.ts
 *
 * Pure regex unit tests for the inline `@skill:<id>` / `/<id>:<cmd>` token
 * parser embedded in routes/run-sessions.ts. We mirror the route regex
 * here so the test exercises the actual parse logic without spinning up
 * an Express server.
 *
 * /review (2026-05-22) regressions guarded:
 *   - A4 — slash regex must NOT eat mid-URL `:` separators
 *   - #1 / A3 — case MUST be preserved (BMAD-METHOD ≠ bmad-method)
 *   - @skill:<id> regression: existing behavior keeps working after the
 *     slash short-circuit was added
 */
import { describe, it, expect } from 'vitest';
// PR-E (Round 4) — the route now delegates `@skill` parsing to the canonical
// `parseSkillToken` module. The slash command parser is independent and
// still mirrored inline (see `slashCmdRe` below).
import { parseSkillToken } from '../lib/skill-token';

// Mirrored verbatim from server/src/routes/run-sessions.ts (W2 / review-fix).
// Keep this in sync if the route's slash regex changes.
const slashCmdRe =
  /(?:^|\s)\/([a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}):([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})(?=\s|$)/;

interface ParseResult {
  inline_skill_token: string | undefined;
  goal_text: string;
}

/** Reproduces the route's parse block exactly. */
function parseGoal(input: string): ParseResult {
  let goal_text = input.trim();
  let inline_skill_token: string | undefined;
  const sm = goal_text.match(slashCmdRe);
  if (sm) {
    inline_skill_token = `${sm[1]}:${sm[2]}`;
    goal_text = goal_text.replace(slashCmdRe, '').replace(/\s{2,}/g, ' ').trim();
    if (!goal_text) goal_text = '执行该 skill 命令。';
  } else {
    // PR-E — canonical parser replaces inline @skill regex.
    const parsed = parseSkillToken(goal_text);
    if (parsed.skill_id) {
      inline_skill_token = parsed.skill_id;
      goal_text = parsed.remaining;
      if (!goal_text) goal_text = '用这个 skill 帮我组建 agent / team。';
    }
  }
  return { inline_skill_token, goal_text };
}

describe('inline /<id>:<cmd> slash parsing', () => {
  it('matches /<id>:<cmd> at start-of-string', () => {
    const r = parseGoal('/bmad:prfaq 帮我做电商');
    expect(r.inline_skill_token).toBe('bmad:prfaq');
    expect(r.goal_text).toBe('帮我做电商');
  });

  it('matches /<id>:<cmd> after whitespace', () => {
    const r = parseGoal('帮我 /bmad:create-prd 做登录');
    expect(r.inline_skill_token).toBe('bmad:create-prd');
    expect(r.goal_text).toBe('帮我 做登录');
  });

  // /review A3 / #1 — case must be preserved
  it('preserves case in <id> (so canonical id BMAD-METHOD resolves)', () => {
    const r = parseGoal('/BMAD-METHOD:prfaq go');
    expect(r.inline_skill_token).toBe('BMAD-METHOD:prfaq');
  });
  it('preserves case in <cmd> too', () => {
    const r = parseGoal('/bmad:CreatePrd go');
    expect(r.inline_skill_token).toBe('bmad:CreatePrd');
  });

  // /review A4 — mid-URL `:` must NOT be parsed as slash command
  it('does NOT match mid-URL paths like https://api.example.com/foo:bar', () => {
    const r = parseGoal('请抓取 https://api.example.com/foo:bar 的数据');
    expect(r.inline_skill_token).toBeUndefined();
    expect(r.goal_text).toBe('请抓取 https://api.example.com/foo:bar 的数据');
  });
  it('does NOT match :: or path tail that looks command-shaped', () => {
    const r = parseGoal('see https://docs/foo:bar:baz');
    expect(r.inline_skill_token).toBeUndefined();
  });

  it('empty after stripping → fallback goal text', () => {
    const r = parseGoal('/bmad:prfaq');
    expect(r.inline_skill_token).toBe('bmad:prfaq');
    expect(r.goal_text).toBe('执行该 skill 命令。');
  });
});

describe('@skill:<id> parsing (regression after slash short-circuit)', () => {
  it('still resolves @skill:<id> alone', () => {
    const r = parseGoal('@skill:bmad-method 帮我做电商');
    expect(r.inline_skill_token).toBe('bmad-method');
    expect(r.goal_text).toBe('帮我做电商');
  });

  // /review #1 / A3 — @skill must also preserve case
  it('preserves case in @skill:<id> (BMAD-METHOD resolves verbatim)', () => {
    const r = parseGoal('@skill:BMAD-METHOD 帮我');
    expect(r.inline_skill_token).toBe('BMAD-METHOD');
  });

  it('slash + @skill both present: slash wins (more specific target)', () => {
    const r = parseGoal('/bmad:prfaq @skill:other go');
    expect(r.inline_skill_token).toBe('bmad:prfaq');
    // @skill token stays in the prose (not the inline_skill_token)
    expect(r.goal_text).toContain('@skill:other');
  });

  it('empty after stripping → fallback goal text', () => {
    const r = parseGoal('@skill:bmad');
    expect(r.inline_skill_token).toBe('bmad');
    expect(r.goal_text).toBe('用这个 skill 帮我组建 agent / team。');
  });
});

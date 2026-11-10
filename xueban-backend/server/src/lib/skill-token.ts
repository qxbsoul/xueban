/**
 * skill-token.ts — canonical `@skill` token parser (Round 4 PR-E).
 *
 * One regex, one parser. Used by `routes/run-sessions.ts` server-side to
 * strip the `@skill:<id>` / `@<id>` token out of the user's goal text, and
 * mirrored byte-equal in `src/lib/skillToken.ts` for the frontend so the
 * UI and server cannot disagree on which token will be recognised.
 *
 * Recognised forms (all case-insensitive match, but the captured `skill_id`
 * preserves the user's original casing — canonical-id.ts is case-sensitive,
 * `BMAD-METHOD` ≠ `bmad-method`):
 *   @<id>           — bare token         (e.g. `@bmad`)
 *   @skill:<id>     — colon-prefixed     (e.g. `@skill:bmad-method`)
 *   @skill <id>     — space-prefixed     (e.g. `@skill foo`)
 *
 * The boundary rule `(?:^|\s)` is load-bearing: it stops emails like
 * `user@example.com` from accidentally matching as `@example` (the `r` to
 * the left of `@` is not whitespace / start-of-string, so the regex bails).
 *
 * Note: this module covers ONLY the `@skill` family. The `/<id>:<cmd>`
 * Claude-Code-style slash command is parsed separately by an unrelated
 * regex in `run-sessions.ts` (W2 — slash wins when both are present).
 *
 * If you edit this file, also update `src/lib/skillToken.ts` (byte copy)
 * and re-run the parity test table in `__tests__/skill-token.test.ts`.
 */

/**
 * Canonical token regex. Anchored at start-of-string OR a whitespace char
 * (NOT consumed by replacement on purpose — see `parseSkillToken` below).
 * The `(?:skill[:\s]+)?` group makes `@skill:` / `@skill ` optional so a
 * bare `@bmad` still parses. The lookahead `(?=\s|$)` keeps the regex
 * from greedily eating the next word.
 */
export const SKILL_TOKEN_RE =
  /(?:^|\s)@(?:skill[:\s]+)?([a-z0-9][a-z0-9_.-]{0,63})(?=\s|$)/i;

export interface SkillTokenParseResult {
  /** Captured id with original casing preserved, or null when no token. */
  skill_id: string | null;
  /** Goal text with the matched token removed + whitespace collapsed. */
  remaining: string;
}

/**
 * Pull the `@skill` token (if any) out of `text` and return what's left.
 *
 * Whitespace handling:
 *   - The leading boundary char (space or "") is consumed along with the
 *     token so we don't leave a stray double-space.
 *   - Internal runs of whitespace are collapsed to a single space.
 *   - The result is `trim()`med — empty string is a valid remaining value
 *     and the caller is expected to substitute a default goal in that case.
 */
export function parseSkillToken(text: string): SkillTokenParseResult {
  const m = text.match(SKILL_TOKEN_RE);
  if (!m) return { skill_id: null, remaining: text };
  const remaining = text
    .replace(SKILL_TOKEN_RE, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return {
    skill_id: m[1], // preserve original casing — canonical-id is case-sensitive
    remaining,
  };
}

/**
 * skill-ingest/canonical-id.ts — derive a stable canonical skill id from a
 * skill source URL.
 *
 * The id is ALWAYS the last meaningful path segment of the URL (the repo slug
 * for git, the file basename stem for raw files). Callers MUST NOT supply a
 * custom id; doing so allows the same GitHub repo to be installed under two
 * different ids (e.g. `bmad` vs `bmad-method`), which pollutes the skill cache
 * and breaks team-yaml `team_ref` resolution.
 *
 * Borrowed from OpenDesign's `sanitizeRepoName` in
 *   D:/VScode/open-design/apps/daemon/src/library-install.ts (line 18-26)
 *
 * Hardenings vs OpenDesign (added 2026-05-22 after /review):
 *   - Preserve original case (do NOT lowercase). `team_ref: BMAD-METHOD` must
 *     match `.xueban/skills/BMAD-METHOD/` and the GitHub upstream slug.
 *   - Reject path-traversal / reserved names (`.`, `..`, ``) that pass
 *     OpenDesign's regex but would let `path.join(SKILLS_ROOT, id)` escape
 *     the skills dir. (review finding A1)
 *   - Strip GitHub `/tree/<branch>`, `/blob/<branch>/<file>`, etc URL tails
 *     so opening two different repos via "View on main" doesn't collide on
 *     id="main". (review finding A2)
 *   - Fallback id is content-addressed (sha1(url) prefix) rather than
 *     `Date.now()`, so re-running ingest with the same bad URL doesn't
 *     mint a fresh id each time. (review finding #6)
 *
 * See docs/architecture/borrowed-from-opendesign.md and
 *     docs/architecture/orchestration-transport.md for the full rationale.
 */

import crypto from 'crypto';

const SAFE_NAME_RE = /^[a-zA-Z0-9_.-]+$/;
// Names that pass SAFE_NAME_RE but are dangerous as filesystem segments.
// '..' / '.' would let `path.join(SKILLS_ROOT, id)` escape the skills dir.
const RESERVED_NAMES = new Set(['.', '..', '']);

// GitHub URLs that include a branch / file path in addition to the repo slug.
// When matched we strip everything from `/tree/...` etc onward so the last
// meaningful segment is still the repo name.
const GITHUB_BRANCH_TAIL_RE =
  /(\/tree\/|\/blob\/|\/raw\/|\/commit\/|\/issues\/|\/pulls?\/|\/releases?\/)/i;

/**
 * Derive a canonical skill id from a URL (git repo, raw file, or arbitrary).
 *
 * Algorithm:
 *   1. Trim. Empty / non-string → fallback.
 *   2. For GitHub-shaped URLs containing `/tree/`, `/blob/`, etc, truncate at
 *      that segment so the slug stays the `<owner>/<repo>` repo name.
 *   3. Strip a trailing `/`.
 *   4. Take the last `/`-delimited segment.
 *   5. Strip a trailing `.git`.
 *   6. Reject `.` / `..` / `` (path traversal) → fallback.
 *   7. Reject anything outside `[a-zA-Z0-9_.-]` → fallback.
 *   8. Truncate to 64 characters.
 *
 * @param url Any URL-shaped string (we don't try to parse it as a URL — that
 *            would reject pasted-text labels, and the fallback handles garbage).
 * @returns A safe, length-bounded canonical id, never empty, never a path-
 *          traversal segment, deterministic for a given URL.
 */
export function canonicalIdFromUrl(url: string): string {
  const raw = typeof url === 'string' ? url.trim() : '';
  if (!raw) return fallbackId(raw);

  const tailMatch = raw.match(GITHUB_BRANCH_TAIL_RE);
  const truncated = tailMatch ? raw.slice(0, tailMatch.index) : raw;

  const trimmed = truncated.replace(/\/+$/, '');
  const segments = trimmed.split('/');
  const last = segments[segments.length - 1] || '';
  const name = last.replace(/\.git$/, '');

  if (RESERVED_NAMES.has(name)) return fallbackId(raw);
  if (!SAFE_NAME_RE.test(name)) return fallbackId(raw);

  return name.slice(0, 64);
}

/** Deterministic fallback id from an arbitrary source string. Uses sha1 so
 *  re-installing the same bad input always produces the same id (no
 *  Date.now() drift across retries). */
function fallbackId(source: string): string {
  const hash = crypto.createHash('sha1').update(source).digest('hex').slice(0, 8);
  return `skill-${hash}`;
}

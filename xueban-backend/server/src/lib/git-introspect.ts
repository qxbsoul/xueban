/**
 * git-introspect.ts — Polynoia-parity git evidence for the chat timeline.
 *
 * After a run finishes inside a git-backed workspace (session.cwd), we surface
 * WHAT the agents actually changed, inline in the chat:
 *   • commit records  — `git log startHead..HEAD` (new commits made this run)
 *   • file diffs      — `git diff startHead..HEAD -- <path>` parsed into
 *                       unified DiffLine[] rows the timeline renderer knows
 *   • numstat table   — per-file +/- summary when more than one file changed
 *
 * Every helper is defensive: not-a-repo, unborn HEAD, missing git binary,
 * binary files, timeouts — all degrade to null/empty, never throw. The caller
 * (routes/run-sessions.ts finally block) treats "no evidence" as "emit
 * nothing" rather than fabricating placeholder cards.
 */
import { spawn } from 'child_process';
import type { CommitFileEntry, DiffLine } from './contracts';

const GIT_TIMEOUT_MS = 8000;

/** Run `git -C cwd <args>`; resolve stdout, or null on any failure/timeout. */
function git(cwd: string, args: string[]): Promise<string | null> {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn('git', ['-C', cwd, ...args], {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch {
      resolve(null);
      return;
    }
    let out = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill();
        resolve(null);
      }
    }, GIT_TIMEOUT_MS);
    child.stdout?.on('data', (d: Buffer) => {
      out += d.toString('utf8');
      if (out.length > 2_000_000) child.kill(); // hard cap 2 MB
    });
    child.on('error', () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(null);
    });
    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(code === 0 ? out : null);
    });
  });
}

/** HEAD commit sha, or null when cwd is not a repo / repo is unborn. */
export async function resolveGitHead(cwd: string): Promise<string | null> {
  const inside = await git(cwd, ['rev-parse', '--is-inside-work-tree']);
  if (inside?.trim() !== 'true') return null;
  const head = await git(cwd, ['rev-parse', 'HEAD']);
  return head ? head.trim() : null;
}

export interface GitCommitRecord {
  repo: string;
  sha: string;
  short_sha: string;
  message: string;
  author: string;
  date?: string;
  files_changed: number;
  insertions: number;
  deletions: number;
  files?: CommitFileEntry[];
}

export interface GitFileDiff {
  path: string;
  language?: string;
  added: number;
  removed: number;
  lines: DiffLine[];
}

export interface GitRunEvidence {
  /** New commits made during the run (oldest first). */
  commits: GitCommitRecord[];
  /** Per-file unified diffs (bounded — see MAX_DIFF_FILES). */
  fileDiffs: GitFileDiff[];
  /** Aggregate numstat rows: [path, insertions, deletions]. */
  numstat: { path: string; insertions: number; deletions: number }[];
}

const MAX_COMMITS = 10;
const MAX_DIFF_FILES = 6;
const MAX_DIFF_LINES_PER_FILE = 400;

/**
 * Parse a unified diff body (output of `git diff -- <path>`) into DiffLine[].
 * Skips the `diff --git` / `index` / `+++`/`---` prologue; renders hunk
 * headers `@@ ...` as context rows so the panel keeps visual anchors.
 */
export function parseUnifiedDiff(body: string): {
  lines: DiffLine[];
  added: number;
  removed: number;
} {
  const lines: DiffLine[] = [];
  let added = 0;
  let removed = 0;
  let no = 0;
  for (const raw of body.split('\n')) {
    if (
      raw.startsWith('diff --git') ||
      raw.startsWith('index ') ||
      raw.startsWith('--- ') ||
      raw.startsWith('+++ ') ||
      raw.startsWith('new file mode') ||
      raw.startsWith('deleted file mode') ||
      raw.startsWith('Binary files') ||
      raw === '\\ No newline at end of file'
    ) {
      continue;
    }
    if (raw.startsWith('@@')) {
      no += 1;
      lines.push({ no, mark: ' ', code: raw.slice(0, 80) });
      continue;
    }
    if (lines.length >= MAX_DIFF_LINES_PER_FILE) {
      no += 1;
      lines.push({ no, mark: ' ', code: `… (${MAX_DIFF_LINES_PER_FILE} 行截断)` });
      break;
    }
    no += 1;
    if (raw.startsWith('+')) {
      added += 1;
      lines.push({ no, mark: '+', code: raw.slice(1) });
    } else if (raw.startsWith('-')) {
      removed += 1;
      lines.push({ no, mark: '-', code: raw.slice(1) });
    } else {
      lines.push({ no, mark: ' ', code: raw.startsWith(' ') ? raw.slice(1) : raw });
    }
  }
  return { lines, added, removed };
}

function guessLanguage(path: string): string | undefined {
  const ext = path.includes('.') ? path.split('.').pop()!.toLowerCase() : '';
  const map: Record<string, string> = {
    ts: 'ts', tsx: 'ts', js: 'js', jsx: 'js', mjs: 'js', cjs: 'js',
    py: 'py', rs: 'rust', go: 'go', java: 'java', rb: 'ruby',
    json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    md: 'markdown', html: 'html', css: 'css', scss: 'scss',
    sh: 'bash', ps1: 'powershell', sql: 'sql',
  };
  return map[ext];
}

function repoDisplayName(cwd: string): string {
  const norm = cwd.replace(/\\/g, '/');
  const parts = norm.split('/').filter(Boolean);
  return parts[parts.length - 1] ?? norm;
}

/**
 * Collect git evidence for the commits/diffs between `startHead` (captured
 * before the run started) and current HEAD. When `startHead` is null (repo
 * was unborn or unknown at start), falls back to `git log --since` against
 * the session's created_at timestamp. Returns null when there is nothing
 * reportable (not a repo / no commits / no changes).
 */
export async function inspectGitRun(
  cwd: string,
  startHead: string | null,
  sinceIso?: string,
): Promise<GitRunEvidence | null> {
  const head = await resolveGitHead(cwd);
  if (!head) return null;

  // ── commit range ─────────────────────────────────────────────────────────
  // startHead..HEAD is exact when we captured HEAD at run start. Otherwise
  // --since=<created_at> heuristically picks the run's commits.
  let logArgs: string[];
  if (startHead && startHead !== head) {
    logArgs = ['log', '--no-color', `--pretty=format:%H%x00%h%x00%an%x00%aI%x00%s`, `${startHead}..HEAD`];
  } else if (startHead === head) {
    return null; // nothing new committed during the run
  } else if (sinceIso) {
    logArgs = ['log', '--no-color', `--pretty=format:%H%x00%h%x00%an%x00%aI%x00%s`, `--since=${sinceIso}`, '-n', '10'];
  } else {
    return null;
  }
  const logOut = await git(cwd, logArgs);
  const commits: GitCommitRecord[] = [];
  if (logOut) {
    for (const line of logOut.split('\n')) {
      const cols = line.split('\x00');
      if (cols.length < 5) continue;
      const [sha, short, author, date, message] = cols;
      commits.push({
        repo: repoDisplayName(cwd),
        sha: sha!,
        short_sha: short!,
        author: author ?? '',
        date: date || undefined,
        message: (message ?? '').trim(),
        files_changed: 0,
        insertions: 0,
        deletions: 0,
      });
      if (commits.length >= MAX_COMMITS) break;
    }
  }

  // ── numstat (per-file +/-) ───────────────────────────────────────────────
  const diffRef = startHead && startHead !== head ? `${startHead}..HEAD` : 'HEAD';
  const numstatOut = await git(cwd, ['diff', '--no-color', '--numstat', diffRef]);
  const numstat: { path: string; insertions: number; deletions: number }[] = [];
  if (numstatOut) {
    for (const line of numstatOut.split('\n')) {
      const m = /^(\d+|-)\s+(\d+|-)\s+(.+)$/.exec(line.trim());
      if (!m) continue;
      numstat.push({
        path: m[3]!.trim(),
        insertions: m[1] === '-' ? 0 : Number(m[1]),
        deletions: m[2] === '-' ? 0 : Number(m[2]),
      });
    }
  }

  if (commits.length === 0 && numstat.length === 0) return null;

  // Stamp aggregate stats onto commit records (single-commit runs only, so
  // the numbers are actually attributable; multi-commit shows 0 + table).
  if (commits.length === 1 && numstat.length > 0) {
    const only = commits[0]!;
    only.files_changed = numstat.length;
    only.insertions = numstat.reduce((s, n) => s + n.insertions, 0);
    only.deletions = numstat.reduce((s, n) => s + n.deletions, 0);
    only.files = numstat.slice(0, 20).map((n) => ({
      path: n.path,
      status: 'M',
    }));
  }

  // ── per-file unified diffs (bounded) ─────────────────────────────────────
  const fileDiffs: GitFileDiff[] = [];
  const diffTargets = numstat.slice(0, MAX_DIFF_FILES);
  for (const n of diffTargets) {
    const body = await git(cwd, ['diff', '--no-color', '-U2', diffRef, '--', n.path]);
    if (!body) continue;
    const parsed = parseUnifiedDiff(body);
    if (parsed.lines.length === 0) continue;
    fileDiffs.push({
      path: n.path,
      language: guessLanguage(n.path),
      added: parsed.added,
      removed: parsed.removed,
      lines: parsed.lines,
    });
  }

  return { commits, fileDiffs, numstat };
}

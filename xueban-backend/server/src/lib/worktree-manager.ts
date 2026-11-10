/**
 * worktree-manager.ts — Polynoia-parity per-agent git worktree lifecycle.
 *
 * Polynoia's core isolation loop (docs/superpowers/specs §orchestration):
 * every (agent, conversation) gets ONE branch + ONE worktree inside a shared
 * .git; agents work in isolation; after the turn the orchestrator merges each
 * branch with `git merge --no-ff` and surfaces conflicts as needs-manual.
 *
 * Xueban mapping (group chat team-DAG path, groups-chat.ts):
 *   repoRoot   — the group's resolved workspace dir (must be a git repo)
 *   runKey     — one fanout round (groupId + turn slug)
 *   agentId    — the team member working this round
 *
 * Layout (everything under the repo's .xueban so user trees stay clean):
 *   <repoRoot>/.xueban/worktrees/<runKey>/<agentId>     ← agent worktree
 *   <repoRoot>/.xueban/worktrees/<runKey>/integration    ← integration worktree
 * Branches:
 *   agent/<agentId>/<runKey>     ← per-agent branch, created from turn base
 *   sf/run/<runKey>              ← integration branch, merged per agent branch
 *
 * Safety boundaries:
 *   • NEVER touches the user's checked-out branch — integration happens in a
 *     dedicated worktree; promoting sf/run/* to main stays a manual step.
 *   • Every helper is defensive: non-repo / unborn HEAD / missing git binary /
 *     timeout → null or a failed result, never a throw (mirrors
 *     git-introspect.ts so callers can treat "no worktree" as "plain mode").
 */
import { spawn } from 'child_process';
import { realpathSync } from 'fs';
import path from 'path';
import type { CommitFileEntry } from './contracts';

const GIT_TIMEOUT_MS = 15000;

/**
 * Canonical comparison key for a filesystem path.
 *
 * `git worktree list --porcelain` always reports the long/canonical form, while
 * a path we build ourselves can still carry a Windows 8.3 short component
 * (e.g. `C:\Users\TERRYS~1\AppData\...` when TEMP is short-named). `path.resolve`
 * does NOT expand short names, so the two forms never compare equal and an
 * existing worktree is treated as missing — the follow-up `worktree add` then
 * fails ("already exists" / "already checked out") and the caller silently
 * degrades to plain mode. `realpathSync.native` asks the OS for the canonical
 * path, which expands 8.3 names and resolves symlinks; paths that do not exist
 * (yet) fall back to a plain resolve.
 */
function canonicalPathKey(p: string): string {
  let resolved: string;
  try {
    resolved = realpathSync.native(p);
  } catch {
    resolved = path.resolve(p);
  }
  return resolved.toLowerCase();
}

function git(cwd: string, args: string[]): Promise<{ ok: boolean; out: string }> {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn('git', ['-C', cwd, ...args], {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch {
      resolve({ ok: false, out: '' });
      return;
    }
    let out = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        child.kill();
        resolve({ ok: false, out });
      }
    }, GIT_TIMEOUT_MS);
    child.stdout?.on('data', (d: Buffer) => {
      out += d.toString('utf8');
      if (out.length > 1_000_000) child.kill();
    });
    child.stderr?.on('data', (d: Buffer) => {
      out += d.toString('utf8');
    });
    child.on('error', () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: false, out });
    });
    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ ok: code === 0, out });
    });
  });
}

/** Sanitize a free-form key into a filesystem/branch-safe slug. */
function slug(key: string): string {
  return key
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48) || 'run';
}

export interface PreparedWorktree {
  dir: string;
  branch: string;
  /** Base commit the branch was created from — evidence diff anchor. */
  baseSha: string;
}

export function worktreePaths(repoRoot: string, runKey: string): {
  root: string;
  integrationDir: () => string;
  agentDir: (agentId: string) => string;
} {
  const root = path.join(repoRoot, '.xueban', 'worktrees', slug(runKey));
  return {
    root,
    integrationDir: () => path.join(root, 'integration'),
    agentDir: (agentId: string) => path.join(root, slug(agentId)),
  };
}

export function worktreeBranchNames(runKey: string): {
  agent: (agentId: string) => string;
  integration: string;
} {
  const key = slug(runKey);
  return {
    agent: (agentId: string) => `agent/${slug(agentId)}/${key}`,
    integration: `sf/run/${key}`,
  };
}

/**
 * Whether the directory is a git repo with at least one commit (we need a
 * committable base to branch worktrees from). Cheap probe, never throws.
 */
export async function isGitWorkable(dir: string): Promise<boolean> {
  const inside = await git(dir, ['rev-parse', '--is-inside-work-tree']);
  if (!inside.ok || inside.out.trim() !== 'true') return false;
  const head = await git(dir, ['rev-parse', 'HEAD']);
  return head.ok;
}

/**
 * Prepare (or reset) one agent's worktree for the round — Polynoia's
 * turn-start reset semantics:
 *   • new round → `git worktree add -b <branch> <dir> <base>`
 *   • stale worktree from an earlier attempt → `reset --hard <base>` +
 *     `clean -fd` so the agent starts from the integration baseline, never
 *     from a half-finished prior attempt.
 * Returns null when the repo isn't workable (caller falls back to plain
 * shared-workspace mode — never fabricated).
 */
export async function prepareWorktree(
  repoRoot: string,
  runKey: string,
  agentId: string,
  baseRef = 'HEAD',
): Promise<PreparedWorktree | null> {
  if (!(await isGitWorkable(repoRoot))) return null;
  const paths = worktreePaths(repoRoot, runKey);
  const branches = worktreeBranchNames(runKey);
  const dir = paths.agentDir(agentId);
  const branch = branches.agent(agentId);

  // Resolve the base commit ONCE (HEAD of the user's repo at turn start).
  const base = await git(repoRoot, ['rev-parse', baseRef]);
  if (!base.ok) return null;
  const baseSha = base.out.trim();

  const existing = await git(repoRoot, ['worktree', 'list', '--porcelain']);
  const dirNormalized = canonicalPathKey(dir);
  const alreadyThere = existing.ok
    && existing.out
      .split('\n')
      .some((l) => l.startsWith('worktree ') && canonicalPathKey(l.slice(9).trim()) === dirNormalized);

  if (alreadyThere) {
    // Turn-reset: hard reset to base + remove untracked files.
    const checkout = await git(dir, ['checkout', '-q', branch]);
    if (!checkout.ok) {
      // Branch was deleted out-of-band — recreate it.
      await git(dir, ['checkout', '-q', '-B', branch, baseSha]);
    } else {
      await git(dir, ['reset', '--hard', baseSha]);
    }
    await git(dir, ['clean', '-fdq']);
    return { dir, branch, baseSha };
  }

  // Branch may exist from an earlier round without a worktree — force re-create
  // at the fresh base so rounds never inherit stale history.
  const add = await git(repoRoot, [
    'worktree', 'add', '--force', '-B', branch, dir, baseSha,
  ]);
  if (!add.ok) {
    // Last resort: plain add without -B (branch exists and is checked out
    // elsewhere — pathological; report failure as null).
    const retry = await git(repoRoot, ['worktree', 'add', dir, branch]);
    if (!retry.ok) return null;
  }
  return { dir, branch, baseSha };
}

export interface WorktreeCommit {
  /** null when the worktree had nothing to commit (clean run). */
  sha: string | null;
  short_sha: string;
  message: string;
  author: string;
  date: string;
  files_changed: number;
  insertions: number;
  deletions: number;
  files: CommitFileEntry[];
}

/**
 * Commit whatever the agent left in its worktree (Polynoia: "clean commits
 * after a successful turn are auto-integrated"). No-op (files_changed=0,
 * sha=null) when the tree is clean. Author identity is the AGENT, so git
 * history attributes work per teammate.
 */
export async function commitWorktree(
  dir: string,
  message: string,
  authorName: string,
): Promise<WorktreeCommit | null> {
  const status = await git(dir, ['status', '--porcelain']);
  if (!status.ok) return null;
  if (!status.out.trim()) {
    return {
      sha: null, short_sha: '', message, author: authorName, date: new Date().toISOString(),
      files_changed: 0, insertions: 0, deletions: 0, files: [],
    };
  }
  const add = await git(dir, ['add', '-A']);
  if (!add.ok) return null;
  const iso = new Date().toISOString();
  const commit = await git(dir, [
    '-c', `user.name=${authorName}`,
    '-c', 'user.email=agent@xueban.dev',
    'commit', '-m', message, `--date=${iso}`,
  ]);
  if (!commit.ok) return null;
  const shaRes = await git(dir, ['rev-parse', 'HEAD']);
  const shortRes = await git(dir, ['rev-parse', '--short', 'HEAD']);
  const stat = await git(dir, ['show', '--numstat', '--format=', 'HEAD']);
  const files: CommitFileEntry[] = [];
  let insertions = 0;
  let deletions = 0;
  if (stat.ok) {
    for (const line of stat.out.split('\n')) {
      const m = /^(\d+|-)\s+(\d+|-)\s+(.+)$/.exec(line.trim());
      if (!m) continue;
      files.push({ path: m[3]!.trim(), status: 'M' });
      insertions += m[1] === '-' ? 0 : Number(m[1]);
      deletions += m[2] === '-' ? 0 : Number(m[2]);
    }
  }
  return {
    sha: shaRes.ok ? shaRes.out.trim() : null,
    short_sha: shortRes.ok ? shortRes.out.trim() : '',
    message,
    author: authorName,
    date: iso,
    files_changed: files.length,
    insertions,
    deletions,
    files,
  };
}

export interface IntegrationResult {
  branch: string;
  ok: boolean;
  /** Populated when ok === false — conflicted paths, needs-manual. */
  conflicts: string[];
  insertions: number;
  deletions: number;
  /** Merge commit sha (ok === true only). */
  mergeSha?: string;
  shortSha?: string;
}

/**
 * Merge every agent branch into the round's integration branch
 * (`sf/run/<runKey>`), checked out in a DEDICATED integration worktree — the
 * user's main checkout is never touched. Sequential merge order = input order
 * (team layer order when the caller passes layers in execution order).
 * Conflicting branches are aborted (`merge --abort`) and reported with their
 * conflicted paths — Polynoia's "conflict → needs-manual" surfacing.
 */
export async function integrateAll(
  repoRoot: string,
  runKey: string,
  branches: string[],
): Promise<IntegrationResult[] | null> {
  if (branches.length === 0) return [];
  if (!(await isGitWorkable(repoRoot))) return null;

  const paths = worktreePaths(repoRoot, runKey);
  const names = worktreeBranchNames(runKey);
  const integDir = paths.integrationDir();
  const integBranch = names.integration;

  const base = await git(repoRoot, ['rev-parse', 'HEAD']);
  if (!base.ok) return null;
  const baseSha = base.out.trim();

  // Integration worktree: reuse + hard-reset when a stale one exists.
  const existing = await git(repoRoot, ['worktree', 'list', '--porcelain']);
  const integNormalized = canonicalPathKey(integDir);
  const alreadyThere = existing.ok
    && existing.out
      .split('\n')
      .some((l) => l.startsWith('worktree ') && canonicalPathKey(l.slice(9).trim()) === integNormalized);

  if (alreadyThere) {
    const co = await git(integDir, ['checkout', '-q', '-B', integBranch, baseSha]);
    if (!co.ok) return null;
    await git(integDir, ['reset', '--hard', baseSha]);
    await git(integDir, ['clean', '-fdq']);
  } else {
    const add = await git(repoRoot, [
      'worktree', 'add', '--force', '-B', integBranch, integDir, baseSha,
    ]);
    if (!add.ok) return null;
  }

  const results: IntegrationResult[] = [];
  for (const branch of branches) {
    const merge = await git(integDir, ['merge', '--no-ff', '-m', `merge ${branch}`, branch]);
    if (merge.ok) {
      const stat = await git(integDir, ['diff', '--numstat', `${integBranch}~1`, integBranch]);
      let insertions = 0;
      let deletions = 0;
      if (stat.ok) {
        for (const line of stat.out.split('\n')) {
          const m = /^(\d+|-)\s+(\d+|-)\s+/.exec(line.trim());
          if (!m) continue;
          insertions += m[1] === '-' ? 0 : Number(m[1]);
          deletions += m[2] === '-' ? 0 : Number(m[2]);
        }
      }
      const shaRes = await git(integDir, ['rev-parse', 'HEAD']);
      const shortRes = await git(integDir, ['rev-parse', '--short', 'HEAD']);
      results.push({
        branch, ok: true, conflicts: [], insertions, deletions,
        ...(shaRes.ok ? { mergeSha: shaRes.out.trim() } : {}),
        ...(shortRes.ok ? { shortSha: shortRes.out.trim() } : {}),
      });
      continue;
    }
    // Conflict (or failure): collect unmerged paths, then abort so the
    // integration branch stays clean for the remaining branches.
    const unmerged = await git(integDir, ['diff', '--name-only', '--diff-filter=U']);
    const conflicts = unmerged.ok
      ? unmerged.out.split('\n').map((l) => l.trim()).filter(Boolean)
      : [];
    await git(integDir, ['merge', '--abort']);
    results.push({ branch, ok: false, conflicts, insertions: 0, deletions: 0 });
  }
  return results;
}

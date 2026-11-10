/**
 * worktree-manager.test.ts — Polynoia worktree lifecycle against a REAL temp
 * git repo: prepare (fresh + turn-reset), commit (clean/dirty), integrate
 * (clean merge / same-line conflict), and non-git degradation.
 */
import { execFile } from 'child_process';
import { mkdtempSync, rmSync, writeFileSync, existsSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';
import { describe, expect, it, beforeAll, afterAll } from 'vitest';
import {
  commitWorktree,
  integrateAll,
  isGitWorkable,
  prepareWorktree,
} from '../worktree-manager';

function run(cwd: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile('git', ['-C', cwd, ...args], { windowsHide: true }, (err, stdout, stderr) => {
      if (err) reject(new Error(`${err.message}\n${stderr}`));
      else resolve(stdout);
    });
  });
}

async function commitAll(cwd: string, msg: string): Promise<void> {
  await run(cwd, ['config', 'user.email', 't@t']);
  await run(cwd, ['config', 'user.name', 't']);
  await run(cwd, ['add', '-A']);
  await run(cwd, ['commit', '-m', msg]);
}

describe('worktree-manager (real temp repo)', () => {
  let repo = '';

  beforeAll(async () => {
    repo = mkdtempSync(path.join(tmpdir(), 'sf-wt-'));
    await run(repo, ['init', '-q']);
    writeFileSync(path.join(repo, 'shared.txt'), 'line1\nline2\n');
    await commitAll(repo, 'base');
  });

  afterAll(() => {
    if (repo) rmSync(repo, { recursive: true, force: true });
  });

  it('isGitWorkable true in repo, false in plain dir', async () => {
    expect(await isGitWorkable(repo)).toBe(true);
    const dir = mkdtempSync(path.join(tmpdir(), 'sf-plain-'));
    try {
      expect(await isGitWorkable(dir)).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('prepareWorktree creates isolated branch+dir per agent', async () => {
    const a = await prepareWorktree(repo, 'r1', 'writer');
    const b = await prepareWorktree(repo, 'r1', 'reviewer');
    expect(a).not.toBeNull();
    expect(b).not.toBeNull();
    expect(a!.dir).not.toBe(b!.dir);
    expect(existsSync(path.join(a!.dir, 'shared.txt'))).toBe(true);
    // Writes in one worktree are invisible in the other + in the main checkout.
    writeFileSync(path.join(a!.dir, 'a-only.txt'), 'x');
    expect(existsSync(path.join(b!.dir, 'a-only.txt'))).toBe(false);
    expect(existsSync(path.join(repo, 'a-only.txt'))).toBe(false);
  });

  it('prepareWorktree resets a stale worktree at turn start (Polynoia semantics)', async () => {
    const wt = await prepareWorktree(repo, 'r2', 'writer');
    expect(wt).not.toBeNull();
    writeFileSync(path.join(wt!.dir, 'stale.txt'), 'junk');
    const again = await prepareWorktree(repo, 'r2', 'writer');
    expect(again).not.toBeNull();
    // reset --hard + clean -fd: stale file gone, baseline restored.
    expect(existsSync(path.join(again!.dir, 'stale.txt'))).toBe(false);
  });

  it('commitWorktree: clean tree → sha null; dirty → commit with agent author', async () => {
    const wt = await prepareWorktree(repo, 'r3', 'writer');
    const clean = await commitWorktree(wt!.dir, 'nothing to commit', 'Writer');
    expect(clean).not.toBeNull();
    expect(clean!.sha).toBeNull();

    writeFileSync(path.join(wt!.dir, 'new.txt'), 'hello\n');
    const commit = await commitWorktree(wt!.dir, 'add new.txt', 'Writer');
    expect(commit).not.toBeNull();
    expect(commit!.sha).toBeTruthy();
    expect(commit!.author).toBe('Writer');
    expect(commit!.files_changed).toBe(1);
    expect(commit!.insertions).toBe(1);
    // Identity via -c overrides (not repo config).
    const authorLine = await run(wt!.dir, ['log', '-1', '--format=%an']);
    expect(authorLine.trim()).toBe('Writer');
  });

  it('integrateAll merges non-conflicting branches cleanly', async () => {
    const a = await prepareWorktree(repo, 'r4', 'writer');
    const b = await prepareWorktree(repo, 'r4', 'reviewer');
    writeFileSync(path.join(a!.dir, 'from-a.txt'), 'a\n');
    writeFileSync(path.join(b!.dir, 'from-b.txt'), 'b\n');
    const ca = await commitWorktree(a!.dir, 'a work', 'Writer');
    const cb = await commitWorktree(b!.dir, 'b work', 'Reviewer');
    expect(ca!.sha).toBeTruthy();
    expect(cb!.sha).toBeTruthy();

    const results = await integrateAll(repo, 'r4', [a!.branch, b!.branch]);
    expect(results).not.toBeNull();
    expect(results).toHaveLength(2);
    expect(results!.every((r) => r.ok)).toBe(true);
    expect(results![0]!.mergeSha).toBeTruthy();
    // Integration branch carries BOTH files; user's checked-out tree untouched.
    const integDir = path.join(repo, '.xueban', 'worktrees', 'r4', 'integration');
    expect(existsSync(path.join(integDir, 'from-a.txt'))).toBe(true);
    expect(existsSync(path.join(integDir, 'from-b.txt'))).toBe(true);
    expect(existsSync(path.join(repo, 'from-a.txt'))).toBe(false);
  });

  it('integrateAll surfaces same-line conflicts as needs-manual', async () => {
    const a = await prepareWorktree(repo, 'r5', 'writer');
    const b = await prepareWorktree(repo, 'r5', 'reviewer');
    writeFileSync(path.join(a!.dir, 'shared.txt'), 'line1\nWRONG-A\n');
    writeFileSync(path.join(b!.dir, 'shared.txt'), 'line1\nWRONG-B\n');
    await commitWorktree(a!.dir, 'edit shared', 'Writer');
    await commitWorktree(b!.dir, 'edit shared', 'Reviewer');

    const results = await integrateAll(repo, 'r5', [a!.branch, b!.branch]);
    expect(results).not.toBeNull();
    // First merge wins (fast path), second conflicts on shared.txt.
    const conflict = results!.find((r) => !r.ok);
    expect(conflict).toBeDefined();
    expect(conflict!.conflicts).toContain('shared.txt');
    // Integration tree stays usable (abort cleaned the conflict state).
    const integDir = path.join(repo, '.xueban', 'worktrees', 'r5', 'integration');
    expect(readFileSync(path.join(integDir, 'shared.txt'), 'utf8')).toContain('WRONG-');
  });

  it('integrateAll returns [] for empty branch list, null for non-repo', async () => {
    expect(await integrateAll(repo, 'r6', [])).toEqual([]);
    const dir = mkdtempSync(path.join(tmpdir(), 'sf-plain2-'));
    try {
      expect(await integrateAll(dir, 'r6', ['agent/x/r6'])).toBeNull();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

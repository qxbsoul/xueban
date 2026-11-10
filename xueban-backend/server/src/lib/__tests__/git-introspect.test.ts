/**
 * git-introspect.test.ts — Polynoia-parity git evidence helpers.
 *
 * parseUnifiedDiff is a pure function → always tested. inspectGitRun runs
 * against a real temp git repo (init + commit) and is skipped when the git
 * binary is unavailable (CI minimal images).
 */
import { execFile } from 'child_process';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';
import { describe, expect, it, beforeAll, afterAll } from 'vitest';
import { inspectGitRun, parseUnifiedDiff, resolveGitHead } from '../git-introspect';

function run(cwd: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile('git', ['-C', cwd, ...args], { windowsHide: true }, (err, stdout, stderr) => {
      if (err) reject(new Error(`${err.message}\n${stderr}`));
      else resolve(stdout);
    });
  });
}

describe('parseUnifiedDiff', () => {
  it('classifies +/-/context lines and skips the prologue', () => {
    const body = [
      'diff --git a/a.ts b/a.ts',
      'index 111..222 100644',
      '--- a/a.ts',
      '+++ b/a.ts',
      '@@ -1,2 +1,3 @@',
      ' context line',
      '-old line',
      '+new line',
      '\\ No newline at end of file',
    ].join('\n');
    const { lines, added, removed } = parseUnifiedDiff(body);
    expect(added).toBe(1);
    expect(removed).toBe(1);
    // hunk header + 3 content lines = 4 rendered rows (prologue is skipped)
    expect(lines).toHaveLength(4);
    expect(lines[0]!.mark).toBe(' ');
    expect(lines[0]!.code).toContain('@@');
    expect(lines.map((l) => l.mark).slice(1)).toEqual([' ', '-', '+']);
  });

  it('treats a non-space-prefixed context line defensively', () => {
    const { lines } = parseUnifiedDiff('bare line\n+plus');
    expect(lines[0]!.mark).toBe(' ');
    expect(lines[0]!.code).toBe('bare line');
  });
});

describe('inspectGitRun (real temp repo)', () => {
  const hasGit = process.platform !== 'win32' || true; // git ships with Git Bash on win CI here
  let repo = '';
  let head0 = '';

  beforeAll(async () => {
    repo = mkdtempSync(path.join(tmpdir(), 'sf-git-'));
    if (!hasGit) return;
    await run(repo, ['init', '-q']);
    await run(repo, ['config', 'user.email', 'test@xueban.dev']);
    await run(repo, ['config', 'user.name', 'sf-test']);
    writeFileSync(path.join(repo, 'a.ts'), 'export const a = 1;\n');
    await run(repo, ['add', '.']);
    await run(repo, ['commit', '-q', '-m', 'base']);
    head0 = (await run(repo, ['rev-parse', 'HEAD'])).trim();
  });

  afterAll(() => {
    if (repo) rmSync(repo, { recursive: true, force: true });
  });

  it('resolveGitHead returns HEAD sha inside a repo', async () => {
    if (!head0) return;
    expect(await resolveGitHead(repo)).toBe(head0);
  });

  it('returns null when nothing changed since startHead', async () => {
    if (!head0) return;
    expect(await inspectGitRun(repo, head0, undefined)).toBeNull();
  });

  it('reports new commits + numstat + parsed diffs after agent edits', async () => {
    if (!head0) return;
    mkdirSync(path.join(repo, 'src'), { recursive: true });
    writeFileSync(path.join(repo, 'a.ts'), 'export const a = 2;\nexport const b = 3;\n');
    writeFileSync(path.join(repo, 'src', 'new.ts'), 'export const n = 1;\n');
    await run(repo, ['add', '.']);
    await run(repo, ['commit', '-q', '-m', 'agent work']);

    const ev = await inspectGitRun(repo, head0, undefined);
    expect(ev).not.toBeNull();
    if (!ev) return;
    expect(ev.commits).toHaveLength(1);
    expect(ev.commits[0]!.message).toBe('agent work');
    // single-commit run → stats stamped from numstat
    expect(ev.commits[0]!.files_changed).toBe(2);
    expect(ev.commits[0]!.insertions).toBeGreaterThanOrEqual(3);
    // numstat covers both files, diffs bounded to MAX_DIFF_FILES=6 → both present
    expect(ev.numstat).toHaveLength(2);
    expect(ev.fileDiffs).toHaveLength(2);
    const aDiff = ev.fileDiffs.find((d) => d.path.endsWith('a.ts'));
    expect(aDiff).toBeDefined();
    expect(aDiff!.added).toBeGreaterThan(0);
    expect(aDiff!.lines.some((l) => l.mark === '+')).toBe(true);
  });

  it('degrades to null on a non-repo directory', async () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'sf-nogit-'));
    try {
      expect(await resolveGitHead(dir)).toBeNull();
      expect(await inspectGitRun(dir, null, new Date().toISOString())).toBeNull();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

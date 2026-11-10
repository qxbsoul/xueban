/**
 * skill-ingest/fetch.ts — resolve a user-supplied skill source to a local dir.
 *
 * Supported sources:
 *   - github.com/<org>/<repo> (with or without https://)  → shallow git clone
 *   - github.com/<org>/<repo>/tree/<branch>/<sub/path>    → clone + extract subdir
 *   - raw.githubusercontent.com/...md                     → single file fetch
 *   - http(s)://...md (any raw markdown)                  → single file fetch
 *   - pasted markdown text                                → write as SKILL.md
 *
 * Output: an absolute path to a temp directory under
 *   .xueban/cache/skill-ingest/<sha1>/
 * Caller is expected to probe it then register the relevant files into
 * .xueban/skills/<id>/ via register.ts.
 */

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { spawnSync } from 'child_process';
import https from 'https';
import http from 'http';

const CACHE_ROOT = path.join(process.cwd(), '.xueban', 'cache', 'skill-ingest');

export type SourceKind = 'git-repo' | 'raw-file' | 'pasted-text';

export interface FetchResult {
  kind: SourceKind;
  /** absolute path to a directory containing fetched files */
  dir: string;
  /** original source string (URL or first 80 chars of pasted text) */
  source_label: string;
  /** sha1 of the source used as cache key */
  source_hash: string;
  /** the inferred repo / file name, used downstream as default skill id */
  inferred_name: string;
  /** optional sub-path inside the cloned repo (when github tree URL had one) */
  subpath?: string;
}

interface ParsedSource {
  kind: SourceKind;
  url?: string;          // for git/raw
  branch?: string;       // for git tree URLs
  subpath?: string;      // for git tree URLs
  text?: string;         // for pasted text
  inferred_name: string;
}

/**
 * Decide which kind of source the user gave us. Defensive — defaults to
 * pasted-text on ambiguous input so we never accidentally fetch a URL the
 * user just wanted to quote.
 */
export function parseSource(raw: string): ParsedSource {
  const s = raw.trim();

  // Pasted text (multi-line, or has prose + no URL pattern) → text
  if (s.length > 0 && !/^https?:\/\//i.test(s) && !/^github\.com\//i.test(s)) {
    const first = s.split('\n')[0].slice(0, 40).replace(/[^a-z0-9-]+/gi, '-').toLowerCase();
    return {
      kind: 'pasted-text',
      text: s,
      inferred_name: first || 'pasted-skill',
    };
  }

  const url = /^https?:\/\//i.test(s) ? s : `https://${s}`;
  const u = new URL(url);

  // github tree URL: https://github.com/org/repo/tree/branch/sub/path
  const ghTree = u.pathname.match(/^\/([^/]+)\/([^/]+)\/tree\/([^/]+)(\/.*)?$/);
  if (u.hostname === 'github.com' && ghTree) {
    return {
      kind: 'git-repo',
      url: `https://github.com/${ghTree[1]}/${ghTree[2]}.git`,
      branch: ghTree[3],
      subpath: (ghTree[4] ?? '').replace(/^\//, '') || undefined,
      inferred_name: ghTree[2].toLowerCase(),
    };
  }

  // github repo URL: https://github.com/org/repo[.git]
  const ghRepo = u.pathname.match(/^\/([^/]+)\/([^/]+?)(\.git)?\/?$/);
  if (u.hostname === 'github.com' && ghRepo) {
    return {
      kind: 'git-repo',
      url: `https://github.com/${ghRepo[1]}/${ghRepo[2]}.git`,
      inferred_name: ghRepo[2].toLowerCase(),
    };
  }

  // anything else http(s) — treat as raw file
  const fname = path.basename(u.pathname) || 'skill.md';
  return {
    kind: 'raw-file',
    url,
    inferred_name: fname.replace(/\.\w+$/, '').toLowerCase() || 'remote-skill',
  };
}

function ensureDir(p: string): void {
  fs.mkdirSync(p, { recursive: true });
}

function hashOf(s: string): string {
  return crypto.createHash('sha1').update(s).digest('hex').slice(0, 12);
}

function downloadToFile(url: string, dest: string, redirectsLeft = 5): Promise<void> {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith('https://') ? https : http;
    lib
      .get(url, { headers: { 'User-Agent': 'xueban-skill-ingest' } }, (res) => {
        if (
          res.statusCode &&
          res.statusCode >= 300 &&
          res.statusCode < 400 &&
          res.headers.location &&
          redirectsLeft > 0
        ) {
          const next = new URL(res.headers.location, url).toString();
          res.resume();
          downloadToFile(next, dest, redirectsLeft - 1).then(resolve).catch(reject);
          return;
        }
        if (res.statusCode !== 200) {
          reject(new Error(`HTTP ${res.statusCode} fetching ${url}`));
          res.resume();
          return;
        }
        const file = fs.createWriteStream(dest);
        res.pipe(file);
        file.on('finish', () => file.close(() => resolve()));
        file.on('error', (err) => {
          fs.unlinkSync(dest);
          reject(err);
        });
      })
      .on('error', reject);
  });
}

/**
 * Fetch a skill source. Idempotent — re-running with the same input reuses
 * the same cache dir.
 */
export async function fetchSkill(raw: string): Promise<FetchResult> {
  const parsed = parseSource(raw);
  const sourceHash = hashOf(raw.trim());
  const dir = path.join(CACHE_ROOT, sourceHash);

  // If cache exists and is non-empty, reuse it.
  if (fs.existsSync(dir) && fs.readdirSync(dir).length > 0) {
    return {
      kind: parsed.kind,
      dir,
      source_label: raw.length > 80 ? raw.slice(0, 80) + '…' : raw,
      source_hash: sourceHash,
      inferred_name: parsed.inferred_name,
      subpath: parsed.subpath,
    };
  }

  ensureDir(dir);

  if (parsed.kind === 'pasted-text') {
    fs.writeFileSync(path.join(dir, 'SKILL.md'), parsed.text!, 'utf-8');
  } else if (parsed.kind === 'raw-file') {
    const fname = path.basename(new URL(parsed.url!).pathname) || 'skill.md';
    await downloadToFile(parsed.url!, path.join(dir, fname));
  } else if (parsed.kind === 'git-repo') {
    const args = ['clone', '--depth', '1'];
    if (parsed.branch) args.push('--branch', parsed.branch);
    args.push(parsed.url!, dir);
    const proc = spawnSync('git', args, { encoding: 'utf-8' });
    if (proc.status !== 0) {
      // Clean up partial dir so cache hit isn't poisoned.
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* noop */ }
      throw new Error(`git clone failed (${proc.status}): ${proc.stderr || proc.stdout}`);
    }
    // Strip .git to keep cache clean and avoid `git status` noise in the host repo.
    try { fs.rmSync(path.join(dir, '.git'), { recursive: true, force: true }); } catch { /* noop */ }
  }

  return {
    kind: parsed.kind,
    dir,
    source_label: raw.length > 80 ? raw.slice(0, 80) + '…' : raw,
    source_hash: sourceHash,
    inferred_name: parsed.inferred_name,
    subpath: parsed.subpath,
  };
}

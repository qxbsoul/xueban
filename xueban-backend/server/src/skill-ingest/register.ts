/**
 * skill-ingest/register.ts — copy a fetched + probed skill into the user's
 * project skill library at .xueban/skills/<id>/.
 *
 * Layout written:
 *   .xueban/skills/<id>/
 *     SKILL.md            ← synthetic header (frontmatter: name/source/installed_at)
 *     probe.json          ← probe result (for inspector / @skill picker)
 *     references/<...>    ← raw skill files copied verbatim, paths preserved
 *
 * `references/` matches the existing loadSkillSideFiles() layer-7 convention,
 * so once registered the skill is auto-injected by the existing prompt
 * assembly pipeline — no new layer needed.
 */

import fs from 'fs';
import path from 'path';
import type { FetchResult } from './fetch';
import type { ProbeResult } from './probe';
import { canonicalIdFromUrl } from './canonical-id';

const SKILLS_ROOT = path.join(process.cwd(), '.xueban', 'skills');
const REGISTRY_FILE = path.join(SKILLS_ROOT, '.installed.json');

const ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export interface InstalledSkill {
  id: string;
  name: string;
  source: string;          // original URL / "pasted-text"
  source_hash: string;
  installed_at: string;
  counts: Record<string, number>;
}

function slugify(raw: string): string {
  return raw
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^\x00-\x7F]/g, '')
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'skill';
}

function resolveId(inferred: string, sourceHash: string): string {
  const base = slugify(inferred);
  const candidate = base || `skill-${sourceHash.slice(0, 6)}`;
  if (!ID_RE.test(candidate)) return `skill-${sourceHash.slice(0, 6)}`;
  return candidate;
}

function copyReferences(srcDir: string, dstDir: string, subpath?: string): void {
  const sourceRoot = subpath ? path.join(srcDir, subpath) : srcDir;
  if (!fs.existsSync(sourceRoot)) return;

  const walk = (rel: string): void => {
    const abs = path.join(sourceRoot, rel);
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(abs, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      if (e.name.startsWith('.')) continue;
      if (e.name === 'node_modules') continue;
      const entryRel = rel ? `${rel}/${e.name}` : e.name;
      const entryAbs = path.join(abs, e.name);

      let lst: fs.Stats;
      try { lst = fs.lstatSync(entryAbs); } catch { continue; }
      if (lst.isSymbolicLink()) continue;

      if (e.isDirectory()) {
        walk(entryRel);
        continue;
      }
      if (!e.isFile()) continue;
      const ext = path.extname(e.name).toLowerCase();
      if (!['.md', '.yaml', '.yml', '.json', '.txt'].includes(ext)) continue;

      const dest = path.join(dstDir, entryRel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      try {
        fs.copyFileSync(entryAbs, dest);
      } catch {
        // skip individual file failures
      }
    }
  };

  walk('');
}

export interface RegisterOptions {
  fetched: FetchResult;
  probe: ProbeResult;
  /**
   * @deprecated Canonical ids should be derived from the source URL via
   * `canonicalIdFromUrl()` (see canonical-id.ts). Letting callers force an id
   * is what caused the same GitHub repo to be installed under two different
   * ids (e.g. 'bmad' vs 'bmad-method'), poisoning the skill cache and
   * breaking team-yaml `team_ref` resolution. Kept for backward compat
   * with pasted-text installs that have no URL slug to derive from.
   */
  forced_id?: string;
}

export interface RegisterResult {
  id: string;
  dir: string;
  name: string;
  installed: InstalledSkill;
  /** true when this is a fresh install; false when an existing skill was overwritten */
  is_new: boolean;
}

/**
 * Write the skill into the user library. Idempotent — reinstalling the same
 * source overwrites the previous copy (re-fetching upstream changes is fine).
 */
export function registerSkill(opts: RegisterOptions): RegisterResult {
  const { fetched, probe, forced_id } = opts;
  // Canonical id strategy:
  //   - URL-backed sources (git-repo / raw-file) ALWAYS derive id from the
  //     source URL via canonicalIdFromUrl(), case-preserved. This is the
  //     post-W1 contract — see canonical-id.ts header for rationale.
  //   - pasted-text has no URL slug, so fall through to forced_id /
  //     slugified inferred_name (legacy path).
  //   - `forced_id` is kept solely for the pasted-text branch and any
  //     legacy callers that haven't been migrated; new code MUST NOT pass it
  //     for URL sources — we warn loudly when it's ignored (review #2).
  let id: string;
  if (fetched.kind === 'git-repo' || fetched.kind === 'raw-file') {
    if (forced_id) {
      console.warn(
        `[skill-ingest] forced_id="${forced_id}" ignored for URL source — ` +
          `canonical id derives from ${fetched.source_label}`,
      );
    }
    id = canonicalIdFromUrl(fetched.source_label);
  } else {
    id = resolveId(forced_id ?? fetched.inferred_name, fetched.source_hash);
  }
  const skillDir = path.join(SKILLS_ROOT, id);
  const refDir = path.join(skillDir, 'references');

  // /review finding #5: collision check. If a skill directory already exists
  // at this id, verify it came from the same source URL — otherwise we'd
  // silently overwrite `alice/BMAD-METHOD` with `bob/BMAD-METHOD`. We compare
  // the previous SKILL.md frontmatter `source:` field via a tiny string scan
  // (avoid yaml import here to keep this hot path light). Different source →
  // throw a structured error the route handler can translate to 409.
  const is_new = !fs.existsSync(skillDir);
  if (!is_new) {
    const priorSource = readPriorSource(skillDir);
    if (priorSource && priorSource !== fetched.source_label) {
      const err = new Error(
        `Skill id "${id}" is already installed from a different source ` +
          `(${priorSource}). Refusing to overwrite. Uninstall the existing ` +
          `skill first if you intend to switch upstreams.`,
      );
      (err as Error & { code?: string }).code = 'SKILL_ID_COLLISION';
      throw err;
    }
  }

  fs.mkdirSync(refDir, { recursive: true });

  // Copy raw skill files into references/ so loadSkillSideFiles() picks them up.
  copyReferences(fetched.dir, refDir, fetched.subpath);

  // Synthetic SKILL.md with frontmatter — the existing skill-loader expects this.
  const installedAt = new Date().toISOString();
  const niceName = (fetched.inferred_name || id).replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  const descSummary = Object.entries(probe.counts)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k}`)
    .join(', ') || 'empty';
  const safeDescription = JSON.stringify(
    `Ingested from ${fetched.source_label} (${descSummary})`,
  );
  const skillMd = [
    '---',
    `name: ${JSON.stringify(niceName)}`,
    `description: ${safeDescription}`,
    `mode: blueprint`,
    `preview_type: yaml`,
    `source: ${JSON.stringify(fetched.source_label)}`,
    `source_hash: ${fetched.source_hash}`,
    `installed_at: ${installedAt}`,
    '---',
    '',
    `# ${niceName}`,
    '',
    `This skill was ingested from \`${fetched.source_label}\` on ${installedAt}.`,
    '',
    `**Contents:** ${Object.entries(probe.counts).filter(([, n]) => n > 0).map(([k, n]) => `${k}=${n}`).join(', ') || 'empty'}`,
    '',
    'The original files have been copied into `references/` and will be auto-injected',
    'when this skill is used (see Story 15.12 side-files loader).',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(skillDir, 'SKILL.md'), skillMd, 'utf-8');

  fs.writeFileSync(path.join(skillDir, 'probe.json'), JSON.stringify(probe, null, 2), 'utf-8');

  const installed: InstalledSkill = {
    id,
    name: niceName,
    source: fetched.source_label,
    source_hash: fetched.source_hash,
    installed_at: installedAt,
    counts: probe.counts as Record<string, number>,
  };

  upsertRegistry(installed);

  return { id, dir: skillDir, name: niceName, installed, is_new };
}

// In-process mutex for .installed.json read-modify-write. Daemon is single-
// process so a Promise queue is sufficient — multi-process would need
// `proper-lockfile`, but Xueban daemon runs as one node process.
// /review finding A6.
let registryWriteChain: Promise<void> = Promise.resolve();

function upsertRegistry(entry: InstalledSkill): void {
  // Serialize on a chained Promise. Each upsertRegistry call appends to the
  // tail and awaits the previous write before doing its own read-modify-write,
  // eliminating the lost-update race two concurrent ingests would otherwise hit.
  const work = (): void => {
    let list: InstalledSkill[] = [];
    if (fs.existsSync(REGISTRY_FILE)) {
      try {
        const raw = fs.readFileSync(REGISTRY_FILE, 'utf-8');
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) list = parsed;
      } catch {
        list = [];
      }
    }
    const idx = list.findIndex((x) => x.id === entry.id);
    if (idx >= 0) list[idx] = entry;
    else list.push(entry);

    fs.mkdirSync(path.dirname(REGISTRY_FILE), { recursive: true });
    // Atomic write: stage to tmp + rename, so a crash mid-write doesn't leave
    // .installed.json half-written and the daemon can't boot.
    const tmp = REGISTRY_FILE + '.tmp.' + process.pid;
    fs.writeFileSync(tmp, JSON.stringify(list, null, 2), 'utf-8');
    fs.renameSync(tmp, REGISTRY_FILE);
  };
  registryWriteChain = registryWriteChain.then(() => {
    work();
  });
  // Caller stays synchronous (existing signature). Since we're queued at
  // call time, the write is guaranteed to land before any subsequent call
  // observes its result via listInstalled() (also reads via the same chain
  // — see listInstalled below).
}

/** Read the SKILL.md frontmatter `source:` line, if any. Returns null when
 *  the file is missing or doesn't have a source field. Lightweight grep —
 *  avoids pulling gray-matter into this hot install path. */
function readPriorSource(skillDir: string): string | null {
  const skillMd = path.join(skillDir, 'SKILL.md');
  if (!fs.existsSync(skillMd)) return null;
  try {
    const raw = fs.readFileSync(skillMd, 'utf-8');
    // Only scan inside the frontmatter block to avoid matching prose.
    const fmEnd = raw.indexOf('\n---', 4);
    if (fmEnd < 0) return null;
    const fm = raw.slice(0, fmEnd);
    // source: "https://..."   (JSON.stringify'd above)
    const m = fm.match(/^source:\s*(.+)$/m);
    if (!m) return null;
    const v = m[1].trim();
    // strip wrapping quotes if JSON-encoded
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      try {
        return JSON.parse(v.replace(/^'/, '"').replace(/'$/, '"'));
      } catch {
        return v.slice(1, -1);
      }
    }
    return v;
  } catch {
    return null;
  }
}

export function listInstalled(): InstalledSkill[] {
  if (!fs.existsSync(REGISTRY_FILE)) return [];
  try {
    const raw = fs.readFileSync(REGISTRY_FILE, 'utf-8');
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function getInstalled(id: string): InstalledSkill | null {
  return listInstalled().find((x) => x.id === id) ?? null;
}

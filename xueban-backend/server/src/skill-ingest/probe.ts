/**
 * skill-ingest/probe.ts — schemaless inspector for a fetched skill directory.
 *
 * Walks the dir, collects .md / .yaml / .yml / .json files, extracts frontmatter
 * where present, and infers each file's "kind" (agent / task / kb / readme /
 * config / doc) by combining:
 *   - directory hints  (agents/, tasks/, kb/, knowledge/, workflows/)
 *   - filename hints   (*-agent.md, *.task.md, README, MANIFEST, ...)
 *   - frontmatter hints (`agent:`, `role:`, `task:` keys)
 *
 * The classifier is intentionally lenient — BMAD uses one layout, Claude
 * Code skills use another, custom user skills use whatever they want. We
 * label each file with our best guess and let the LLM-side Assembler weigh
 * the evidence holistically.
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';

const ALLOWED_EXT = new Set(['.md', '.yaml', '.yml', '.json', '.txt']);
const MAX_FILE_BYTES = 200 * 1024;       // 200 KB per file
const MAX_TOTAL_BYTES = 2 * 1024 * 1024; // 2 MB total per skill
const MAX_FILES = 200;
const MAX_DEPTH = 8;

export type ProbeKind = 'agent' | 'task' | 'kb' | 'workflow' | 'readme' | 'config' | 'doc';

export interface ProbeFile {
  rel_path: string;
  kind: ProbeKind;
  size: number;
  /** parsed frontmatter (any shape), or null when none */
  frontmatter: Record<string, unknown> | null;
  /** first 400 chars of body content after frontmatter (for LLM summarization) */
  excerpt: string;
}

export interface ProbeResult {
  files: ProbeFile[];
  /** quick counts per kind for the LLM-facing summary block */
  counts: Record<ProbeKind, number>;
  total_bytes: number;
  truncated: boolean;
  /** README or top-level skill.md / SKILL.md / index.md content (whole body), if found */
  intro: string | null;
}

function classify(relPath: string, frontmatter: Record<string, unknown> | null): ProbeKind {
  const lower = relPath.toLowerCase().replace(/\\/g, '/');
  const base = path.basename(lower);
  const segs = lower.split('/');

  // frontmatter signals win — author's explicit declaration
  if (frontmatter) {
    if ('agent' in frontmatter || 'role' in frontmatter) return 'agent';
    if ('task' in frontmatter || 'workflow' in frontmatter) {
      return 'workflow' in frontmatter ? 'workflow' : 'task';
    }
  }

  // directory-based hints
  if (segs.includes('agents') || segs.includes('agent')) return 'agent';
  if (segs.includes('tasks') || segs.includes('task')) return 'task';
  if (segs.includes('workflows') || segs.includes('workflow') || segs.includes('pipelines')) return 'workflow';
  if (segs.includes('kb') || segs.includes('knowledge') || segs.includes('data')) return 'kb';

  // filename-based hints
  if (/^readme(\.|$)/.test(base) || /^skill\.md$/.test(base) || /^index\.md$/.test(base)) return 'readme';
  if (/-agent\.(md|ya?ml)$/.test(base) || /agent\.(md|ya?ml)$/.test(base)) return 'agent';
  if (/\.task\.(md|ya?ml)$/.test(base) || /^task-/.test(base)) return 'task';
  if (/manifest|config|\.json$|\.ya?ml$/.test(base)) return 'config';

  return 'doc';
}

function walk(
  root: string,
  rel: string,
  depth: number,
  acc: { files: { rel: string; abs: string; size: number }[]; total: number; truncated: boolean },
): void {
  if (depth > MAX_DEPTH || acc.truncated) return;
  if (acc.files.length >= MAX_FILES) {
    acc.truncated = true;
    return;
  }

  const abs = path.join(root, rel);
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(abs, { withFileTypes: true });
  } catch {
    return;
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));

  for (const e of entries) {
    if (acc.truncated) return;
    if (e.name.startsWith('.')) continue;          // skip hidden
    if (e.name === 'node_modules') continue;
    const entryRel = rel ? `${rel}/${e.name}` : e.name;
    const entryAbs = path.join(abs, e.name);

    let lst: fs.Stats;
    try { lst = fs.lstatSync(entryAbs); } catch { continue; }
    if (lst.isSymbolicLink()) continue;

    if (e.isDirectory()) {
      walk(root, entryRel, depth + 1, acc);
      continue;
    }
    if (!e.isFile()) continue;

    const ext = path.extname(e.name).toLowerCase();
    if (!ALLOWED_EXT.has(ext)) continue;

    if (lst.size > MAX_FILE_BYTES) continue;
    if (acc.total + lst.size > MAX_TOTAL_BYTES) {
      acc.truncated = true;
      return;
    }
    acc.files.push({ rel: entryRel, abs: entryAbs, size: lst.size });
    acc.total += lst.size;
    if (acc.files.length >= MAX_FILES) {
      acc.truncated = true;
      return;
    }
  }
}

/**
 * Probe a fetched skill directory. Pure read — never modifies the dir.
 *
 * @param root        absolute path returned by fetchSkill().dir
 * @param subpath     optional sub-path inside root to focus on (from github tree URLs)
 */
export function probeSkill(root: string, subpath?: string): ProbeResult {
  const baseDir = subpath ? path.join(root, subpath) : root;

  const counts: Record<ProbeKind, number> = {
    agent: 0, task: 0, kb: 0, workflow: 0, readme: 0, config: 0, doc: 0,
  };

  if (!fs.existsSync(baseDir)) {
    return { files: [], counts, total_bytes: 0, truncated: false, intro: null };
  }

  const acc = { files: [] as { rel: string; abs: string; size: number }[], total: 0, truncated: false };
  walk(baseDir, '', 0, acc);

  const probed: ProbeFile[] = [];
  let intro: string | null = null;

  for (const f of acc.files) {
    let content: string;
    try {
      content = fs.readFileSync(f.abs, 'utf-8');
    } catch {
      continue;
    }

    let frontmatter: Record<string, unknown> | null = null;
    let body = content;
    if (f.rel.endsWith('.md')) {
      try {
        const parsed = matter(content);
        if (parsed.data && Object.keys(parsed.data).length > 0) {
          frontmatter = parsed.data as Record<string, unknown>;
        }
        body = parsed.content;
      } catch {
        // matter() throws on malformed frontmatter — fall through with raw content
      }
    }

    const kind = classify(f.rel, frontmatter);
    counts[kind]++;

    if (kind === 'readme' && intro === null) {
      intro = body.trim().slice(0, 4000);
    }

    probed.push({
      rel_path: f.rel,
      kind,
      size: f.size,
      frontmatter,
      excerpt: body.trim().slice(0, 400),
    });
  }

  return { files: probed, counts, total_bytes: acc.total, truncated: acc.truncated, intro };
}

/**
 * Render a probe result as a markdown block to inject into the LLM system
 * prompt. Compact — meant to fit alongside other prompt layers.
 */
export function renderProbeForPrompt(probe: ProbeResult, skillName: string): string {
  const parts: string[] = [];
  parts.push(`<skill name="${skillName.replace(/"/g, '&quot;')}">`);
  parts.push('');

  if (probe.intro) {
    parts.push('## SKILL INTRO');
    parts.push('');
    parts.push(probe.intro);
    parts.push('');
  }

  const summary = (Object.entries(probe.counts) as [ProbeKind, number][])
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${k}=${n}`)
    .join(', ');
  if (summary) {
    parts.push(`## CONTENTS (${summary})`);
    parts.push('');
  }

  // Group files by kind so the LLM gets a structured view
  const byKind: Record<ProbeKind, ProbeFile[]> = {
    agent: [], task: [], workflow: [], kb: [], config: [], readme: [], doc: [],
  };
  for (const f of probe.files) byKind[f.kind].push(f);

  const ORDER: ProbeKind[] = ['agent', 'task', 'workflow', 'kb', 'config', 'doc'];
  for (const kind of ORDER) {
    const group = byKind[kind];
    if (group.length === 0) continue;
    parts.push(`### ${kind.toUpperCase()}S`);
    parts.push('');
    for (const f of group) {
      const fmKeys = f.frontmatter ? Object.keys(f.frontmatter).slice(0, 6).join(', ') : '';
      const header = fmKeys ? `**${f.rel_path}** (${fmKeys})` : `**${f.rel_path}**`;
      parts.push(header);
      if (f.excerpt) {
        const safe = f.excerpt.replace(/```/g, '` ` `');
        parts.push('```');
        parts.push(safe);
        parts.push('```');
      }
      parts.push('');
    }
  }

  if (probe.truncated) {
    parts.push('_(probe was truncated — only first portion of skill content shown)_');
  }

  parts.push('</skill>');
  return parts.join('\n');
}

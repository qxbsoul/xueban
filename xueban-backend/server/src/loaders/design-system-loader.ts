/**
 * design-system-loader.ts — FS-based Design System loader (Story 15.11)
 *
 * Scans `.xueban/design-systems/*.md` files at startup or on
 * `POST /api/design-systems/reload`, parses their YAML frontmatter via
 * gray-matter, and returns DesignSystem objects ready to merge into the
 * in-memory registry alongside the hardcoded built-ins (15.5).
 *
 * Behaviour contract (AC1-AC6):
 * - Returns `{ loaded, errors }`. NEVER throws fatal.
 * - Empty / missing dir → empty result, no error.
 * - Missing required `ds_id` → skip + warn.
 * - Empty body → skip + warn.
 * - Invalid YAML / unreadable file → skip + warn, other DS unaffected.
 *
 * MVP simplification (per story spec): the 9-section schema is parsed only
 * far enough to detect which sections are present (`detected_sections`).
 * The body is injected as a single string into `injection_prompt`. Per-section
 * dynamic selection is deferred to a future story.
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import type { DesignSystem } from '../design-systems';

const DEFAULT_DS_DIR = path.join(process.cwd(), '.xueban', 'design-systems');

/** The 9 valid section names (lower-case) per OpenDesign DESIGN.md schema. */
const VALID_SECTIONS: ReadonlyArray<string> = [
  'palette',
  'typography',
  'spacing',
  'components',
  'motion',
  'voice',
  'brand',
  'anti-patterns',
  'code examples',
];

export interface FsDesignSystem extends DesignSystem {
  /** Lower-cased section names detected in the body (diagnostic). */
  detected_sections: string[];
  /** Always 'fs' for FS-loaded; built-ins use 'builtin'. */
  source: 'fs';
  /** Relative source path for log/debug output. */
  source_path: string;
}

export interface DesignSystemLoadResult {
  loaded: FsDesignSystem[];
  errors: Array<{ file: string; reason: string }>;
}

/**
 * Scan `.xueban/design-systems/` for `*.md` files and parse them.
 *
 * @param dsDirOverride  test-only: redirect the scan target
 */
export function loadDesignSystemsFromFs(
  dsDirOverride?: string,
): DesignSystemLoadResult {
  const dsDir = dsDirOverride ?? DEFAULT_DS_DIR;
  const result: DesignSystemLoadResult = { loaded: [], errors: [] };

  if (!fs.existsSync(dsDir)) {
    return result;
  }

  let files: string[];
  try {
    files = fs.readdirSync(dsDir).filter((f) => f.endsWith('.md'));
  } catch (err) {
    const msg = `cannot read design-systems dir: ${(err as Error).message}`;
    result.errors.push({ file: '<root>', reason: msg });
    console.warn(`[design-system-loader] ${msg}`);
    return result;
  }

  const seenIds = new Set<string>();

  for (const file of files) {
    const fullPath = path.join(dsDir, file);

    let raw: string;
    try {
      raw = fs.readFileSync(fullPath, 'utf-8');
    } catch (err) {
      const reason = `read failed: ${(err as Error).message}`;
      result.errors.push({ file, reason });
      console.warn(`[design-system-loader] skipped ${file}: ${reason}`);
      continue;
    }

    let parsed: matter.GrayMatterFile<string>;
    try {
      parsed = matter(raw);
    } catch (err) {
      const reason = `invalid frontmatter: ${(err as Error).message}`;
      result.errors.push({ file, reason });
      console.warn(`[design-system-loader] skipped ${file}: ${reason}`);
      continue;
    }

    const fm = (parsed.data ?? {}) as Record<string, unknown>;

    const ds_id =
      typeof fm.ds_id === 'string' && fm.ds_id.trim() ? fm.ds_id.trim() : '';
    if (!ds_id) {
      const reason = 'missing ds_id in frontmatter';
      result.errors.push({ file, reason });
      console.warn(`[design-system-loader] skipped ${file}: ${reason}`);
      continue;
    }

    // 2026-05-11 review P1-3: strict ID charset whitelist (与 15.10 同模式)。
    if (!/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(ds_id)) {
      const reason = `invalid ds_id "${ds_id}" (must match /^[a-z0-9][a-z0-9_-]{0,63}$/i)`;
      result.errors.push({ file, reason });
      console.warn(`[design-system-loader] skipped ${file}: ${reason}`);
      continue;
    }

    if (seenIds.has(ds_id)) {
      const reason = `duplicate ds_id "${ds_id}" (already loaded from another file)`;
      result.errors.push({ file, reason });
      console.warn(`[design-system-loader] skipped ${file}: ${reason}`);
      continue;
    }

    const body = parsed.content.trim();
    if (!body) {
      const reason = 'empty body (no design content)';
      result.errors.push({ file, reason });
      console.warn(`[design-system-loader] skipped ${file}: ${reason}`);
      continue;
    }

    const name = typeof fm.name === 'string' && fm.name.trim() ? fm.name : ds_id;
    const description =
      typeof fm.description === 'string' ? fm.description : '';
    const compatible_skills = Array.isArray(fm.compatible_skills)
      ? (fm.compatible_skills as unknown[]).filter(
          (x): x is string => typeof x === 'string',
        )
      : [];

    const detected_sections = detectSections(body);

    seenIds.add(ds_id);
    result.loaded.push({
      ds_id,
      name,
      description,
      compatible_skills,
      injection_prompt: body,
      detected_sections,
      source: 'fs',
      source_path: path.relative(process.cwd(), fullPath),
    });
  }

  if (result.loaded.length > 0) {
    console.log(
      `[design-system-loader] loaded ${result.loaded.length} design system(s) from ${dsDir}`,
    );
  }

  return result;
}

function detectSections(body: string): string[] {
  const found: string[] = [];
  const lines = body.split(/\r?\n/);
  for (const line of lines) {
    const m = line.match(/^##\s+(.+?)\s*$/);
    if (!m) continue;
    const lower = m[1].toLowerCase().trim();
    if (VALID_SECTIONS.includes(lower) && !found.includes(lower)) {
      found.push(lower);
    }
  }
  return found;
}

/**
 * Seed `.xueban/design-systems/` with the 4 built-in DS files (one .md per
 * DS) so users can edit them on disk. Idempotent: never overwrites an existing
 * file (AC5 — user-edited content must be preserved).
 *
 * @param builtins  built-in design systems (DesignSystem shape from 15.5)
 * @param dsDirOverride  test-only: redirect the seed target
 */
export function seedBuiltinDesignSystems(
  builtins: DesignSystem[],
  dsDirOverride?: string,
): { written: string[]; skipped: string[] } {
  const dsDir = dsDirOverride ?? DEFAULT_DS_DIR;
  const written: string[] = [];
  const skipped: string[] = [];

  try {
    fs.mkdirSync(dsDir, { recursive: true });
  } catch (err) {
    console.warn(
      `[design-system-loader] cannot create seed dir ${dsDir}: ${(err as Error).message}`,
    );
    return { written, skipped };
  }

  for (const b of builtins) {
    const file = path.join(dsDir, `${b.ds_id}.md`);
    if (fs.existsSync(file)) {
      skipped.push(b.ds_id);
      continue;
    }

    const fmLines: string[] = ['---'];
    fmLines.push(`ds_id: ${b.ds_id}`);
    fmLines.push(`name: ${JSON.stringify(b.name)}`);
    fmLines.push(`description: ${JSON.stringify(b.description)}`);
    fmLines.push(
      `compatible_skills: ${JSON.stringify(b.compatible_skills)}`,
    );
    fmLines.push('---');
    fmLines.push('');

    // For 'none' (empty injection_prompt) we still seed a tiny body so AC2's
    // "empty body → skip" rule does not later drop the file on reload. Use a
    // single "## Voice" section explaining the no-op.
    const body =
      b.injection_prompt && b.injection_prompt.trim().length > 0
        ? b.injection_prompt
        : '## Voice\n不附加任何设计指令，按用户输入直接生成。';

    try {
      fs.writeFileSync(file, fmLines.join('\n') + body + '\n', 'utf-8');
      written.push(b.ds_id);
    } catch (err) {
      console.warn(
        `[design-system-loader] could not seed ${file}: ${(err as Error).message}`,
      );
    }
  }

  if (written.length > 0) {
    console.log(
      `[design-system-loader] seeded ${written.length} built-in DS file(s): ${written.join(', ')}`,
    );
  }

  return { written, skipped };
}

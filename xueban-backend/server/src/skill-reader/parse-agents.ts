/**
 * skill-reader/parse-agents.ts — collect verbatim agent files from a skill dir.
 *
 * Two input shapes are supported, picked up in order:
 *
 *   1. **Standard layout**: `<skillDir>/agents/*.md|yaml|yml`. The Claude Code
 *      / paper-review-style skill convention. Each file becomes one
 *      `AgentFile` with raw bytes preserved and `.md` frontmatter parsed.
 *
 *   2. **BMAD-METHOD layout**: `<skillDir>/bmad-modules.yaml` is a registry of
 *      sub-modules (each lives in its own GitHub repo). For PR-A purposes
 *      every entry under `modules.*` is treated as an "agent-shaped" record
 *      so PR-C's compiler has something to chew on. We synthesize an
 *      `agents/<id>.synthesized.md` entry per module with the registry
 *      metadata serialized as YAML frontmatter + a short body.
 *
 *      Note: the spec text mentions `modules.*.agents[]` but the real-world
 *      `bmad-modules.yaml` is a flat module registry without a nested
 *      `agents` array — it lists 6 BMAD sub-modules. We synthesize one
 *      agent_file per module entry, which satisfies the "BMAD returns ≥1
 *      agent_file" acceptance and gives PR-C a concrete handle on the BMAD
 *      structure. (Implementer decision, noted in commit body.)
 *
 *   3. **Missing both**: returns `[]`. No throws — empty skill dirs and
 *      unusual layouts are first-class cases.
 *
 * `frontmatter` follows the convention in `types.ts`: `gray-matter` for `.md`,
 * `null` for `.yaml`/`.yml` (PR-C parses YAML directly when needed). The
 * synthesized BMAD entries are written as `.md` with frontmatter so they
 * round-trip through the same code path.
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import yaml from 'js-yaml';
import type { AgentFile } from './types';

const AGENT_EXTS = new Set(['.md', '.yaml', '.yml']);

/** POSIX-normalize a path so the content hash is stable across Windows / Linux. */
function toPosix(p: string): string {
  return p.replace(/\\/g, '/');
}

// 2026-06-01 — real-agent discovery. The skill-reader's job is to surface the
// agents a skill ACTUALLY ships, verbatim — never to invent them. Real skills
// don't always keep agents in a flat top-level `agents/` dir; BMAD-METHOD, for
// example, puts each real persona at `src/bmm-skills/<phase>/bmad-agent-*/SKILL.md`.
// We walk the tree (bounded) and collect a file as an agent when it is EITHER:
//   (a) inside a directory literally named `agents/` (at any depth), or
//   (b) a `SKILL.md` (or SKILL.yaml) inside an `*agent*`-named dir (e.g.
//       `bmad-agent-analyst/`).
// Heavy / irrelevant subtrees are pruned and depth is capped so a big repo
// checkout (BMAD ships its whole source tree) stays fast.

const SKIP_DIRS = new Set([
  '.git', 'node_modules', 'dist', 'build', '.cache', 'coverage',
  'evals', 'website', 'tools', 'test', 'tests', '__tests__',
  '.next', 'out', 'vendor', 'docs',
]);
const MAX_WALK_DEPTH = 8;
const MAX_NESTED_AGENTS = 50;
/** Matches dir names like `bmad-agent-analyst`, `agent-foo`, `my_agent`. */
const AGENT_DIR_RE = /(^|[-_])agent([-_]|$)/i;
const AGENT_DIR_SKILL_RE = /^skill\.(md|ya?ml)$/i;

/** Read one on-disk agent file into an AgentFile (verbatim raw + parsed fm). */
function readAgentFile(absFile: string, relPath: string, ext: string): AgentFile | null {
  let raw: string;
  try {
    raw = fs.readFileSync(absFile, 'utf-8');
  } catch {
    return null;
  }
  let frontmatter: Record<string, unknown> | null = null;
  if (ext === '.md') {
    try {
      const parsed = matter(raw);
      if (parsed.data && Object.keys(parsed.data).length > 0) {
        frontmatter = parsed.data as Record<string, unknown>;
      }
    } catch {
      // malformed frontmatter → keep raw, drop frontmatter
    }
  }
  return { path: toPosix(relPath), raw, frontmatter };
}

/**
 * Recursively discover real agent files. Handles the flat agents-dir layout
 * (top level or nested) AND the BMAD-style nested `<agent-named-dir>/SKILL.md`
 * layout. Verbatim — never synthesizes. Returns `[]` when none are found.
 */
function discoverAgents(skillDir: string): AgentFile[] {
  const out: AgentFile[] = [];
  const seen = new Set<string>();

  function visit(absDir: string, relDir: string, depth: number): void {
    if (depth > MAX_WALK_DEPTH || out.length >= MAX_NESTED_AGENTS) return;
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(absDir, { withFileTypes: true });
    } catch {
      return;
    }
    // Deterministic order so the content hash is stable across platforms.
    entries.sort((a, b) => a.name.localeCompare(b.name));

    const dirBase = path.basename(absDir).toLowerCase();
    const isAgentsDir = dirBase === 'agents';
    const isAgentNamedDir = AGENT_DIR_RE.test(dirBase);

    for (const e of entries) {
      if (e.name.startsWith('.')) continue;
      const rel = relDir ? `${relDir}/${e.name}` : e.name;
      if (e.isDirectory()) {
        if (SKIP_DIRS.has(e.name.toLowerCase())) continue;
        visit(path.join(absDir, e.name), rel, depth + 1);
        continue;
      }
      if (!e.isFile()) continue;
      const ext = path.extname(e.name).toLowerCase();
      if (!AGENT_EXTS.has(ext)) continue;

      // (a) any file directly inside an `agents/` dir (depth ≥ 1 so the skill
      //     root itself is never mistaken for an agent), or
      // (b) a SKILL.md inside an `*agent*`-named dir (one persona per dir).
      const underAgentsDir = isAgentsDir && depth >= 1;
      const agentDirSkill = isAgentNamedDir && AGENT_DIR_SKILL_RE.test(e.name);
      if (!underAgentsDir && !agentDirSkill) continue;
      if (seen.has(rel)) continue;

      const af = readAgentFile(path.join(absDir, e.name), rel, ext);
      if (af) {
        out.push(af);
        seen.add(rel);
        if (out.length >= MAX_NESTED_AGENTS) return;
      }
    }
  }

  if (!fs.existsSync(skillDir)) return [];
  visit(skillDir, '', 0);
  return out;
}

/**
 * If `<skillDir>/bmad-modules.yaml` exists, synthesize one AgentFile per
 * module entry. Each synthetic file mirrors the standard `agents/*.md` shape
 * (frontmatter + body) so PR-C's compiler can treat them uniformly.
 *
 * The synthesized `raw` content is deterministic for a given module entry —
 * a regenerated readSkill on the same skillDir must produce a byte-equal
 * synthesized file so the content_hash stays stable. We rely on `js-yaml`'s
 * default block style with sortKeys=false (the spec says insertion order).
 */
function synthesizeBmadModules(skillDir: string): AgentFile[] {
  const file = path.join(skillDir, 'bmad-modules.yaml');
  if (!fs.existsSync(file)) return [];

  let raw: string;
  try {
    raw = fs.readFileSync(file, 'utf-8');
  } catch {
    return [];
  }

  let doc: unknown;
  try {
    doc = yaml.load(raw);
  } catch {
    return [];
  }

  if (!doc || typeof doc !== 'object') return [];
  const modules = (doc as { modules?: Record<string, unknown> }).modules;
  if (!modules || typeof modules !== 'object') return [];

  const out: AgentFile[] = [];
  // Sort module ids for deterministic emission — critical for hash stability.
  const moduleIds = Object.keys(modules).sort();
  for (const id of moduleIds) {
    const entry = (modules as Record<string, unknown>)[id];
    if (!entry || typeof entry !== 'object') continue;
    const meta = entry as Record<string, unknown>;

    // Frontmatter mirrors the YAML entry verbatim so PR-C sees the same data.
    const frontmatter: Record<string, unknown> = {
      id,
      derived_from: 'bmad-modules.yaml',
      ...meta,
    };

    // Body is a short prose summary the LLM can latch onto.
    const fmYaml = yaml.dump(frontmatter, { sortKeys: false, lineWidth: 1000 }).trimEnd();
    const description = typeof meta.description === 'string' ? meta.description : '';
    const name = typeof meta.name === 'string' ? meta.name : id;
    const body =
      `# ${name}\n\n` +
      `${description}\n\n` +
      `Synthesized from \`bmad-modules.yaml\` registry entry \`${id}\`. ` +
      `This module lives in a separate repository; the registry metadata is ` +
      `mirrored here so the SkillCompiler can treat each BMAD module as one ` +
      `"agent-shaped" input without resolving the upstream repo.\n`;

    const synthesizedRaw = `---\n${fmYaml}\n---\n\n${body}`;
    out.push({
      path: toPosix(`agents/${id}.synthesized.md`),
      raw: synthesizedRaw,
      frontmatter,
    });
  }

  return out;
}

/**
 * Public entry: collect all agent-shaped files from a skill directory.
 *
 * Order: standard `agents/*` first, then synthesized BMAD entries. Each
 * sub-array is internally sorted; the merged output is re-sorted at the call
 * site (`readSkill` → `computeContentHash` sorts again) so the order here is
 * informational only.
 */
export async function parseAgents(skillDir: string): Promise<AgentFile[]> {
  // Principle (2026-06-01): a skill's agents are whatever the skill SHIPS —
  // read them verbatim, never invent. Discover real agent files first (flat
  // `agents/*` + nested `**/<*agent*>/SKILL.md`).
  const real = discoverAgents(skillDir);
  if (real.length > 0) {
    real.sort((a, b) => a.path.localeCompare(b.path));
    return real;
  }

  // LAST RESORT ONLY — when the skill ships ZERO real agent files. The
  // `bmad-modules.yaml` entries are an INSTALL-MODULE registry (separate repos),
  // NOT agent roles; synthesizing "agents" from them is a degraded heuristic and
  // is logged loudly so we know we're not reading real structure. Previously
  // this ran unconditionally and SHADOWED real nested agents (e.g. BMAD's
  // bmad-agent-* personas), producing garbage teams of module names.
  const synthesized = synthesizeBmadModules(skillDir);
  if (synthesized.length > 0) {
    console.warn(
      `[skill-reader] ${skillDir}: no real agent files found — fell back to ` +
        `${synthesized.length} bmad-modules.yaml module entries (install modules, NOT agent roles).`,
    );
  }
  return synthesized;
}

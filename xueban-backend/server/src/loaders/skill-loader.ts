/**
 * skill-loader.ts — FS-based Skill loader (Story 15.10)
 *
 * Scans `.xueban/skills/<id>/SKILL.md` files at startup or on
 * `POST /api/skills/reload`, parses their YAML frontmatter via gray-matter,
 * and produces SkillDefinition objects that merge into the in-memory
 * registry alongside the hardcoded built-ins (15.1 / 15.2).
 *
 * Behaviour contract (AC1-AC6):
 * - Returns `{ loaded, errors, overrides }`. NEVER throws fatal.
 * - Empty / missing dir → empty result, no error.
 * - Missing required frontmatter (`name` / `description`) → skip + warn.
 * - Invalid YAML / unreadable file → skip + warn, other skills unaffected.
 * - `overrides` lists ids that exist in BOTH FS and the hardcoded baseline,
 *   so the caller can log "override hardcoded skill: <id>" (AC3).
 */

import fs from 'fs';
import path from 'path';
import matter from 'gray-matter';
import type { SkillDefinition, SkillMode, PreviewType, SkillSource, SkillKind } from '../skills';
import { inferKind } from '../skills';
import { loadTeam as loadLegacyTeam } from '../lib/skill-yaml';
import { loadTeam as loadGlobalTeam } from '../lib/team-yaml';
// Round 4 PR-C: warm the compile cache in the background for any installed
// skill that doesn't already have one. Intentionally fire-and-forget so
// boot is not blocked on (potentially slow) LLM compile calls.
import { tryReadSkill } from '../skill-reader';
import {
  compile as compileSkill,
  getCompiledSkill,
} from '../lib/skill-compiler';
// Round 4 PR-E — surface "compiling..." state to the `compile-status`
// endpoint so the SkillDropdown can render a spinner while the warmer
// runs. In-memory only; pure UX-affordance.
import {
  markCompiling,
  markCompileDone,
} from '../routes/skills-preview-triage';

const DEFAULT_SKILLS_DIR = path.join(process.cwd(), '.xueban', 'skills');
// S6.4 — when the Node server is launched from the `server/` subdir
// (`npm run dev:server`), `process.cwd()` resolves to that subdir, so
// `.xueban/skills` lands at `server/.xueban/skills`. The
// source-tracked demo bundles live at the project root's `.xueban/skills`
// instead. We scan both locations and merge — local (server-rooted) entries
// override root entries when both exist with the same id, so the existing
// runtime-state convention (editor-export-demo etc.) keeps working.
const ROOT_SKILLS_DIR = path.join(process.cwd(), '..', '.xueban', 'skills');

// 2026-06-03 — file-form built-in skills shipped IN the repo (git-tracked),
// the Xueban analogue of OpenDesign's `skills/` built-in root. Resolved
// relative to __dirname (NOT process.cwd()) so it works regardless of which
// dir the server is launched from, and in both layouts:
//   tsx:  server/src/loaders → ../../skills = server/skills
//   dist: server/dist/loaders → ../../skills = server/skills
// Scanned with source='builtin'; user roots are scanned AFTER it so a user
// skill of the same id shadows the built-in (loaded[id] is last-write-wins).
const BUILTIN_SKILLS_DIR = path.join(__dirname, '..', '..', 'skills');

export interface SkillLoadResult {
  loaded: Record<string, SkillDefinition>;
  errors: Array<{ id: string; message: string }>;
  overrides: string[];
}

const REQUIRED_FIELDS = ['name', 'description'] as const;

// 2026-05-11 review P1-1 (OpenDesign 模式): 严格 ID 字符集白名单。
// 防止控制字符 / unicode RTL / `\n` 等通过目录名传到 API JSON / 日志。
// kernel 不会产生字面 `..` 名，但 symlink/手动创建可绕过。
const VALID_SKILL_ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

// 2026-05-11 review P1-2: cap entries.length 防 fs.readdirSync 万级目录 DoS。
const MAX_ENTRIES = 200;

const VALID_MODES: ReadonlySet<SkillMode> = new Set(['blueprint', 'prototype', 'report']);
const VALID_PREVIEW_TYPES: ReadonlySet<PreviewType> = new Set(['yaml', 'html', 'markdown']);
// 2026-06-03 — 分类主轴白名单（docs §10.3）。
const VALID_KINDS: ReadonlySet<SkillKind> = new Set(['workflow', 'capability', 'generator']);

/**
 * 2026-06-03 — domain 归一（对齐 OpenDesign `normalizeCategory`，apps/daemon/src/skills.ts）。
 * lowercase + slug 白名单（`[a-z0-9][a-z0-9-]*`）+ 截断 64；空/非法 → undefined。
 * 防止 `Design` vs `design` 产生两个重复 filter pill，防止自由串/换行/超长污染
 * API JSON、DomainBadge 渲染、domainPills 的 value(state key)。
 */
function normalizeDomain(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const s = raw.trim().toLowerCase().slice(0, 64);
  if (!s) return undefined;
  return /^[a-z0-9][a-z0-9-]*$/.test(s) ? s : undefined;
}

/**
 * Scan `.xueban/skills/` for SKILL.md files and parse them.
 *
 * Two-dir scan strategy (S6.4): we scan the project root's
 * `.xueban/skills/` first (where the source-tracked demo bundles live)
 * and then the server-local `.xueban/skills/` (runtime user content +
 * legacy test skills), merging the results. Local entries win on id
 * collision so the long-standing `editor-export-demo` etc. keep their
 * existing behaviour.
 *
 * @param hardcodedIds  ids of built-in skills (used to flag overrides)
 * @param skillsDirOverride  test-only: redirect the scan target. When set,
 *                           only this single dir is scanned (no merging).
 */
export function loadFsSkills(
  hardcodedIds: ReadonlyArray<string> = [],
  skillsDirOverride?: string,
): SkillLoadResult {
  const loaded: Record<string, SkillDefinition> = {};
  const errors: Array<{ id: string; message: string }> = [];
  const overrides: string[] = [];

  // Ordered scan roots, each tagged with the `source` to stamp on the skills
  // it surfaces. Built-in root is scanned FIRST so the user roots (scanned
  // after) shadow it on id collision — `loaded[id] = skill` is last-write-wins
  // (see scanOneDir), so later roots override earlier ones.
  const candidates: Array<{ dir: string; source: SkillSource }> = skillsDirOverride
    ? [{ dir: skillsDirOverride, source: 'user' }]
    : [
        { dir: BUILTIN_SKILLS_DIR, source: 'builtin' },
        { dir: ROOT_SKILLS_DIR, source: 'user' },
        { dir: DEFAULT_SKILLS_DIR, source: 'user' },
      ];
  const roots = candidates.filter((r) => fs.existsSync(r.dir));

  if (roots.length === 0) {
    return { loaded, errors, overrides };
  }
  // Inline the single-dir scan body via a labelled loop so we can re-run
  // for each candidate dir while keeping the existing per-file error /
  // override book-keeping logic untouched.
  for (const { dir, source } of roots) {
    scanOneDir(dir, source, hardcodedIds, loaded, errors, overrides);
  }
  return { loaded, errors, overrides };
}

function scanOneDir(
  skillsDir: string,
  source: SkillSource,
  hardcodedIds: ReadonlyArray<string>,
  loaded: Record<string, SkillDefinition>,
  errors: Array<{ id: string; message: string }>,
  overrides: string[],
): void {
  if (!fs.existsSync(skillsDir)) return;

  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(skillsDir, { withFileTypes: true });
  } catch (err) {
    const msg = `cannot read skills dir: ${(err as Error).message}`;
    errors.push({ id: '<root>', message: msg });
    console.warn(`[skill-loader] ${msg}`);
    return;
  }

  // P1-2: cap iterations even if directory has 10k+ entries.
  if (entries.length > MAX_ENTRIES) {
    console.warn(
      `[skill-loader] ${entries.length} entries in ${skillsDir} — capped at ${MAX_ENTRIES}`,
    );
    entries = entries.slice(0, MAX_ENTRIES);
  }

  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const id = entry.name;

    // Skip dotted/hidden entries (e.g. .gitkeep, .DS_Store dirs)
    if (id.startsWith('.')) continue;

    // P1-1: reject any id that doesn't match the strict whitelist (OpenDesign 模式)
    if (!VALID_SKILL_ID_RE.test(id)) {
      const msg = `invalid id "${id}" (must match ${VALID_SKILL_ID_RE})`;
      errors.push({ id, message: msg });
      console.warn(`[skill-loader] skip ${id}: ${msg}`);
      continue;
    }

    const skillMdPath = path.join(skillsDir, id, 'SKILL.md');
    if (!fs.existsSync(skillMdPath)) continue;

    let raw: string;
    try {
      raw = fs.readFileSync(skillMdPath, 'utf-8');
    } catch (err) {
      const msg = `read failed: ${(err as Error).message}`;
      errors.push({ id, message: msg });
      console.warn(`[skill-loader] skip ${id}: ${msg}`);
      continue;
    }

    if (!raw.trim()) {
      errors.push({ id, message: 'empty SKILL.md' });
      console.warn(`[skill-loader] skip ${id}: empty SKILL.md`);
      continue;
    }

    let parsed: matter.GrayMatterFile<string>;
    try {
      parsed = matter(raw);
    } catch (err) {
      const msg = `invalid frontmatter: ${(err as Error).message}`;
      errors.push({ id, message: msg });
      console.warn(`[skill-loader] skip ${id}: ${msg}`);
      continue;
    }

    const fm = (parsed.data ?? {}) as Record<string, unknown>;

    const missing = REQUIRED_FIELDS.find(
      (f) => typeof fm[f] !== 'string' || !(fm[f] as string).trim(),
    );
    if (missing) {
      const msg = `missing required frontmatter field ${missing}`;
      errors.push({ id, message: msg });
      console.warn(`[skill-loader] skip ${id}: ${msg}`);
      continue;
    }

    // Optional skill_id self-check: if author wrote `skill_id`, it must match dir
    if (typeof fm.skill_id === 'string' && fm.skill_id.trim() && fm.skill_id !== id) {
      const msg = `skill_id "${fm.skill_id}" does not match directory name "${id}"`;
      errors.push({ id, message: msg });
      console.warn(`[skill-loader] skip ${id}: ${msg}`);
      continue;
    }

    // Validate enum-shaped optional fields, fall back to defaults if invalid
    const rawMode = typeof fm.mode === 'string' ? (fm.mode.trim() as SkillMode) : undefined;
    const mode: SkillMode = rawMode && VALID_MODES.has(rawMode) ? rawMode : 'prototype';

    const rawPreview =
      typeof fm.preview_type === 'string'
        ? (fm.preview_type.trim() as PreviewType)
        : undefined;
    const preview_type: PreviewType =
      rawPreview && VALID_PREVIEW_TYPES.has(rawPreview) ? rawPreview : 'html';

    // Story 15.19 v2 — optional `executor` selector. We accept any string and
    // let the dispatcher validate; this keeps registry growth data-driven.
    const executor =
      typeof fm.executor === 'string' && fm.executor.trim().length > 0
        ? fm.executor.trim()
        : undefined;

    // S6 (skill-team-conversion-design-v1.md §5 line 875) — parse
    // `allowed-tools: [list_team_agents, ...]` frontmatter into string[].
    // Skip anything non-string (gray-matter parses YAML into JS values, so
    // a quoted entry comes through as string; bare numbers / objects are
    // silently dropped). Case is preserved verbatim — matching policy is
    // exact (see PermissionPolicy JSDoc).
    const rawAllowed = (fm as Record<string, unknown>)['allowed-tools'];
    const allowed_tools = Array.isArray(rawAllowed)
      ? rawAllowed.filter((t): t is string => typeof t === 'string' && t.length > 0)
      : undefined;

    // 2026-06-03 — 分类（docs §10.3）。kind 取 frontmatter（白名单校验），缺省由 mode
    // 推断；domain 是自由字符串（领域/功能细分），缺省 undefined。
    const rawKind = typeof fm.kind === 'string' ? (fm.kind.trim() as SkillKind) : undefined;
    const kind: SkillKind = rawKind && VALID_KINDS.has(rawKind) ? rawKind : inferKind(mode);
    const domain = normalizeDomain(fm.domain);

    // 2026-08-15 — optional cover image for gallery/template cards. Accepts
    // `cover_url` (preferred) and the `cover` alias; empty/absent → undefined
    // so the key is omitted from the API JSON (no fabricated default).
    const rawCover = fm.cover_url ?? fm.cover;
    const cover_url =
      typeof rawCover === 'string' && rawCover.trim().length > 0
        ? rawCover.trim()
        : undefined;

    const skill: SkillDefinition = {
      name: String(fm.name).trim(),
      description: String(fm.description).trim(),
      mode,
      preview_type,
      source,
      kind,
      domain,
      platform: typeof fm.platform === 'string' ? fm.platform : 'web',
      scenario: typeof fm.scenario === 'string' ? fm.scenario : '',
      fidelity: typeof fm.fidelity === 'string' ? fm.fidelity : 'high',
      example_prompt: typeof fm.example_prompt === 'string' ? fm.example_prompt : '',
      cover_url,
      system_prompt: parsed.content ?? '',
      executor,
      allowed_tools,
    };

    // S6.0 + S0.5 — attach the structured team. Two paths:
    //   1. NEW (S0.5): SKILL.md frontmatter has `team_ref: <team-id>` →
    //      resolve via `.xueban/teams/<team-id>.team.yaml` (global lib)
    //   2. LEGACY (S6.0): skill dir has `team.skill.yaml` → resolve locally
    //
    // Path 1 wins when both are present. Per-skill load failures are non-fatal:
    // the skill itself stays loaded so older system_prompt flows still work.
    try {
      const teamRef = typeof fm.team_ref === 'string' ? fm.team_ref.trim() : '';
      if (teamRef) {
        // S0.5 path
        const result = loadGlobalTeam(teamRef);
        if (result.team) {
          // Convert TeamDefV1 → legacy TeamDef shape expected by SkillDefinition.team
          skill.team = {
            name: result.team.name,
            mode: result.team.mode,
            policy: result.team.policy,
            retry: result.team.retry,
            agents: result.resolvedAgents,
            edges: result.team.edges_v1.map(e => ({ from: e.from, to: e.to })),
            loaded_at: result.team.loaded_at,
            source_dir: result.team.source_dir,
          };
        }
        for (const e of result.errors) {
          errors.push({ id, message: `team_ref(${teamRef}): ${e}` });
          console.warn(`[skill-loader] ${id} team_ref(${teamRef}): ${e}`);
        }
      } else {
        // Legacy path
        const teamResult = loadLegacyTeam(path.join(skillsDir, id));
        if (teamResult.team) {
          skill.team = teamResult.team;
        }
        for (const e of teamResult.errors) {
          errors.push({ id, message: `team.skill.yaml: ${e}` });
          console.warn(`[skill-loader] ${id} team load: ${e}`);
        }
      }
    } catch (err) {
      console.warn(`[skill-loader] ${id} team load threw: ${(err as Error).message}`);
    }

    if (hardcodedIds.includes(id)) {
      overrides.push(id);
    }
    loaded[id] = skill;

    // W2 (Lane B) — mirror Claude Code v2.1.88 plugin `/<id>:<cmd>` syntax.
    // If the skill bundle has a `commands/` subdir, register each
    // `commands/<X>.md` as a sibling skill keyed `<id>:<X>` so the run-sessions
    // dispatcher can look it up via the same SKILLS registry. These entries
    // are intentionally simple (mode=prototype, no team, no executor) —
    // matching the Claude Code plugin command model where the .md body is
    // the prompt and frontmatter declares allowed-tools.
    const cmdsDir = path.join(skillsDir, id, 'commands');
    if (fs.existsSync(cmdsDir)) {
      let cmdEntries: fs.Dirent[];
      try {
        cmdEntries = fs.readdirSync(cmdsDir, { withFileTypes: true });
      } catch (err) {
        errors.push({
          id,
          message: `cannot read commands dir: ${(err as Error).message}`,
        });
        cmdEntries = [];
      }
      if (cmdEntries.length > MAX_ENTRIES) {
        console.warn(
          `[skill-loader] ${cmdEntries.length} commands in ${cmdsDir} — capped at ${MAX_ENTRIES}`,
        );
        cmdEntries = cmdEntries.slice(0, MAX_ENTRIES);
      }
      for (const cmdEntry of cmdEntries) {
        if (!cmdEntry.isFile()) continue;
        if (!cmdEntry.name.endsWith('.md')) continue;
        const cmdName = cmdEntry.name.slice(0, -3); // strip .md
        if (!VALID_SKILL_ID_RE.test(cmdName)) {
          errors.push({
            id: `${id}:${cmdName}`,
            message: `invalid command name "${cmdName}" (must match ${VALID_SKILL_ID_RE})`,
          });
          continue;
        }
        const cmdKey = `${id}:${cmdName}`;
        const cmdPath = path.join(cmdsDir, cmdEntry.name);
        let cmdRaw: string;
        try {
          cmdRaw = fs.readFileSync(cmdPath, 'utf-8');
        } catch (err) {
          errors.push({
            id: cmdKey,
            message: `read failed: ${(err as Error).message}`,
          });
          continue;
        }
        if (!cmdRaw.trim()) {
          errors.push({ id: cmdKey, message: 'empty command .md' });
          continue;
        }
        let cmdParsed: matter.GrayMatterFile<string>;
        try {
          cmdParsed = matter(cmdRaw);
        } catch (err) {
          errors.push({
            id: cmdKey,
            message: `invalid frontmatter: ${(err as Error).message}`,
          });
          continue;
        }
        const cmdFm = (cmdParsed.data ?? {}) as Record<string, unknown>;

        // description: prefer frontmatter `description`, else fall back to the
        // first non-blank line of the body. Claude Code requires description
        // in plugin commands; we soften it to never-throw to keep parity with
        // SKILL.md scanning behaviour (skip + warn).
        let cmdDescription =
          typeof cmdFm.description === 'string' && cmdFm.description.trim()
            ? cmdFm.description.trim()
            : '';
        if (!cmdDescription) {
          const firstLine = (cmdParsed.content ?? '')
            .split('\n')
            .map(l => l.trim())
            .find(l => l.length > 0);
          if (firstLine) cmdDescription = firstLine.slice(0, 200);
        }
        if (!cmdDescription) {
          errors.push({
            id: cmdKey,
            message: 'missing description (frontmatter or body)',
          });
          continue;
        }

        // allowed-tools: Claude Code uses HYPHEN ("allowed-tools"); we also
        // accept "allowed_tools" for parity with our own SKILL.md convention.
        // Two shapes in the wild:
        //   - YAML list:  allowed-tools: [Bash, Read]
        //   - CSV string: allowed-tools: Bash(gh issue view:*), Bash(gh pr view:*)
        // Both produce a string[] of trimmed non-empty entries.
        const rawCmdAllowed =
          (cmdFm as Record<string, unknown>)['allowed-tools'] ??
          (cmdFm as Record<string, unknown>)['allowed_tools'];
        let cmdAllowedTools: string[] | undefined;
        if (Array.isArray(rawCmdAllowed)) {
          cmdAllowedTools = rawCmdAllowed
            .filter((t): t is string => typeof t === 'string' && t.trim().length > 0)
            .map(t => t.trim());
        } else if (typeof rawCmdAllowed === 'string' && rawCmdAllowed.trim()) {
          cmdAllowedTools = rawCmdAllowed
            .split(',')
            .map(t => t.trim())
            .filter(t => t.length > 0);
        }

        // disable-model-invocation: Claude Code hyphen form; accept underscore
        // for parity. Coerce truthy YAML scalars (true / "true") to boolean.
        const rawDisable =
          (cmdFm as Record<string, unknown>)['disable-model-invocation'] ??
          (cmdFm as Record<string, unknown>)['disable_model_invocation'];
        const disableModelInvocation =
          rawDisable === true ||
          (typeof rawDisable === 'string' && rawDisable.trim().toLowerCase() === 'true');

        const cmdSkill: SkillDefinition = {
          name: cmdKey,
          description: cmdDescription,
          mode: 'prototype',
          preview_type: 'html',
          source,
          // 命令子 skill（/<id>:<cmd>）是 agent 装配的原子命令 → 归 capability。
          kind: 'capability',
          domain: normalizeDomain(cmdFm.domain),
          platform: typeof cmdFm.platform === 'string' ? cmdFm.platform : 'web',
          scenario: typeof cmdFm.scenario === 'string' ? cmdFm.scenario : '',
          fidelity: typeof cmdFm.fidelity === 'string' ? cmdFm.fidelity : 'high',
          example_prompt:
            typeof cmdFm.example_prompt === 'string' ? cmdFm.example_prompt : '',
          system_prompt: cmdParsed.content ?? '',
          allowed_tools: cmdAllowedTools,
          disable_model_invocation: disableModelInvocation || undefined,
          // executor + team intentionally undefined: command-skills are simple
          // prompt-only entries; dispatcher falls back to default executor.
        };

        if (hardcodedIds.includes(cmdKey)) {
          overrides.push(cmdKey);
        }
        loaded[cmdKey] = cmdSkill;
      }
    }
  }

  if (Object.keys(loaded).length > 0) {
    console.log(
      `[skill-loader] loaded ${Object.keys(loaded).length} skill(s) from ${skillsDir}`,
    );
  }
  for (const id of overrides) {
    console.log(`[skill-loader] override hardcoded skill: ${id}`);
  }
}

// ─── PR-C: background compile cache warmer ───────────────────────────────────

/**
 * For every skill in `loaded`, check whether the compile cache already has
 * an entry for it (via `getCompiledSkill`). If not, fire a fire-and-forget
 * compile() pass in the background. Boot is never blocked.
 *
 * Called from `reloadSkills()` after `loadFsSkills()` returns. Intentionally
 * not awaited — the assembler also runs an on-demand compile when it sees
 * a cache miss at run time, so this is just a latency-optimisation pre-warm.
 *
 * Skills without `.xueban/skills/<id>/references/` (built-in skills,
 * command-skills) are skipped — they ship with system_prompt in the
 * SkillDefinition and don't need a separate compile pass.
 */
export function warmCompileCache(
  loaded: Record<string, SkillDefinition>,
): void {
  for (const skillId of Object.keys(loaded)) {
    if (skillId.includes(':')) continue; // skip W2 command-skills
    void warmOneSkill(skillId);
  }
}

async function warmOneSkill(skillId: string): Promise<void> {
  try {
    const existing = await getCompiledSkill(skillId);
    if (existing) return;
  } catch {
    /* fall through to compile attempt */
  }

  const candidates = [
    path.join(process.cwd(), '.xueban', 'skills', skillId, 'references'),
    path.join(process.cwd(), '..', '.xueban', 'skills', skillId, 'references'),
  ];
  const refDir = candidates.find((d) => fs.existsSync(d));
  if (!refDir) return; // no on-disk references — caller (assembler) skips compile

  try {
    const skillRead = await tryReadSkill(refDir, { skill_id: skillId });
    if (!skillRead) return;
    console.log(`[skill-loader] background compile starting for ${skillId}`);
    // PR-E — let `GET /api/skills/:id/compile-status` show "compiling..."
    // while we're inside the LLM round-trip. Always paired with the
    // markCompileDone in the finally below so a thrown compile never
    // leaves the dropdown stuck on the spinner.
    markCompiling(skillId);
    try {
      await compileSkill(skillRead);
    } finally {
      markCompileDone(skillId);
    }
    console.log(`[skill-loader] background compile finished for ${skillId}`);
  } catch (err) {
    console.warn(
      `[skill-loader] background compile failed for ${skillId}: ${(err as Error).message ?? err}`,
    );
  }
}

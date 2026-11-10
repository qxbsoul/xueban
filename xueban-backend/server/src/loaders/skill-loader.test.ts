/**
 * skill-loader.test.ts — standalone smoke tests for loadFsSkills (Story 15.10)
 *
 * Run with:  npx tsx src/loaders/skill-loader.test.ts   (from server/)
 *
 * Pattern mirrors parser.test.ts — no jest/vitest dependency. Each `check`
 * prints PASS/FAIL; the process exits non-zero if any check fails.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import { loadFsSkills } from './skill-loader';
import { HARDCODED_SKILLS, reloadSkills, SKILLS } from '../skills';

let passCount = 0;
let failCount = 0;

function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    passCount++;
    console.log(`  PASS  ${label}`);
  } else {
    failCount++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('        detail:', detail);
  }
}

/** Make a throwaway `<tmp>/skills/` root and return its absolute path. */
function makeTmpSkillsRoot(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-skill-loader-'));
  return root;
}

function writeSkill(root: string, id: string, body: string) {
  const dir = path.join(root, id);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, 'SKILL.md'), body, 'utf-8');
}

function rmrf(p: string) {
  try {
    fs.rmSync(p, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}

// ─── Test 1: missing dir → empty result, no throw (AC1 fallback) ────────────

(function testMissingDir() {
  console.log('\n[1] missing skills dir → empty result');
  const ghostDir = path.join(os.tmpdir(), `sf-skill-loader-ghost-${Date.now()}`);
  const result = loadFsSkills(['agent-team-blueprint'], ghostDir);
  check('loaded is empty', Object.keys(result.loaded).length === 0);
  check('errors is empty', result.errors.length === 0);
  check('overrides is empty', result.overrides.length === 0);
})();

// ─── Test 2: parse a valid SKILL.md (AC1, AC2 happy path) ───────────────────

(function testValidSkill() {
  console.log('\n[2] parse a valid SKILL.md');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'my-prd',
      `---
name: PRD 生成器
description: 根据需求生成 PRD
mode: report
preview_type: markdown
platform: docs
scenario: business
fidelity: high
example_prompt: 帮我写一个 SaaS 工具的 PRD
---
你是 PRD 写作专家。
按章节输出。
`,
    );
    const result = loadFsSkills([], root);
    check('1 skill loaded', Object.keys(result.loaded).length === 1);
    check('errors empty', result.errors.length === 0);
    const s = result.loaded['my-prd'];
    check('skill exists at id', !!s);
    check('name parsed', s?.name === 'PRD 生成器');
    check('description parsed', s?.description === '根据需求生成 PRD');
    check('mode = report', s?.mode === 'report');
    check('preview_type = markdown', s?.preview_type === 'markdown');
    check('platform = docs', s?.platform === 'docs');
    check('scenario = business', s?.scenario === 'business');
    check('fidelity = high', s?.fidelity === 'high');
    check(
      'example_prompt set',
      s?.example_prompt === '帮我写一个 SaaS 工具的 PRD',
    );
    check(
      'system_prompt = body',
      typeof s?.system_prompt === 'string' &&
        s!.system_prompt.includes('你是 PRD 写作专家'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 3: defaults applied when optional fields missing (AC2) ────────────

(function testDefaults() {
  console.log('\n[3] defaults for optional fields');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'minimal',
      `---
name: Minimal
description: only required fields
---
body here
`,
    );
    const result = loadFsSkills([], root);
    const s = result.loaded.minimal;
    check('loaded', !!s);
    check("default mode = 'prototype'", s?.mode === 'prototype');
    check("default preview_type = 'html'", s?.preview_type === 'html');
    check("default platform = 'web'", s?.platform === 'web');
    check("default scenario = ''", s?.scenario === '');
    check("default fidelity = 'high'", s?.fidelity === 'high');
    check("default example_prompt = ''", s?.example_prompt === '');
  } finally {
    rmrf(root);
  }
})();

// ─── Test 4: missing required field → skip + error (AC2) ────────────────────

(function testMissingRequired() {
  console.log('\n[4] missing required field → skip');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'no-name',
      `---
description: missing name
---
body
`,
    );
    const result = loadFsSkills([], root);
    check('not loaded', !result.loaded['no-name']);
    check('error recorded', result.errors.some((e) => e.id === 'no-name'));
    check(
      'error mentions name',
      result.errors.some(
        (e) => e.id === 'no-name' && e.message.includes('name'),
      ),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 5: broken YAML → skip + error, others still load (AC5) ────────────

(function testBrokenYaml() {
  console.log('\n[5] broken frontmatter → skip + warn, others unaffected');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'broken',
      `---
name: [unclosed
description: oops
---
body
`,
    );
    writeSkill(
      root,
      'good',
      `---
name: Good
description: valid skill
---
ok
`,
    );
    const result = loadFsSkills([], root);
    check('good loaded', !!result.loaded.good);
    check('broken skipped', !result.loaded.broken);
    check(
      'broken error recorded',
      result.errors.some((e) => e.id === 'broken'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 6: empty file → skip + warn (AC5) ─────────────────────────────────

(function testEmptyFile() {
  console.log('\n[6] empty SKILL.md → skip');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(root, 'empty', '');
    const result = loadFsSkills([], root);
    check('empty skipped', !result.loaded.empty);
    check(
      'empty error recorded',
      result.errors.some(
        (e) => e.id === 'empty' && /empty/i.test(e.message),
      ),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 7: override flag for hardcoded ids (AC3) ──────────────────────────

(function testOverride() {
  console.log('\n[7] FS override of hardcoded id');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'web-prototype',
      `---
name: 用户自定义 Web Prototype
description: 用户覆盖
---
custom prompt
`,
    );
    const result = loadFsSkills(['web-prototype', 'agent-team-blueprint'], root);
    check('overrides contains web-prototype', result.overrides.includes('web-prototype'));
    check(
      'loaded version is user version',
      result.loaded['web-prototype']?.name === '用户自定义 Web Prototype',
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 8: skill_id mismatch → skip (defensive) ───────────────────────────

(function testSkillIdMismatch() {
  console.log('\n[8] skill_id frontmatter must match dir name');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'dir-name',
      `---
skill_id: different-id
name: Mismatch
description: should be skipped
---
body
`,
    );
    const result = loadFsSkills([], root);
    check('mismatched skill skipped', !result.loaded['dir-name']);
    check(
      'mismatch error recorded',
      result.errors.some(
        (e) => e.id === 'dir-name' && /skill_id/.test(e.message),
      ),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 9: reloadSkills() shape + hardcoded baseline ──────────────────────
// Validates the public reloadSkills() API contract. The example/ seed at
// .xueban/skills/example/ is only loaded when server runs from a cwd
// that contains that directory — this test does NOT assert on that
// (Test 1 already covers the "missing dir" path), only on the always-true
// invariant that hardcoded skills survive every reload.

(function testReloadSkills() {
  console.log('\n[9] reloadSkills() public API');
  const result = reloadSkills();
  check(
    'SKILLS contains all hardcoded after reload',
    Object.keys(HARDCODED_SKILLS).every((id) => !!SKILLS[id]),
  );
  check(
    'agent-team-blueprint preserved',
    SKILLS['agent-team-blueprint']?.mode === 'blueprint',
  );
  check(
    'reloadSkills returns { reloaded, errors }',
    typeof result.reloaded === 'number' && Array.isArray(result.errors),
  );
})();

// ─── Test 10: reloadSkills replaces previous overrides on subsequent calls ──
// Important for AC4/hot-reload: removing a SKILL.md and reloading should
// drop the override and restore the hardcoded version.

(function testReloadResetsOverrides() {
  console.log('\n[10] reloadSkills rebuilds from hardcoded baseline');
  const before = SKILLS['web-prototype']?.name;
  // Manually mutate SKILLS to simulate a stale override
  SKILLS['web-prototype'] = {
    ...SKILLS['web-prototype'],
    name: 'STALE OVERRIDE',
  };
  check('mutation took effect', SKILLS['web-prototype'].name === 'STALE OVERRIDE');
  reloadSkills();
  check(
    'reload restores hardcoded name (when no FS override exists at cwd)',
    SKILLS['web-prototype']?.name === before ||
      // if a user happens to have a real .xueban/skills/web-prototype/
      // SKILL.md at cwd, reload should still produce a non-stale name.
      SKILLS['web-prototype']?.name !== 'STALE OVERRIDE',
  );
})();

// ─── Test 11 (S6): allowed-tools frontmatter parsing ────────────────────────

(function testAllowedToolsParsing() {
  console.log('\n[11] S6: allowed-tools frontmatter → SkillDefinition.allowed_tools');
  const root = makeTmpSkillsRoot();
  try {
    // Skill with allowed-tools list
    writeSkill(
      root,
      'team-skill',
      `---
name: Team Skill
description: team-backed skill with tool grants
allowed-tools:
  - list_team_agents
  - get_skill_anchor
  - register_agent
  - register_edge
---
body
`,
    );
    // Skill without allowed-tools
    writeSkill(
      root,
      'no-tools-skill',
      `---
name: No Tools
description: no allowed-tools key at all
---
body
`,
    );
    // Skill with mixed types in allowed-tools (numbers ignored, strings kept)
    writeSkill(
      root,
      'mixed-tools',
      `---
name: Mixed
description: bad entries should be filtered out
allowed-tools:
  - valid_tool
  - 42
  - ""
  - another_valid
---
body
`,
    );

    const result = loadFsSkills([], root);
    const teamSkill = result.loaded['team-skill'];
    check('S6: team-skill loaded', !!teamSkill);
    check(
      'S6: allowed_tools is array',
      Array.isArray(teamSkill?.allowed_tools),
    );
    check('S6: allowed_tools length = 4', teamSkill?.allowed_tools?.length === 4);
    check(
      'S6: allowed_tools includes list_team_agents',
      teamSkill?.allowed_tools?.includes('list_team_agents') === true,
    );
    check(
      'S6: allowed_tools includes register_edge',
      teamSkill?.allowed_tools?.includes('register_edge') === true,
    );

    const noTools = result.loaded['no-tools-skill'];
    check('S6: no-tools-skill loaded', !!noTools);
    check(
      'S6: missing allowed-tools → allowed_tools undefined',
      noTools?.allowed_tools === undefined,
    );

    const mixed = result.loaded['mixed-tools'];
    check('S6: mixed-tools loaded', !!mixed);
    check(
      'S6: numeric / empty-string entries filtered out',
      mixed?.allowed_tools?.length === 2,
    );
    check(
      'S6: only valid string entries kept',
      JSON.stringify(mixed?.allowed_tools?.sort()) ===
        JSON.stringify(['another_valid', 'valid_tool']),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 12 (W2): commands/*.md → `<id>:<cmd>` skill entries ───────────────

(function testSlashCommands() {
  console.log('\n[12] W2: commands/*.md → `<id>:<cmd>` skill entries');
  const root = makeTmpSkillsRoot();
  try {
    // Parent skill with two sub-commands; commands have hyphen-form frontmatter
    // matching real Claude Code plugin commands (allowed-tools, description,
    // disable-model-invocation).
    const skillDir = path.join(root, 'bmad');
    fs.mkdirSync(path.join(skillDir, 'commands'), { recursive: true });
    fs.writeFileSync(
      path.join(skillDir, 'SKILL.md'),
      `---
name: BMAD
description: BMAD-METHOD bundle
---
parent skill body
`,
      'utf-8',
    );
    fs.writeFileSync(
      path.join(skillDir, 'commands', 'prfaq.md'),
      `---
allowed-tools: Bash(gh issue view:*), Bash(gh pr view:*)
description: Generate a PRFAQ
disable-model-invocation: false
---
You are a PRFAQ writer. Output sections.
`,
      'utf-8',
    );
    fs.writeFileSync(
      path.join(skillDir, 'commands', 'create-prd.md'),
      `---
description: Create a PRD
disable-model-invocation: true
---
You are a PRD writer.
`,
      'utf-8',
    );
    // .md file with no frontmatter description should fall back to first line
    fs.writeFileSync(
      path.join(skillDir, 'commands', 'no-fm.md'),
      `Fallback description line\n\nBody continues here.\n`,
      'utf-8',
    );

    const result = loadFsSkills([], root);
    check('W2: parent skill loaded', !!result.loaded.bmad);
    check('W2: bmad:prfaq registered', !!result.loaded['bmad:prfaq']);
    check('W2: bmad:create-prd registered', !!result.loaded['bmad:create-prd']);
    check('W2: bmad:no-fm registered', !!result.loaded['bmad:no-fm']);
    check(
      'W2: prfaq description parsed',
      result.loaded['bmad:prfaq']?.description === 'Generate a PRFAQ',
    );
    check(
      'W2: prfaq system_prompt = body',
      result.loaded['bmad:prfaq']?.system_prompt?.includes('PRFAQ writer') === true,
    );
    check(
      'W2: prfaq allowed_tools parsed (CSV string)',
      Array.isArray(result.loaded['bmad:prfaq']?.allowed_tools) &&
        result.loaded['bmad:prfaq']!.allowed_tools!.length === 2 &&
        result.loaded['bmad:prfaq']!.allowed_tools![0] === 'Bash(gh issue view:*)',
    );
    check(
      'W2: prfaq disable_model_invocation = false → undefined',
      result.loaded['bmad:prfaq']?.disable_model_invocation === undefined,
    );
    check(
      'W2: create-prd disable_model_invocation = true',
      result.loaded['bmad:create-prd']?.disable_model_invocation === true,
    );
    check(
      'W2: no-fm description falls back to first body line',
      result.loaded['bmad:no-fm']?.description === 'Fallback description line',
    );
    check(
      "W2: command-skill mode = 'prototype'",
      result.loaded['bmad:prfaq']?.mode === 'prototype',
    );
    check(
      'W2: command-skill team undefined',
      result.loaded['bmad:prfaq']?.team === undefined,
    );
    check(
      'W2: command-skill executor undefined',
      result.loaded['bmad:prfaq']?.executor === undefined,
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 13 (W2): missing commands/ dir → only parent skill loads ──────────

(function testNoCommandsDir() {
  console.log('\n[13] W2: skill without commands/ subdir still loads cleanly');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'plain',
      `---
name: Plain
description: no commands
---
body
`,
    );
    const result = loadFsSkills([], root);
    check('W2: plain loaded', !!result.loaded.plain);
    check(
      'W2: no <id>:<cmd> entries created',
      !Object.keys(result.loaded).some(k => k.startsWith('plain:')),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 14: cover_url frontmatter passthrough ─────────────────────────────

(function testCoverUrl() {
  console.log('\n[14] cover_url / cover frontmatter → SkillDefinition.cover_url');
  const root = makeTmpSkillsRoot();
  try {
    writeSkill(
      root,
      'with-cover',
      `---
name: With Cover
description: has cover_url
cover_url: https://cdn.example.com/covers/with-cover.png
---
body
`,
    );
    writeSkill(
      root,
      'alias-cover',
      `---
name: Alias Cover
description: uses the cover alias key
cover: /covers/alias.png
---
body
`,
    );
    writeSkill(
      root,
      'no-cover',
      `---
name: No Cover
description: neither key present
---
body
`,
    );

    const result = loadFsSkills([], root);
    check(
      'cover_url parsed from cover_url key',
      result.loaded['with-cover']?.cover_url ===
        'https://cdn.example.com/covers/with-cover.png',
    );
    check(
      'cover alias accepted',
      result.loaded['alias-cover']?.cover_url === '/covers/alias.png',
    );
    check(
      'missing cover → cover_url undefined (omitted from JSON)',
      result.loaded['no-cover']?.cover_url === undefined,
    );
    check(
      'no fabricated default: key absent in JSON',
      !Object.prototype.hasOwnProperty.call(
        JSON.parse(JSON.stringify(result.loaded['no-cover'])),
        'cover_url',
      ),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log('\n────────────────────────────────────────');
console.log(`  ${passCount} passed,  ${failCount} failed`);
console.log('────────────────────────────────────────\n');

if (failCount > 0) process.exit(1);

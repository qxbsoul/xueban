/**
 * design-system-loader.test.ts — standalone smoke tests for the FS-based
 * Design System loader (Story 15.11).
 *
 * Run with:  npx tsx src/loaders/design-system-loader.test.ts   (from server/)
 *
 * Pattern mirrors skill-loader.test.ts — no jest/vitest dependency. Each
 * `check` prints PASS/FAIL; the process exits non-zero if any check fails.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import {
  loadDesignSystemsFromFs,
  seedBuiltinDesignSystems,
} from './design-system-loader';
import {
  HARDCODED_DS,
  DESIGN_SYSTEMS,
  reloadDesignSystems,
  listDesignSystems,
  seedBuiltinDesignSystems as seedFromRegistry,
} from '../design-systems';

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

function makeTmpDsRoot(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'sf-ds-loader-'));
}

function writeDs(root: string, file: string, body: string) {
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, file), body, 'utf-8');
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
  console.log('\n[1] missing design-systems dir → empty result');
  const ghost = path.join(os.tmpdir(), `sf-ds-ghost-${Date.now()}`);
  const result = loadDesignSystemsFromFs(ghost);
  check('loaded is empty', result.loaded.length === 0);
  check('errors is empty', result.errors.length === 0);
})();

// ─── Test 2: parse a valid DESIGN.md (AC1, AC2 happy path) ──────────────────

(function testValidDs() {
  console.log('\n[2] parse a valid DESIGN.md');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'acme.md',
      `---
ds_id: acme-brand
name: ACME Brand 2026
description: ACME 2026 视觉系统
compatible_skills:
  - web-prototype
  - slide-deck
---

## Palette
- Primary: #C9A24A
- Background: #0A0A0F

## Typography
- 标题：Inter Display 700

## Voice
克制、理性、第二人称
`,
    );
    const result = loadDesignSystemsFromFs(root);
    check('1 DS loaded', result.loaded.length === 1);
    check('errors empty', result.errors.length === 0);
    const ds = result.loaded[0];
    check('ds_id parsed', ds.ds_id === 'acme-brand');
    check('name parsed', ds.name === 'ACME Brand 2026');
    check('description parsed', ds.description === 'ACME 2026 视觉系统');
    check(
      'compatible_skills array',
      Array.isArray(ds.compatible_skills) &&
        ds.compatible_skills.length === 2 &&
        ds.compatible_skills.includes('web-prototype') &&
        ds.compatible_skills.includes('slide-deck'),
    );
    check(
      'injection_prompt contains body sections',
      ds.injection_prompt.includes('## Palette') &&
        ds.injection_prompt.includes('Inter Display'),
    );
    check(
      'detected_sections has palette/typography/voice',
      ds.detected_sections.includes('palette') &&
        ds.detected_sections.includes('typography') &&
        ds.detected_sections.includes('voice'),
    );
    check('source = fs', ds.source === 'fs');
    check(
      'source_path ends with acme.md',
      ds.source_path.endsWith('acme.md'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 3: missing ds_id → skip + error (AC6) ─────────────────────────────

(function testMissingDsId() {
  console.log('\n[3] missing ds_id → skip + error');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'broken.md',
      `---
name: No Id Here
description: missing ds_id
---

## Palette
- something
`,
    );
    const result = loadDesignSystemsFromFs(root);
    check('not loaded', result.loaded.length === 0);
    check('error recorded', result.errors.length === 1);
    check(
      'error mentions ds_id',
      result.errors.some(
        (e) => e.file === 'broken.md' && /ds_id/i.test(e.reason),
      ),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 4: broken YAML → skip; other DS unaffected (AC6) ──────────────────

(function testBrokenYaml() {
  console.log('\n[4] broken YAML → skip + warn, others unaffected');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'broken.md',
      `---
ds_id: bad
name: [unclosed
---
body
`,
    );
    writeDs(
      root,
      'good.md',
      `---
ds_id: good-ds
name: Good
description: valid
---

## Palette
- ok
`,
    );
    const result = loadDesignSystemsFromFs(root);
    check('good loaded', result.loaded.some((d) => d.ds_id === 'good-ds'));
    check(
      'broken not loaded',
      !result.loaded.some((d) => d.ds_id === 'bad'),
    );
    check(
      'broken error recorded',
      result.errors.some((e) => e.file === 'broken.md'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 5: empty body → skip ──────────────────────────────────────────────

(function testEmptyBody() {
  console.log('\n[5] empty body → skip');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'empty.md',
      `---
ds_id: empty
name: Empty
description: no body
---
`,
    );
    const result = loadDesignSystemsFromFs(root);
    check(
      'empty not loaded',
      !result.loaded.some((d) => d.ds_id === 'empty'),
    );
    check(
      'empty error recorded',
      result.errors.some(
        (e) => e.file === 'empty.md' && /empty/i.test(e.reason),
      ),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 6: compatible_skills missing → empty array default ────────────────

(function testCompatibleSkillsDefault() {
  console.log('\n[6] missing compatible_skills → []');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'no-compat.md',
      `---
ds_id: no-compat
name: NoCompat
description: no compatible_skills key
---

## Voice
default
`,
    );
    const result = loadDesignSystemsFromFs(root);
    const ds = result.loaded.find((d) => d.ds_id === 'no-compat');
    check('loaded', !!ds);
    check(
      'compatible_skills = []',
      Array.isArray(ds?.compatible_skills) && ds!.compatible_skills.length === 0,
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 7: detected_sections rejects non-canonical headings ───────────────

(function testDetectedSections() {
  console.log('\n[7] detected_sections only includes valid 9-section names');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'mix.md',
      `---
ds_id: mix
name: Mix
description: mix valid + invalid sections
---

## Palette
ok

## Random Section
should be ignored

## Anti-patterns
ok

## Code Examples
ok
`,
    );
    const result = loadDesignSystemsFromFs(root);
    const ds = result.loaded.find((d) => d.ds_id === 'mix')!;
    check(
      'palette detected',
      ds.detected_sections.includes('palette'),
    );
    check(
      'anti-patterns detected',
      ds.detected_sections.includes('anti-patterns'),
    );
    check(
      'code examples detected',
      ds.detected_sections.includes('code examples'),
    );
    check(
      '"Random Section" NOT detected',
      !ds.detected_sections.includes('random section'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 8: seedBuiltinDesignSystems writes 4 files & is idempotent (AC5) ──

(function testSeedIdempotent() {
  console.log('\n[8] seedBuiltinDesignSystems is idempotent');
  const root = makeTmpDsRoot();
  try {
    const builtins = Object.values(HARDCODED_DS);
    // First run: all 4 should be written.
    const r1 = seedBuiltinDesignSystems(builtins, root);
    check('first run wrote 4 files', r1.written.length === 4);
    check('first run skipped 0', r1.skipped.length === 0);
    for (const id of ['tailwind', 'material', 'shadcn', 'none']) {
      check(
        `${id}.md exists on disk`,
        fs.existsSync(path.join(root, `${id}.md`)),
      );
    }
    // Mutate one file to verify it is NOT overwritten on second run
    const tailwindPath = path.join(root, 'tailwind.md');
    const userEdited =
      '---\nds_id: tailwind\nname: "User Edited Tailwind"\ndescription: "edited"\ncompatible_skills: ["web-prototype"]\n---\n\n## Palette\nUSER CHANGED\n';
    fs.writeFileSync(tailwindPath, userEdited, 'utf-8');

    const r2 = seedBuiltinDesignSystems(builtins, root);
    check('second run wrote 0 files', r2.written.length === 0);
    check('second run skipped 4', r2.skipped.length === 4);
    const after = fs.readFileSync(tailwindPath, 'utf-8');
    check(
      'user edit preserved (no overwrite)',
      after.includes('USER CHANGED'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 9: seed + load roundtrip — FS files parse back into 4 builtins ────

(function testSeedRoundtrip() {
  console.log('\n[9] seed → load roundtrip yields 4 valid DS');
  const root = makeTmpDsRoot();
  try {
    const builtins = Object.values(HARDCODED_DS);
    seedBuiltinDesignSystems(builtins, root);
    const result = loadDesignSystemsFromFs(root);
    check('4 DS loaded from seed', result.loaded.length === 4);
    check('no errors', result.errors.length === 0);
    for (const id of ['tailwind', 'material', 'shadcn', 'none']) {
      check(
        `${id} present in loaded`,
        result.loaded.some((d) => d.ds_id === id),
      );
    }
    // tailwind seed should still mention CDN script tag
    const tw = result.loaded.find((d) => d.ds_id === 'tailwind')!;
    check(
      'tailwind injection_prompt round-trips CDN tag',
      tw.injection_prompt.includes('cdn.tailwindcss.com'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 10: reloadDesignSystems merge — FS overrides hardcoded (AC1) ──────

(function testReloadMerge() {
  console.log('\n[10] reloadDesignSystems FS-priority merge');
  const root = makeTmpDsRoot();
  try {
    // FS provides override for `tailwind` AND a brand-new `acme-brand`.
    writeDs(
      root,
      'tailwind.md',
      `---
ds_id: tailwind
name: User Override Tailwind
description: user-edited
compatible_skills: ["web-prototype"]
---

## Palette
custom user palette
`,
    );
    writeDs(
      root,
      'acme.md',
      `---
ds_id: acme-brand
name: ACME Brand
description: bespoke brand DS
compatible_skills: []
---

## Brand
ACME stuff
`,
    );

    const result = reloadDesignSystems(root);
    check('reloaded count = 2', result.reloaded === 2);
    check('failed count = 0', result.failed === 0);
    check(
      'overrides contains tailwind',
      result.overrides.includes('tailwind'),
    );
    // After reload, DESIGN_SYSTEMS reflects the new merge.
    check(
      'tailwind name overridden',
      DESIGN_SYSTEMS.tailwind?.name === 'User Override Tailwind',
    );
    check(
      'acme-brand added',
      !!DESIGN_SYSTEMS['acme-brand'] &&
        DESIGN_SYSTEMS['acme-brand'].name === 'ACME Brand',
    );
    // Hardcoded `material` / `shadcn` / `none` survive (no FS override).
    for (const id of ['material', 'shadcn', 'none']) {
      check(
        `${id} hardcoded baseline preserved`,
        DESIGN_SYSTEMS[id]?.name === HARDCODED_DS[id].name,
      );
    }

    // Now remove the FS override and reload — tailwind should restore.
    fs.unlinkSync(path.join(root, 'tailwind.md'));
    fs.unlinkSync(path.join(root, 'acme.md'));
    const r2 = reloadDesignSystems(root);
    check('reload after delete: 0 loaded', r2.reloaded === 0);
    check(
      'tailwind restored to hardcoded name',
      DESIGN_SYSTEMS.tailwind?.name === HARDCODED_DS.tailwind.name,
    );
    check(
      'acme-brand removed after FS deletion',
      !DESIGN_SYSTEMS['acme-brand'],
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 11: listDesignSystems(?skill) — empty array = compatible all ─────

(function testListFilter() {
  console.log('\n[11] listDesignSystems compatible_skills filter (AC3)');
  const root = makeTmpDsRoot();
  try {
    writeDs(
      root,
      'allcompat.md',
      `---
ds_id: allcompat
name: AllCompat
description: empty array means all
compatible_skills: []
---

## Voice
universal
`,
    );
    writeDs(
      root,
      'webonly.md',
      `---
ds_id: webonly
name: WebOnly
description: only web
compatible_skills: ["web-prototype"]
---

## Palette
web-only stuff
`,
    );
    reloadDesignSystems(root);

    const all = listDesignSystems();
    check(
      'no skill filter returns full list (≥ 2 from FS + builtins)',
      all.length >= 2,
    );
    const forReport = listDesignSystems('report');
    check(
      'report sees allcompat (empty array = all)',
      forReport.some((d) => d.ds_id === 'allcompat'),
    );
    check(
      'report does NOT see webonly',
      !forReport.some((d) => d.ds_id === 'webonly'),
    );
    const forWeb = listDesignSystems('web-prototype');
    check(
      'web-prototype sees webonly',
      forWeb.some((d) => d.ds_id === 'webonly'),
    );
    check(
      'web-prototype sees allcompat',
      forWeb.some((d) => d.ds_id === 'allcompat'),
    );

    // Cleanup: reload from a now-deleted root to clear stale entries
    fs.unlinkSync(path.join(root, 'allcompat.md'));
    fs.unlinkSync(path.join(root, 'webonly.md'));
    reloadDesignSystems(root);
  } finally {
    rmrf(root);
  }
})();

// ─── Test 12: seedBuiltinDesignSystems re-export from registry module ───────

(function testSeedRegistryExport() {
  console.log('\n[12] seedBuiltinDesignSystems re-exported from registry');
  const root = makeTmpDsRoot();
  try {
    const r = seedFromRegistry(root);
    check('wrote 4 builtins via registry export', r.written.length === 4);
    check(
      'tailwind.md exists',
      fs.existsSync(path.join(root, 'tailwind.md')),
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

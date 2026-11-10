/**
 * prompt-assembly.test.ts — standalone smoke test for prompt-assembly.ts
 * (Story 15.13)
 *
 * Run with:  npx tsx src/prompt-assembly.test.ts   (from server/)
 *
 * No external test framework — vitest/jest are not yet installed in the
 * server package. Mirrors the pattern used by design-systems.test.ts.
 *
 * Coverage:
 *   - 7-layer composition with all data present
 *   - Layer order is canonical (DISCOVERY → Identity → DS → Skill →
 *     Framework → ProjectMeta → Sides; conversation_history slots in before
 *     Side files when supplied — framework is STATIC, not last)
 *   - layer_toggles disables individual layers without affecting siblings
 *   - Empty inputs (no DS, no project_meta, no sides) drop layers cleanly
 *     (no leftover separators, no blank lines)
 *   - project_meta serialization: string / number / boolean direct, complex
 *     values via JSON.stringify; key insertion order preserved
 *   - Framework directive triggers ONLY when skill.mode === 'deck'
 *   - total_chars matches prompt.length
 *   - layers_included + layers_skipped together account for all 8 keys
 */

import {
  composeSystemPrompt,
  LAYER_ORDER,
  LAYER_SEPARATOR,
} from './prompt-assembly';
import { DISCOVERY_CHARTER } from './discovery-charter';
import { IDENTITY_CHARTER } from './identity-charter';

let passCount = 0;
let failCount = 0;

function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    passCount++;
    console.log(`  PASS  ${label}`);
  } else {
    failCount++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('       ', detail);
  }
}

function main(): void {
  console.log('\n[1] full 7-layer composition (all data present)');
  const full = composeSystemPrompt({
    ds_injection: '## Design System: Tailwind\n\nUse utility classes.',
    skill_system_prompt: '## Skill: Web Prototype\n\nProduce a single HTML file.',
    skill_mode: 'deck',
    project_meta: {
      kind: 'landing-page',
      fidelity: 'production',
      animations: 'minimal',
    },
    side_files: '## SIDE FILES\n\n- references/brand.md',
  });
  // Story 15.29: 8 layers when conversation_history is also supplied; this
  // assertion stays at 7 because `full` does not provide conversation_history.
  check('7 layers included when all (non-history) data present', full.layers_included.length === 7);
  // Story 15.29: with no conversation_history input, that single layer is
  // dropped — the remaining 7 must still appear in canonical order.
  check(
    'layers_included matches canonical order (minus conversation_history)',
    JSON.stringify(full.layers_included) ===
      JSON.stringify(LAYER_ORDER.filter((k) => k !== 'conversation_history')),
    full.layers_included,
  );
  check(
    'layers_skipped is just conversation_history when no history input',
    full.layers_skipped.length === 1 &&
      full.layers_skipped[0] === 'conversation_history',
    full.layers_skipped,
  );
  check('framework label = "deck" for deck mode', full.framework === 'deck');
  check('total_chars matches prompt.length', full.total_chars === full.prompt.length);
  check('prompt starts with DISCOVERY charter', full.prompt.startsWith(DISCOVERY_CHARTER));
  check('prompt contains Identity charter', full.prompt.includes(IDENTITY_CHARTER));
  check(
    'prompt contains DS injection',
    full.prompt.includes('## Design System: Tailwind'),
  );
  check(
    'prompt contains Skill system_prompt',
    full.prompt.includes('## Skill: Web Prototype'),
  );
  check('prompt contains Project Meta block', full.prompt.includes('## PROJECT META'));
  check(
    'prompt contains Side files block',
    full.prompt.includes('## SIDE FILES'),
  );
  check(
    'prompt contains Framework directive (PPTX)',
    full.prompt.includes('PPTX-COMPATIBLE DECK'),
  );
  // Layers are joined by LAYER_SEPARATOR and the S8 dynamic-boundary marker
  // (`__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__`) occupies a segment of its own, so a
  // prompt with N rendered layers always carries exactly N separators
  // (N-1 between layers + 1 straddling the boundary marker) — not N-1.
  const sepCount = full.prompt.split(LAYER_SEPARATOR).length - 1;
  check(
    'separator count = rendered layer count (boundary marker is its own segment)',
    sepCount === full.layers_included.length,
    { sepCount, layers: full.layers_included.length },
  );

  // Layer ORDER verification: walk indexOf for each layer marker
  const idxDiscovery = full.prompt.indexOf('## DISCOVERY MODE');
  const idxIdentity = full.prompt.indexOf('## IDENTITY');
  const idxDS = full.prompt.indexOf('## Design System: Tailwind');
  const idxSkill = full.prompt.indexOf('## Skill: Web Prototype');
  const idxFramework = full.prompt.indexOf('## FRAMEWORK: PPTX-COMPATIBLE DECK');
  const idxProject = full.prompt.indexOf('## PROJECT META');
  const idxSides = full.prompt.indexOf('## SIDE FILES');
  check(
    'physical order matches canonical layer order',
    idxDiscovery < idxIdentity &&
      idxIdentity < idxDS &&
      idxDS < idxSkill &&
      idxSkill < idxFramework &&
      idxFramework < idxProject &&
      idxProject < idxSides,
    {
      idxDiscovery,
      idxIdentity,
      idxDS,
      idxSkill,
      idxFramework,
      idxProject,
      idxSides,
    },
  );

  console.log('\n[2] non-deck skill → framework layer is dropped');
  const proto = composeSystemPrompt({
    ds_injection: 'DS_BODY',
    skill_system_prompt: 'SKILL_BODY',
    skill_mode: 'prototype',
  });
  check('framework not in layers_included', !proto.layers_included.includes('framework'));
  check('framework in layers_skipped', proto.layers_skipped.includes('framework'));
  check('framework label is null', proto.framework === null);
  check(
    'no PPTX directive leaks for prototype mode',
    !proto.prompt.includes('PPTX-COMPATIBLE DECK'),
  );

  console.log('\n[3] layer_toggles disables specific layers');
  const noIdentity = composeSystemPrompt({
    ds_injection: 'DS_BODY',
    skill_system_prompt: 'SKILL_BODY',
    skill_mode: 'prototype',
    layer_toggles: { identity: false },
  });
  check('identity not included', !noIdentity.layers_included.includes('identity'));
  check('identity in skipped', noIdentity.layers_skipped.includes('identity'));
  check('discovery still included', noIdentity.layers_included.includes('discovery'));
  check('ds still included', noIdentity.layers_included.includes('ds'));
  check('skill still included', noIdentity.layers_included.includes('skill'));
  check(
    'no Identity content in prompt',
    !noIdentity.prompt.includes('OpenDesign-compatible artifact author'),
  );

  console.log('\n[4] empty DS / project_meta / sides → those layers drop');
  const sparse = composeSystemPrompt({
    skill_system_prompt: 'JUST_THE_SKILL',
    skill_mode: 'prototype',
    // ds_injection, project_meta, side_files all omitted
  });
  check('discovery included', sparse.layers_included.includes('discovery'));
  check('identity included', sparse.layers_included.includes('identity'));
  check('skill included', sparse.layers_included.includes('skill'));
  check('ds skipped', sparse.layers_skipped.includes('ds'));
  check('project skipped', sparse.layers_skipped.includes('project'));
  // Story 15.29: history skipped when input.conversation_history not supplied.
  check('conversation_history skipped', sparse.layers_skipped.includes('conversation_history'));
  check('sides skipped', sparse.layers_skipped.includes('sides'));
  check('framework skipped (prototype mode)', sparse.layers_skipped.includes('framework'));
  // Critical: no orphan separator / blank line where empty layers used to be.
  check(
    'no doubled separator in prompt',
    !sparse.prompt.includes(LAYER_SEPARATOR + LAYER_SEPARATOR),
  );
  check(
    'no leading/trailing separator',
    !sparse.prompt.startsWith(LAYER_SEPARATOR) &&
      !sparse.prompt.endsWith(LAYER_SEPARATOR),
  );
  // 3 layers (discovery, identity, skill) → 3 separators: 2 between layers +
  // 1 straddling the always-emitted S8 boundary marker.
  const sparseSeps = sparse.prompt.split(LAYER_SEPARATOR).length - 1;
  check(
    'separator count = 3 rendered layers (no orphan separators)',
    sparseSeps === sparse.layers_included.length && sparse.layers_included.length === 3,
    { sparseSeps, layers: sparse.layers_included.length },
  );

  console.log('\n[5] empty project_meta {} drops the layer');
  const emptyMeta = composeSystemPrompt({
    skill_system_prompt: 'X',
    project_meta: {},
  });
  check('empty {} → project skipped', emptyMeta.layers_skipped.includes('project'));
  check('no PROJECT META heading in prompt', !emptyMeta.prompt.includes('## PROJECT META'));

  console.log('\n[6] project_meta value type handling');
  const meta = composeSystemPrompt({
    skill_system_prompt: 'X',
    project_meta: {
      kind: 'landing-page',
      fidelity_score: 0.85,
      production_ready: true,
      tags: ['b2b', 'enterprise'],
      config: { theme: 'dark', density: 'compact' },
    },
  });
  check('string value rendered raw', meta.prompt.includes('- kind: landing-page'));
  check('number value rendered raw', meta.prompt.includes('- fidelity_score: 0.85'));
  check('boolean value rendered raw', meta.prompt.includes('- production_ready: true'));
  check(
    'array value JSON.stringify\'d',
    meta.prompt.includes('- tags: ["b2b","enterprise"]'),
  );
  check(
    'object value JSON.stringify\'d',
    meta.prompt.includes('- config: {"theme":"dark","density":"compact"}'),
  );

  // Key order preservation
  const idxKind = meta.prompt.indexOf('- kind:');
  const idxFid = meta.prompt.indexOf('- fidelity_score:');
  const idxProd = meta.prompt.indexOf('- production_ready:');
  const idxTags = meta.prompt.indexOf('- tags:');
  const idxConfig = meta.prompt.indexOf('- config:');
  check(
    'project_meta key insertion order preserved',
    idxKind < idxFid && idxFid < idxProd && idxProd < idxTags && idxTags < idxConfig,
  );

  console.log('\n[7] all toggles off → layers_included is empty, prompt is empty');
  const allOff = composeSystemPrompt({
    skill_system_prompt: 'X',
    layer_toggles: {
      discovery: false,
      identity: false,
      ds: false,
      skill: false,
      project: false,
      sides: false,
      framework: false,
    },
  });
  check('all-off → 0 included layers', allOff.layers_included.length === 0);
  // Story 15.29: 8 layers total (7 + conversation_history)
  check('all-off → 8 skipped layers', allOff.layers_skipped.length === 8);
  check('all-off → empty prompt', allOff.prompt === '');
  check('all-off → total_chars 0', allOff.total_chars === 0);

  console.log('\n[8] included + skipped partition all 8 layers (any input)');
  const partition = composeSystemPrompt({
    ds_injection: '',
    skill_system_prompt: 'X',
    skill_mode: 'prototype',
  });
  const allKeys = [...partition.layers_included, ...partition.layers_skipped].sort();
  const expected = [...LAYER_ORDER].sort();
  check(
    'included ∪ skipped = canonical 8 keys',
    JSON.stringify(allKeys) === JSON.stringify(expected),
    { allKeys, expected },
  );
  check(
    'no key appears in both partitions',
    partition.layers_included.every((k) => !partition.layers_skipped.includes(k)),
  );

  console.log('\n[9] layer_toggles default (undefined) → all layers eligible');
  const defaultToggles = composeSystemPrompt({
    ds_injection: 'DS',
    skill_system_prompt: 'SKILL',
    skill_mode: 'deck',
    project_meta: { k: 'v' },
    side_files: 'SIDES',
    // layer_toggles intentionally omitted
  });
  check(
    'undefined toggles → 7 layers (no conversation_history input)',
    defaultToggles.layers_included.length === 7,
    defaultToggles.layers_included,
  );

  console.log('\n[10] compose SSE event shape (matches what run-sessions.ts emits)');
  // Construct the event payload from a compose result the same way
  // run-sessions.ts will. This locks the schema in test.
  const r = composeSystemPrompt({
    ds_injection: 'DS',
    skill_system_prompt: 'SKILL',
    skill_mode: 'deck',
  });
  const event = {
    layers: r.layers_included,
    skipped: r.layers_skipped,
    total_chars: r.total_chars,
    framework: r.framework,
  };
  check('event.layers is array', Array.isArray(event.layers));
  check('event.skipped is array', Array.isArray(event.skipped));
  check('event.total_chars is number', typeof event.total_chars === 'number');
  check(
    'event has no prompt text leak (no `prompt` field)',
    !('prompt' in event),
  );

  // ── Story 15.29 — conversation_history layer ─────────────────────────────
  console.log('\n[11] Story 15.29 — conversation_history layer');

  // 11a — LAYER_ORDER constant has 8 keys in canonical order. The first five
  // (discovery / identity / ds / skill / framework) form the cacheable STATIC
  // segment, so framework sits at slot 4 — not last, as it did before S8.
  const expectedOrder = [
    'discovery',
    'identity',
    'ds',
    'skill',
    'framework',
    'project',
    'conversation_history',
    'sides',
  ];
  check(
    'LAYER_ORDER has 8 keys with conversation_history at slot 6',
    JSON.stringify([...LAYER_ORDER]) === JSON.stringify(expectedOrder),
    [...LAYER_ORDER],
  );

  // 11b — conversation_history is inserted between project and sides
  const withHistory = composeSystemPrompt({
    skill_system_prompt: '## Skill: X',
    project_meta: { kind: 'webpage' },
    conversation_history:
      '## CONVERSATION HISTORY\n\n### User\n做一个网页\n\n### Assistant\n好的',
    side_files: '## SIDE FILES\n\n<snippet>',
  });
  check(
    'conversation_history is in layers_included',
    withHistory.layers_included.includes('conversation_history'),
    withHistory.layers_included,
  );
  const idxProject2 = withHistory.layers_included.indexOf('project');
  const idxHistory2 = withHistory.layers_included.indexOf('conversation_history');
  const idxSides2 = withHistory.layers_included.indexOf('sides');
  check(
    'project < conversation_history < sides in layers_included',
    idxProject2 >= 0 && idxProject2 < idxHistory2 && idxHistory2 < idxSides2,
    { idxProject2, idxHistory2, idxSides2 },
  );
  check(
    'rendered prompt contains the history block text',
    withHistory.prompt.includes('做一个网页'),
  );
  check(
    'rendered prompt: project block precedes history precedes sides',
    withHistory.prompt.indexOf('## PROJECT META') <
      withHistory.prompt.indexOf('做一个网页') &&
      withHistory.prompt.indexOf('做一个网页') <
        withHistory.prompt.indexOf('## SIDE FILES'),
  );

  // 11c — empty / missing conversation_history → drop the layer
  const noHistory = composeSystemPrompt({
    skill_system_prompt: 'X',
    skill_mode: 'prototype',
  });
  check(
    'no history input → conversation_history in layers_skipped',
    noHistory.layers_skipped.includes('conversation_history'),
  );
  check(
    'no history input → conversation_history NOT in layers_included',
    !noHistory.layers_included.includes('conversation_history'),
  );

  // 11d — toggle off explicitly drops the layer even when content present
  const toggledOff = composeSystemPrompt({
    skill_system_prompt: 'X',
    conversation_history: '## CONVERSATION HISTORY\n\n### User\nHELLO',
    layer_toggles: { conversation_history: false },
  });
  check(
    'toggle off → conversation_history dropped despite content',
    toggledOff.layers_skipped.includes('conversation_history') &&
      !toggledOff.prompt.includes('HELLO'),
  );

  // 11e — pure-whitespace conversation_history is treated as empty
  const whitespaceHistory = composeSystemPrompt({
    skill_system_prompt: 'X',
    conversation_history: '   \n\n  \t  \n',
  });
  check(
    'pure-whitespace history is dropped',
    whitespaceHistory.layers_skipped.includes('conversation_history'),
  );

  // 11f — when only conversation_history + skill present, total layers = 3
  // (discovery + identity + skill are charters that always render)
  const minimalWithHistory = composeSystemPrompt({
    skill_system_prompt: 'SKILL',
    conversation_history: '## CONVERSATION HISTORY\n\n### User\nA',
  });
  check(
    'minimal + history → 4 included layers (discovery+identity+skill+history)',
    minimalWithHistory.layers_included.length === 4,
    minimalWithHistory.layers_included,
  );

  // ── Summary ───────────────────────────────────────────────────────────────
  console.log('\n────────────────────────────────────────');
  console.log(`  ${passCount} passed,  ${failCount} failed`);
  console.log('────────────────────────────────────────\n');

  // vitest 把直接调用 process.exit() 判为「unexpected exit」，整个文件会红成
  // file-level error（看不出是哪条 check 挂了）。所以在 vitest 里改成抛错，
  // 让 runner 正常报出失败；`npx tsx` 单独跑时保留退出码语义。
  if (failCount > 0) {
    if (process.env.VITEST) throw new Error(`${failCount} checks failed`);
    process.exit(1);
  }
}

main();

/**
 * design-systems.test.ts — standalone smoke test for design-systems.ts
 * (Story 15.5)
 *
 * Run with:  npx tsx src/design-systems.test.ts   (from server/)
 *
 * No external test framework — vitest/jest are not yet installed in the
 * server package. Mirrors the pattern used by storage/runs.test.ts.
 *
 * Coverage:
 *   - DESIGN_SYSTEMS schema (4 entries, required fields)
 *   - 'none' has empty injection_prompt; others non-empty
 *   - compatible_skills correctness (web-prototype ⊆ all DS, blueprint/report
 *     only in 'none')
 *   - Tailwind injection_prompt mentions CDN script tag (DoD: generated HTML
 *     should be wired through this string for downstream verification)
 *   - getInjectionPrompt() helper: known id, unknown id, undefined / null
 */

import { DESIGN_SYSTEMS, getInjectionPrompt } from './design-systems';

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

function main() {
  // ── Test 1: schema ────────────────────────────────────────────────────────
  console.log('\n[1] DESIGN_SYSTEMS schema');
  const ids = Object.keys(DESIGN_SYSTEMS).sort();
  check(
    'exactly 4 design systems registered',
    ids.length === 4,
    ids,
  );
  check(
    'expected ids present',
    ['material', 'none', 'shadcn', 'tailwind'].every((k) => ids.includes(k)),
    ids,
  );

  for (const [id, ds] of Object.entries(DESIGN_SYSTEMS)) {
    check(`[${id}] ds_id matches key`, ds.ds_id === id);
    check(`[${id}] name non-empty string`, typeof ds.name === 'string' && ds.name.length > 0);
    check(`[${id}] description non-empty`, typeof ds.description === 'string' && ds.description.length > 0);
    check(
      `[${id}] compatible_skills is array`,
      Array.isArray(ds.compatible_skills) && ds.compatible_skills.length > 0,
    );
    check(`[${id}] injection_prompt is string`, typeof ds.injection_prompt === 'string');
  }

  // ── Test 2: injection_prompt presence ─────────────────────────────────────
  console.log('\n[2] injection_prompt presence');
  check(
    "'none' has empty injection_prompt",
    DESIGN_SYSTEMS.none.injection_prompt === '',
  );
  check(
    "'tailwind' has non-empty injection_prompt",
    DESIGN_SYSTEMS.tailwind.injection_prompt.length > 0,
  );
  check(
    "'material' has non-empty injection_prompt",
    DESIGN_SYSTEMS.material.injection_prompt.length > 0,
  );
  check(
    "'shadcn' has non-empty injection_prompt",
    DESIGN_SYSTEMS.shadcn.injection_prompt.length > 0,
  );

  // ── Test 3: compatible_skills correctness ─────────────────────────────────
  console.log('\n[3] compatible_skills');
  // every DS must be compatible with web-prototype (the only HTML-emitting skill)
  for (const id of ['none', 'tailwind', 'material', 'shadcn']) {
    check(
      `[${id}] compatible with web-prototype`,
      DESIGN_SYSTEMS[id].compatible_skills.includes('web-prototype'),
    );
  }
  // only 'none' is compatible with blueprint / report
  check(
    "'none' compatible with agent-team-blueprint",
    DESIGN_SYSTEMS.none.compatible_skills.includes('agent-team-blueprint'),
  );
  check(
    "'none' compatible with report",
    DESIGN_SYSTEMS.none.compatible_skills.includes('report'),
  );
  check(
    "'tailwind' NOT compatible with agent-team-blueprint",
    !DESIGN_SYSTEMS.tailwind.compatible_skills.includes('agent-team-blueprint'),
  );
  check(
    "'tailwind' NOT compatible with report",
    !DESIGN_SYSTEMS.tailwind.compatible_skills.includes('report'),
  );
  check(
    "'material' NOT compatible with report",
    !DESIGN_SYSTEMS.material.compatible_skills.includes('report'),
  );
  check(
    "'shadcn' NOT compatible with report",
    !DESIGN_SYSTEMS.shadcn.compatible_skills.includes('report'),
  );

  // ── Test 4: Tailwind injection mentions CDN ───────────────────────────────
  console.log('\n[4] tailwind injection content');
  const tw = DESIGN_SYSTEMS.tailwind.injection_prompt;
  check(
    'tailwind injection includes CDN script tag',
    tw.includes('cdn.tailwindcss.com'),
  );
  check(
    'tailwind injection mentions blue-600',
    tw.includes('blue-600'),
  );

  // ── Test 5: getInjectionPrompt() helper ───────────────────────────────────
  console.log('\n[5] getInjectionPrompt');
  check(
    'known id returns matching prompt',
    getInjectionPrompt('tailwind') === DESIGN_SYSTEMS.tailwind.injection_prompt,
  );
  check(
    "'none' returns ''",
    getInjectionPrompt('none') === '',
  );
  check(
    'unknown id returns ""',
    getInjectionPrompt('does-not-exist') === '',
  );
  check(
    'undefined returns ""',
    getInjectionPrompt(undefined) === '',
  );
  check(
    'null returns ""',
    getInjectionPrompt(null) === '',
  );

  // ── Test 6: route-layer composition shape (mirrors run-sessions.ts) ──────
  console.log('\n[6] augmented prompt composition');
  const fakeSkillPrompt = 'BASE_SKILL_PROMPT';
  const composed = DESIGN_SYSTEMS.tailwind.injection_prompt
    ? `${fakeSkillPrompt}\n\n${DESIGN_SYSTEMS.tailwind.injection_prompt}`
    : fakeSkillPrompt;
  check(
    'composed prompt starts with skill prompt',
    composed.startsWith('BASE_SKILL_PROMPT\n\n'),
  );
  check(
    'composed prompt contains injected DS block',
    composed.includes('Design System: Tailwind CSS'),
  );

  // 'none' must not append a separator (caller side decides via empty string)
  const composedNone = DESIGN_SYSTEMS.none.injection_prompt
    ? `${fakeSkillPrompt}\n\n${DESIGN_SYSTEMS.none.injection_prompt}`
    : fakeSkillPrompt;
  check(
    "'none' yields skill prompt unchanged",
    composedNone === 'BASE_SKILL_PROMPT',
  );

  // ── Summary ───────────────────────────────────────────────────────────────
  console.log('\n────────────────────────────────────────');
  console.log(`  ${passCount} passed,  ${failCount} failed`);
  console.log('────────────────────────────────────────\n');

  if (failCount > 0) process.exit(1);
}

main();

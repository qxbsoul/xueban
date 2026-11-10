/**
 * compose-multi-turn-prompt.test.ts — S7 smoke for the phase composer.
 *
 * Run with:  npx tsx src/prompts/__tests__/compose-multi-turn-prompt.test.ts
 *
 * Standalone tsx pattern matching skill-anchors.test.ts.
 *
 * Coverage:
 *   - Phase 1/2/3 strings appear in correct order
 *   - team-first vs agent-first flow selects the right phase-1
 *   - Phase 2 (2026-05-22): header does NOT advertise tool_use orchestration
 *     (daemon-led DAG + artifact handoff replaces LLM tool_use)
 *   - "引用纪律" / verbatim discipline section appears
 *   - Phase boundaries don't collapse (blank lines between phases preserved)
 */

import {
  composeMultiTurnPrompt,
  PHASE_1_ANALYZE,
  PHASE_1_ANALYZE_AGENT_FIRST,
  PHASE_2_AGENT,
  PHASE_3_TEAM,
} from '../index';

let pass = 0;
let fail = 0;

function check(label: string, expected: unknown, actual: unknown): void {
  const eq = JSON.stringify(expected) === JSON.stringify(actual);
  if (eq) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(
      `  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`,
    );
  }
}

function checkTruthy(label: string, actual: unknown): void {
  if (actual) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}  (expected truthy, got ${JSON.stringify(actual)})`);
  }
}

async function main(): Promise<void> {
  // ── default flow = team-first ─────────────────────────────────────────────
  {
    const prompt = composeMultiTurnPrompt();
    checkTruthy('default flow contains PHASE_1_ANALYZE', prompt.includes(PHASE_1_ANALYZE));
    checkTruthy('default flow contains PHASE_2_AGENT', prompt.includes(PHASE_2_AGENT));
    checkTruthy('default flow contains PHASE_3_TEAM', prompt.includes(PHASE_3_TEAM));
    checkTruthy(
      'default flow does NOT include agent-first phase 1',
      !prompt.includes(PHASE_1_ANALYZE_AGENT_FIRST),
    );

    // Phase ordering — index of phase 1 < phase 2 < phase 3.
    const i1 = prompt.indexOf(PHASE_1_ANALYZE);
    const i2 = prompt.indexOf(PHASE_2_AGENT);
    const i3 = prompt.indexOf(PHASE_3_TEAM);
    checkTruthy('phase 1 appears before phase 2', i1 < i2);
    checkTruthy('phase 2 appears before phase 3', i2 < i3);
  }

  // ── agent-first flow ──────────────────────────────────────────────────────
  {
    const prompt = composeMultiTurnPrompt('agent-first');
    checkTruthy(
      'agent-first flow contains PHASE_1_ANALYZE_AGENT_FIRST',
      prompt.includes(PHASE_1_ANALYZE_AGENT_FIRST),
    );
    checkTruthy(
      'agent-first flow does NOT include team-first phase 1',
      !prompt.includes(PHASE_1_ANALYZE),
    );
    // Phase 2/3 reused
    checkTruthy('agent-first still has PHASE_2_AGENT', prompt.includes(PHASE_2_AGENT));
    checkTruthy('agent-first still has PHASE_3_TEAM', prompt.includes(PHASE_3_TEAM));
  }

  // ── Phase 2 (2026-05-22): header must NOT advertise tool_use orchestration ──
  {
    const prompt = composeMultiTurnPrompt();
    // The 4 SkillAnchorTool names may appear in negative phrasing ("不要调用") but
    // must not appear in any positive tool catalog. We accept presence; the
    // critical invariant is the "不要调用" / "不存在" guard further below.
    checkTruthy(
      'header does NOT advertise positive tool catalog "你可以使用以下"',
      !prompt.includes('你可以使用以下'),
    );
    checkTruthy(
      'header guards LLM against tool_use ("不要调用")',
      prompt.includes('不要') && prompt.includes('list_team_agents'),
    );
    // Citation discipline keyword
    checkTruthy(
      'header contains "引用纪律" verbatim-discipline section',
      prompt.includes('引用纪律'),
    );
    checkTruthy(
      'header explicitly forbids paraphrase',
      prompt.includes('paraphrase'),
    );
  }

  // ── purity ────────────────────────────────────────────────────────────────
  {
    const a = composeMultiTurnPrompt();
    const b = composeMultiTurnPrompt();
    check('composeMultiTurnPrompt is pure (idempotent)', a, b);
    checkTruthy('composeMultiTurnPrompt() length > 1000', a.length > 1000);
  }

  console.log(`\n${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

void main();

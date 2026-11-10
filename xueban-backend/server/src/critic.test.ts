/**
 * critic.test.ts — Story 15.14 — standalone runner.
 *
 * Verifies:
 *   - composeCritiquePrompt embeds user goal, expected steps, sf_steps_seen,
 *     lint findings, and asks for the <sf:critique> JSON envelope.
 *   - parseCritique handles valid JSON-in-XML (5 dims + summary).
 *   - parseCritique handles missing tag → scores:null.
 *   - parseCritique handles invalid JSON → scores:null.
 *   - runCritique returns CRITIQUE_NO_API_KEY when no key supplied.
 *   - runCritique returns CRITIQUE_API_ERROR on 401 (mocked fetch).
 *   - runCritique returns parsed output on success (mocked fetch).
 */

import {
  composeCritiquePrompt,
  parseCritique,
  runCritique,
  CRITIQUE_DIMENSIONS,
  type CritiqueOutput,
} from './critic';
import type { LintResult } from './lint';

let failed = 0;
function assert(cond: unknown, msg: string): void {
  if (!cond) { console.error(`  FAIL: ${msg}`); failed += 1; }
  else { console.log(`  ok: ${msg}`); }
}

const fakeLint: LintResult = {
  filename: 'team.blueprint.yml',
  type: 'yaml',
  language: 'yaml',
  findings: [
    { rule: 'missing-required-field', severity: 'error', message: "blueprint missing top-level 'policy_matrix'" },
    { rule: 'empty-collection', severity: 'warning', message: "blueprint 'agents' list is empty" },
  ],
  summary: { errors: 1, warnings: 1, infos: 0 },
  _meta: {
    sf_steps_seen: ['discover', 'draft', 'review'],
    sf_steps_completed: ['discover', 'draft'],
  },
};

console.log('--- composeCritiquePrompt ---');
{
  const goal = 'organize a 3-person sales team';
  const expected = ['discover', 'draft', 'review'];
  const content = 'agents: []\nskills: []\n';
  const prompt = composeCritiquePrompt('team.blueprint.yml', content, fakeLint, goal, expected);

  assert(prompt.includes(goal), 'prompt embeds user goal');
  assert(prompt.includes('discover'), 'prompt mentions expected step "discover"');
  assert(prompt.includes('missing-required-field'), 'prompt embeds lint rule');
  assert(prompt.includes('review (running, no done!)'), 'prompt flags incomplete step');
  assert(prompt.includes('<sf:critique>'), 'prompt asks for <sf:critique> envelope');
  for (const dim of CRITIQUE_DIMENSIONS) {
    assert(prompt.includes(dim), `prompt mentions dimension ${dim}`);
  }
}

console.log('--- parseCritique (success) ---');
{
  const raw = `Here is my critique:
<sf:critique>
{
  "goal_achievement": {"score": 7, "rationale": "covers core goal", "improvement": "add CRM check"},
  "skill_completeness": {"score": 4, "rationale": "review never done", "improvement": "complete review"},
  "structural_integrity": {"score": 5, "rationale": "missing policy_matrix", "improvement": "add it"},
  "reference_grounding": {"score": 8, "rationale": "uses sides", "improvement": "deeper refs"},
  "anti_pattern_free": {"score": 9, "rationale": "no fluff", "improvement": "n/a"},
  "overall_summary": "Decent but incomplete."
}
</sf:critique>`;
  const out = parseCritique(raw, { errors: 1, warnings: 1, infos: 0 });
  assert(out.scores !== null, 'scores not null');
  assert(out.scores?.goal_achievement?.score === 7, 'goal_achievement=7');
  assert(out.scores?.skill_completeness?.score === 4, 'skill_completeness=4');
  assert(out.scores?.policy_compliance === null, 'policy_compliance=null (this Story)');
  assert(out.overall_summary.startsWith('Decent'), 'overall_summary parsed');
  assert(out.error_code === undefined, 'no error_code on success');
}

console.log('--- parseCritique (missing tag) ---');
{
  const out = parseCritique('Sorry I forgot the format.', { errors: 0, warnings: 0, infos: 0 });
  assert(out.scores === null, 'scores=null when tag missing');
  assert(out.error_code === 'CRITIQUE_PARSE_FAILED', 'error_code=CRITIQUE_PARSE_FAILED');
}

console.log('--- parseCritique (invalid JSON) ---');
{
  const out = parseCritique('<sf:critique>not json {{{</sf:critique>', { errors: 0, warnings: 0, infos: 0 });
  assert(out.scores === null, 'scores=null when JSON invalid');
  assert(out.error_code === 'CRITIQUE_PARSE_FAILED', 'error_code=CRITIQUE_PARSE_FAILED');
}

console.log('--- parseCritique (clamps score 1..10) ---');
{
  const raw = `<sf:critique>{"goal_achievement":{"score":99,"rationale":"too high"}, "skill_completeness":{"score":-5,"rationale":"too low"}}</sf:critique>`;
  const out = parseCritique(raw, { errors: 0, warnings: 0, infos: 0 });
  assert(out.scores?.goal_achievement?.score === 10, '99→10');
  assert(out.scores?.skill_completeness?.score === 1, '-5→1');
}

async function main() {
  console.log('--- runCritique (no API key) ---');
  {
    const prevKey = process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    const out = await runCritique({
      session_id: 'mock',
      filename: 'team.blueprint.yml',
      user_goal: 'goal',
      expected_steps: ['a'],
      lintImpl: () => fakeLint,
      readArtifact: () => 'agents: []\n',
    });
    if (prevKey !== undefined) process.env.ANTHROPIC_API_KEY = prevKey;
    assert(out.scores === null, 'scores=null when no key');
    assert(out.error_code === 'CRITIQUE_NO_API_KEY', 'error_code=CRITIQUE_NO_API_KEY');
    assert(out.lint_summary.errors === 1, 'lint_summary still propagates');
  }

  console.log('--- runCritique (mock fetch 401) ---');
  {
    const mockFetch: typeof fetch = async () =>
      new Response(JSON.stringify({ error: { type: 'authentication_error' } }), {
        status: 401,
        headers: { 'content-type': 'application/json' },
      });
    const out = await runCritique({
      session_id: 'mock',
      filename: 'team.blueprint.yml',
      user_goal: 'goal',
      expected_steps: ['a'],
      anthropic_key: 'sk-fake',
      lintImpl: () => fakeLint,
      readArtifact: () => 'agents: []\n',
      fetchImpl: mockFetch,
    });
    assert(out.scores === null, 'scores=null on 401');
    assert(out.error_code === 'CRITIQUE_API_ERROR', `error_code=CRITIQUE_API_ERROR (got ${out.error_code})`);
  }

  console.log('--- runCritique (mock fetch success) ---');
  {
    const goodResponse = {
      content: [{
        type: 'text',
        text: `<sf:critique>{"goal_achievement":{"score":8,"rationale":"good"},"skill_completeness":{"score":5,"rationale":"ok"},"structural_integrity":{"score":6,"rationale":"ok"},"reference_grounding":{"score":7,"rationale":"ok"},"anti_pattern_free":{"score":9,"rationale":"ok"},"overall_summary":"all good"}</sf:critique>`,
      }],
    };
    const mockFetch: typeof fetch = async () =>
      new Response(JSON.stringify(goodResponse), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    const stages: string[] = [];
    const out = await runCritique({
      session_id: 'mock',
      filename: 'team.blueprint.yml',
      user_goal: 'goal',
      expected_steps: ['a'],
      anthropic_key: 'sk-fake',
      lintImpl: () => fakeLint,
      readArtifact: () => 'agents: []\n',
      fetchImpl: mockFetch,
    }, (stage) => stages.push(stage));
    assert(out.scores !== null, 'scores not null on success');
    assert(out.scores?.goal_achievement?.score === 8, 'goal_achievement=8');
    assert(out.error_code === undefined, 'no error_code on success');
    assert(stages.includes('lint'), 'emit lint stage');
    assert(stages.includes('streaming'), 'emit streaming stage');
    assert(stages.includes('done'), 'emit done stage');
    assert(typeof out.duration_ms === 'number' && out.duration_ms >= 0, 'duration_ms set');
  }

  console.log('--- runCritique (artifact read failure) ---');
  {
    const out = await runCritique({
      session_id: 'mock',
      filename: 'missing.yml',
      user_goal: 'goal',
      expected_steps: [],
      anthropic_key: 'sk-fake',
      lintImpl: () => { throw Object.assign(new Error('ARTIFACT_NOT_FOUND'), { code: 'ARTIFACT_NOT_FOUND' }); },
    });
    assert(out.scores === null, 'scores=null on read failure');
    assert(out.error_code === 'CRITIQUE_FAILED', 'error_code=CRITIQUE_FAILED');
  }

  console.log('---');
  if (failed > 0) {
    console.error(`${failed} assertion(s) failed`);
    process.exit(1);
  } else {
    console.log('All critic assertions passed.');
  }
}

main().catch((err: unknown) => {
  const out = err as CritiqueOutput;
  console.error('UNCAUGHT:', out);
  process.exit(1);
});

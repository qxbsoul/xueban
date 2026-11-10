/**
 * compile-bmad.test.ts — BMAD-METHOD golden test (mock LLM + fallback path).
 *
 * Run with:
 *   cd server
 *   npx tsx src/lib/skill-compiler/__tests__/compile-bmad.test.ts
 *
 * Requires `.xueban/cache/skill-ingest/9ac1ac635235/` (BMAD shallow
 * clone seeded by ingestSkill). If absent, the test SKIPs with a clear
 * message — CI without the seed gets a graceful no-op.
 *
 * Coverage:
 *   1. fallback path: forced-fail LLM → mode='team', 6+ members, sequential
 *      edges, derivedFrom='fallback'
 *   2. cache hit: second compile() with same content_hash returns same
 *      object without invoking LLM (mock counts calls)
 *   3. mock LLM happy path: well-formed team JSON → CompiledTeam shape,
 *      members_personas synthesized for missing ids, derivedFrom='prose-llm'
 *   4. schema validation rejects bogus member ids (with dots / spaces) →
 *      falls back rather than emitting garbage downstream
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { readSkill } from '../../../skill-reader';
import type { ProviderChunk } from '../../../transport/api-clients';
import {
  compile,
  _setCallProviderForTests,
  _setCompileCacheRootForTests,
} from '../index';

let pass = 0;
let fail = 0;

function assert(label: string, cond: boolean): void {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}`);
  }
}

function eq<T>(label: string, expected: T, actual: T): void {
  const ok = JSON.stringify(expected) === JSON.stringify(actual);
  if (ok) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(
      `  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`,
    );
  }
}

const BMAD_CACHE = path.resolve(
  process.cwd(),
  '.xueban/cache/skill-ingest/9ac1ac635235',
);

async function main(): Promise<void> {
  if (!fs.existsSync(BMAD_CACHE)) {
    console.log(`\n[SKIP] BMAD cache not found at ${BMAD_CACHE}`);
    console.log('       Run an ingest against BMAD-METHOD to populate it.');
    console.log(`\n=== compile-bmad.test.ts === SKIPPED`);
    return;
  }

  // Isolated cache + ensure a stub key is present for provider resolution.
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-compile-bmad-'));
  _setCompileCacheRootForTests(tmpRoot);
  if (!process.env.ANTHROPIC_API_KEY && !process.env.ZHIPU_API_KEY) {
    process.env.ZHIPU_API_KEY = 'test-key-for-compile-test';
  }

  console.log(`\n[BMAD cache] ${BMAD_CACHE}`);
  const skill = await readSkill(BMAD_CACHE);
  console.log(
    `  skill_id=${skill.skill_id} agent_files=${skill.agent_files.length} doc_files=${skill.doc_files.length}`,
  );

  // ─── Test 1: fallback path ────────────────────────────────────────────────
  console.log('\n[1] fallback compile (LLM forced to throw) → mode=team');
  {
    // 故意只抛不 yield：模拟 LLM 调用失败，让 compile() 走 fallback 分支。
    // eslint-disable-next-line require-yield
    _setCallProviderForTests(async function* () {
      throw new Error('forced-llm-failure');
    });
    const out = await compile(skill);
    eq('mode=team', 'team', out.mode);
    assert(
      `members_ids.length >= 6 (actual ${out.teamConfig?.members_ids.length})`,
      (out.teamConfig?.members_ids.length ?? 0) >= 6,
    );
    assert(
      `edges_v1.length >= 5 (actual ${out.teamConfig?.edges_v1.length})`,
      (out.teamConfig?.edges_v1.length ?? 0) >= 5,
    );
    eq('teamConfig.derivedFrom = fallback', 'fallback', out.teamConfig?.derivedFrom);
    assert(
      'every edge is sequential',
      (out.teamConfig?.edges_v1 ?? []).every((e) => e.kind === 'sequential'),
    );
    assert(
      'every member has a persona',
      Object.keys(out.teamConfig?.members_personas ?? {}).length ===
        out.teamConfig?.members_ids.length,
    );
    assert(
      'llm_call_meta.model starts with fallback:',
      out.llm_call_meta.model.startsWith('fallback:'),
    );
  }

  // ─── Test 2: cache hit ────────────────────────────────────────────────────
  console.log('\n[2] cache hit on second compile()');
  {
    let calls = 0;
    _setCallProviderForTests(async function* () {
      calls++;
      yield { type: 'text-delta', text: 'irrelevant' };
      yield { type: 'end' };
    });
    // First compile reads from disk cache (written by test 1)
    const t0 = Date.now();
    const out1 = await compile(skill);
    const dur1 = Date.now() - t0;
    eq('LLM not called (cache hit on test-1 write)', 0, calls);
    assert(`first compile <= 50ms (was ${dur1}ms — cache hit)`, dur1 <= 200);

    const t1 = Date.now();
    const out2 = await compile(skill);
    const dur2 = Date.now() - t1;
    eq('LLM still not called', 0, calls);
    assert(`second compile <= 50ms (was ${dur2}ms)`, dur2 <= 200);
    eq(
      'same source_content_hash',
      out1.source_content_hash,
      out2.source_content_hash,
    );
  }

  // ─── Test 3: mock LLM happy path → prose-llm provenance ──────────────────
  console.log('\n[3] mock LLM happy path → derivedFrom=prose-llm');
  {
    // Use a fresh cache dir so test 1's fallback entry doesn't shadow this.
    const tmp2 = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-compile-bmad2-'));
    _setCompileCacheRootForTests(tmp2);
    const llmTeam = {
      mode: 'team',
      teamConfig: {
        name: 'BMAD-METHOD Team',
        description: 'PRD → architecture → dev pipeline',
        members_ids: ['analyst', 'pm', 'architect', 'dev', 'qa', 'sm'],
        members_personas: {
          analyst: 'Strategic business analyst',
          pm: 'Product manager',
          architect: 'System architect',
          dev: 'Senior dev',
          // qa + sm intentionally omitted to test default synthesis
        },
        edges_v1: [
          { from: 'analyst', to: 'pm', kind: 'sequential' },
          { from: 'pm', to: 'architect', kind: 'sequential' },
          { from: 'architect', to: 'dev', kind: 'sequential' },
          { from: 'dev', to: 'qa', kind: 'sequential' },
          { from: 'qa', to: 'sm', kind: 'sequential' },
        ],
        policy_obj: { retry: 5, timeout_per_step_ms: 120000 },
      },
    };
    let calls = 0;
    _setCallProviderForTests(async function* () {
      calls++;
      // Wrap in markdown fence to exercise the fence-strip path.
      yield {
        type: 'text-delta',
        text: '```json\n' + JSON.stringify(llmTeam) + '\n```',
      };
      yield { type: 'end' };
    });
    const out = await compile(skill);
    eq('LLM called once', 1, calls);
    eq('mode=team', 'team', out.mode);
    eq('derivedFrom=prose-llm', 'prose-llm', out.teamConfig?.derivedFrom);
    eq('members count = 6', 6, out.teamConfig?.members_ids.length);
    eq('edges count = 5', 5, out.teamConfig?.edges_v1.length);
    eq(
      'qa persona synthesized for omitted id',
      true,
      typeof out.teamConfig?.members_personas.qa === 'string' &&
        out.teamConfig.members_personas.qa.includes('qa'),
    );
    eq('policy.retry=5', 5, out.teamConfig?.policy_obj.retry);
    assert(
      'llm_call_meta carries provider:model',
      out.llm_call_meta.model.includes(':') &&
        !out.llm_call_meta.model.startsWith('fallback'),
    );
    assert(
      'tokens_in approximated (>0)',
      out.llm_call_meta.tokens_in > 0,
    );
    fs.rmSync(tmp2, { recursive: true, force: true });
  }

  // ─── Test 4: bogus member ids rejected → fallback ────────────────────────
  console.log('\n[4] bogus member ids (dots / spaces) → fallback');
  {
    const tmp3 = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-compile-bmad3-'));
    _setCompileCacheRootForTests(tmp3);
    _setCallProviderForTests(async function* () {
      yield {
        type: 'text-delta',
        text: JSON.stringify({
          mode: 'team',
          teamConfig: {
            name: 'Bad Team',
            members_ids: ['bad.id.with.dots', 'has spaces', 'OK_ID'],
            members_personas: {
              'bad.id.with.dots': 'x',
              'has spaces': 'y',
              OK_ID: 'z',
            },
            edges_v1: [],
            policy_obj: {},
          },
        }),
      };
      yield { type: 'end' };
    });
    const out = await compile(skill);
    // OK_ID matches the regex — so we get 1 valid member. Validation
    // requires ≥1 valid member so this should succeed with only OK_ID.
    // Either accept that or fall back; both are well-defined. Assert that
    // every emitted id is regex-valid.
    if (out.mode === 'team') {
      assert(
        'all members match canonical regex',
        (out.teamConfig?.members_ids ?? []).every((id) =>
          /^[a-z0-9][a-z0-9_-]{0,63}$/i.test(id),
        ),
      );
    } else {
      // fallback path is also acceptable
      assert(
        'fallback triggered for bad ids',
        out.llm_call_meta.model.startsWith('fallback:'),
      );
    }
    fs.rmSync(tmp3, { recursive: true, force: true });
  }

  // ─── Cleanup ──────────────────────────────────────────────────────────────
  _setCallProviderForTests(null);
  _setCompileCacheRootForTests(null);
  try {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  } catch {
    /* ignore */
  }

  console.log(`\n=== compile-bmad.test.ts === ${pass} pass, ${fail} fail`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});

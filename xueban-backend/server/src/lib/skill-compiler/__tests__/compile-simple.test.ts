/**
 * compile-simple.test.ts — synthetic skill → compile() round-trip.
 *
 * Run with:
 *   cd server
 *   npx tsx src/lib/skill-compiler/__tests__/compile-simple.test.ts
 *
 * No framework — mirrors PR-A's `parse-simple.test.ts` pattern. Covers:
 *
 *   1. Mocked LLM returning a well-formed AGENT JSON → CompiledSkill.mode=agent
 *   2. Cache hit on second compile() (no LLM re-invocation)
 *   3. LLM returns malformed JSON → fallback compile (mode=agent)
 *   4. LLM returns schema-invalid JSON (mode missing) → fallback
 *   5. LLM error chunk → fallback, no throw
 *   6. Single agent_file skill → fallback mode is 'agent' not 'team'
 *   7. Empty agent_files skill → fallback mode is 'agent' (zero handled)
 *
 * Each test uses an isolated tmp cache root so they don't pollute each
 * other. LLM is a stub via `_setCallProviderForTests`.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { computeContentHash } from '../../../skill-reader/cache';
import type { SkillReadOutput } from '../../../skill-reader/types';
import type { ProviderChunk, ProviderInput } from '../../../transport/api-clients';
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

/** Build a fake SkillReadOutput with a real, stable content hash. */
function makeSkill(opts: {
  skill_id: string;
  raw_skill_md?: string;
  agent_files?: Array<{ path: string; raw: string }>;
}): SkillReadOutput {
  const raw_skill_md = opts.raw_skill_md ?? '';
  const agent_files = (opts.agent_files ?? []).map((f) => ({
    path: f.path,
    raw: f.raw,
    frontmatter: null,
  }));
  const all: Array<{ path: string; raw: string }> = [];
  if (raw_skill_md) all.push({ path: 'SKILL.md', raw: raw_skill_md });
  for (const f of agent_files) all.push({ path: f.path, raw: f.raw });
  return {
    skill_id: opts.skill_id,
    content_hash: computeContentHash(all),
    raw_skill_md,
    agent_files,
    workflow_files: [],
    doc_files: [],
  };
}

/** Make a stub callProvider that yields text deltas + an end chunk. */
function stubLlm(text: string): {
  fn: (provider: string, input: ProviderInput) => AsyncGenerator<ProviderChunk>;
  calls: number;
} {
  let calls = 0;
  async function* fn(): AsyncGenerator<ProviderChunk> {
    calls++;
    yield { type: 'text-delta', text };
    yield { type: 'end' };
  }
  return {
    // Wrap to expose `calls` after each invocation
    fn: (() => {
      const gen = async function* (): AsyncGenerator<ProviderChunk> {
        calls++;
        yield { type: 'text-delta', text };
        yield { type: 'end' };
      };
      return gen as unknown as (
        p: string,
        i: ProviderInput,
      ) => AsyncGenerator<ProviderChunk>;
    })(),
    get calls() {
      return calls;
    },
  } as { fn: (p: string, i: ProviderInput) => AsyncGenerator<ProviderChunk>; calls: number };
}

/** Inject an env API key + ensure provider resolution works (Zhipu fallback). */
function ensureKeyEnv(): void {
  if (!process.env.ANTHROPIC_API_KEY && !process.env.ZHIPU_API_KEY) {
    process.env.ZHIPU_API_KEY = 'test-key-for-compile-test';
  }
}

async function main(): Promise<void> {
  // ─── Setup: isolated cache root + env key ──────────────────────────────────
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-compile-'));
  _setCompileCacheRootForTests(tmpRoot);
  ensureKeyEnv();

  // ─── Test 1: mock LLM returns well-formed AGENT JSON ──────────────────────
  console.log('\n[1] mock LLM → agent JSON');
  {
    const skill = makeSkill({
      skill_id: 'paper-review-quick',
      raw_skill_md: '# Paper Review\n\nReview academic papers.',
      agent_files: [
        { path: 'agents/reviewer.md', raw: '# Reviewer\nSeasoned researcher.' },
      ],
    });
    const llmJson = JSON.stringify({
      mode: 'agent',
      agentConfig: {
        persona: 'Seasoned academic reviewer.',
        system_prompt: 'You are a paper reviewer.',
        tools: ['read_file', 'web_search'],
        max_iterations: 30,
      },
    });
    let calls = 0;
    _setCallProviderForTests(async function* () {
      calls++;
      yield { type: 'text-delta', text: llmJson };
      yield { type: 'end' };
    });
    const out = await compile(skill);
    eq('mode=agent', 'agent', out.mode);
    assert('agentConfig present', !!out.agentConfig);
    assert('teamConfig absent', !out.teamConfig);
    eq('persona matches', 'Seasoned academic reviewer.', out.agentConfig?.persona);
    eq(
      'tools = [read_file, web_search]',
      ['read_file', 'web_search'],
      out.agentConfig?.tools,
    );
    eq('max_iterations = 30', 30, out.agentConfig?.max_iterations);
    eq('LLM called once', 1, calls);
  }

  // ─── Test 2: cache hit on second compile() ─────────────────────────────────
  console.log('\n[2] cache hit — second compile() does not call LLM');
  {
    const skill = makeSkill({
      skill_id: 'cached-skill',
      raw_skill_md: '# Cached',
      agent_files: [{ path: 'agents/a.md', raw: '# A' }],
    });
    const llmJson = JSON.stringify({
      mode: 'agent',
      agentConfig: {
        persona: 'A',
        system_prompt: 'You are A.',
        tools: [],
      },
    });
    let calls = 0;
    _setCallProviderForTests(async function* () {
      calls++;
      yield { type: 'text-delta', text: llmJson };
      yield { type: 'end' };
    });
    const out1 = await compile(skill);
    const out2 = await compile(skill);
    eq('first call count = 1', 1, calls);
    eq('second call count = 1 (cache hit)', 1, calls);
    eq('outputs identical content_hash', out1.source_content_hash, out2.source_content_hash);
    eq('outputs identical mode', out1.mode, out2.mode);
  }

  // ─── Test 3: malformed JSON → fallback ────────────────────────────────────
  console.log('\n[3] malformed LLM JSON → fallback compile (no throw)');
  {
    const skill = makeSkill({
      skill_id: 'malformed-test',
      raw_skill_md: '# Malformed',
      agent_files: [{ path: 'agents/x.md', raw: '# X' }],
    });
    _setCallProviderForTests(async function* () {
      yield { type: 'text-delta', text: '{ not valid json at all' };
      yield { type: 'end' };
    });
    const out = await compile(skill);
    eq('mode=agent (single agent_file)', 'agent', out.mode);
    assert(
      'llm_call_meta.model starts with fallback:',
      out.llm_call_meta.model.startsWith('fallback:'),
    );
    assert(
      'reason captured',
      out.llm_call_meta.model.includes('json-parse-failed') ||
        out.llm_call_meta.model.includes('schema-invalid'),
    );
  }

  // ─── Test 4: schema-invalid (mode missing) → fallback ─────────────────────
  console.log('\n[4] schema-invalid JSON (mode missing) → fallback');
  {
    const skill = makeSkill({
      skill_id: 'no-mode-test',
      raw_skill_md: '# Test',
      agent_files: [{ path: 'agents/y.md', raw: '# Y' }],
    });
    _setCallProviderForTests(async function* () {
      yield { type: 'text-delta', text: JSON.stringify({ foo: 'bar' }) };
      yield { type: 'end' };
    });
    const out = await compile(skill);
    assert(
      'llm_call_meta.model = fallback:schema-invalid',
      out.llm_call_meta.model === 'fallback:schema-invalid',
    );
  }

  // ─── Test 5: LLM error chunk → fallback ───────────────────────────────────
  console.log('\n[5] LLM error chunk → fallback (no throw)');
  {
    const skill = makeSkill({
      skill_id: 'err-test',
      raw_skill_md: '# Err',
      agent_files: [{ path: 'agents/z.md', raw: '# Z' }],
    });
    _setCallProviderForTests(async function* () {
      yield {
        type: 'error',
        message: 'simulated rate limit',
        code: 'RATE_LIMITED',
      };
    });
    const out = await compile(skill);
    assert(
      'llm_call_meta.model = fallback:llm-error:RATE_LIMITED',
      out.llm_call_meta.model === 'fallback:llm-error:RATE_LIMITED',
    );
  }

  // ─── Test 6: single agent_file → fallback agent mode ──────────────────────
  console.log('\n[6] single agent_file → fallback agent mode');
  {
    const skill = makeSkill({
      skill_id: 'single-agent-fallback',
      raw_skill_md: '# Single Agent',
      agent_files: [{ path: 'agents/lone.md', raw: '# Lone wolf' }],
    });
    // 故意只抛不 yield：模拟网络中断，验证 fallback 到 agent 模式。
    // eslint-disable-next-line require-yield
    _setCallProviderForTests(async function* () {
      throw new Error('simulated network drop');
    });
    const out = await compile(skill);
    eq('mode=agent', 'agent', out.mode);
    assert('agentConfig.tools has read_file', out.agentConfig?.tools.includes('read_file') ?? false);
  }

  // ─── Test 7: empty agent_files skill → fallback agent mode ────────────────
  console.log('\n[7] empty agent_files → fallback agent mode (graceful)');
  {
    const skill = makeSkill({
      skill_id: 'empty-skill',
      raw_skill_md: '# Empty\nNo agents declared.',
      agent_files: [],
    });
    // 故意只抛不 yield：验证 provider 失败时仍能产出可用的 agent 配置。
    // eslint-disable-next-line require-yield
    _setCallProviderForTests(async function* () {
      throw new Error('forced');
    });
    const out = await compile(skill);
    eq('mode=agent', 'agent', out.mode);
    assert('persona non-empty', (out.agentConfig?.persona ?? '').length > 0);
  }

  // ─── Cleanup ──────────────────────────────────────────────────────────────
  _setCallProviderForTests(null);
  _setCompileCacheRootForTests(null);
  try {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  } catch {
    /* ignore */
  }

  console.log(`\n=== compile-simple.test.ts === ${pass} pass, ${fail} fail`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});

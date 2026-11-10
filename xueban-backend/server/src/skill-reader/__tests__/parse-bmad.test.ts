/**
 * parse-bmad.test.ts — golden test against the real BMAD-METHOD shallow clone.
 *
 * Run with:
 *   cd server
 *   npx tsx src/skill-reader/__tests__/parse-bmad.test.ts
 *
 * Requires: `.xueban/cache/skill-ingest/9ac1ac635235/` to exist (a prior
 * `ingestSkill('https://github.com/bmad-code-org/BMAD-METHOD')` populates it).
 * If the cache dir is absent the test SKIPs with a clear message rather than
 * failing — CI environments without the seed get a graceful no-op.
 *
 * Coverage (updated 2026-06-01 — reader now discovers REAL nested agents,
 * not synthesized bmad-modules.yaml entries):
 *   1. readSkill returns ≥1 REAL agent_file (the bmad-agent-* personas)
 *   2. doc_files contains README.md (and other top-level prose)
 *   3. raw of doc_file matches fs.readFileSync byte-for-byte
 *   4. workflow_files is 0 (BMAD has no root-level workflows/ dir)
 *   5. Agent paths are real on-disk files, NOT `*.synthesized.md`, and at
 *      least one bmad-agent-* persona is present
 *   6. content_hash is stable across two calls
 */

import fs from 'fs';
import path from 'path';
import { readSkill } from '../index';

let pass = 0;
let fail = 0;

function assert(label: string, cond: boolean) {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}`);
  }
}

function eq<T>(label: string, expected: T, actual: T) {
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

// Path is computed relative to the server cwd (the test is run from `server/`
// per the `npx tsx` convention) so it works whether you run from project root
// or from inside server/.
const BMAD_CACHE = path.resolve(
  process.cwd(),
  '.xueban/cache/skill-ingest/9ac1ac635235',
);

async function main() {
  if (!fs.existsSync(BMAD_CACHE)) {
    console.log(`\n[SKIP] BMAD cache not found at ${BMAD_CACHE}`);
    console.log('       Run an ingest against BMAD-METHOD to populate it.');
    console.log(`\n=== parse-bmad.test.ts ===  SKIPPED`);
    return;
  }

  console.log(`\n[BMAD cache] ${BMAD_CACHE}`);
  const out = await readSkill(BMAD_CACHE);

  // ── Test 1: REAL discovered agents ────────────────────────────────────
  console.log('\n[1] real agent_files discovered (bmad-agent-* personas)');
  assert('agent_files.length >= 1', out.agent_files.length >= 1);
  // BMAD ships its phase personas under src/bmm-skills/*/bmad-agent-*/SKILL.md.
  // Loosely assert ≥3 to allow upstream churn without breaking the test.
  assert(
    `agent_files.length >= 3 (actual: ${out.agent_files.length})`,
    out.agent_files.length >= 3,
  );
  assert(
    'NONE are synthesized placeholders (no *.synthesized.md)',
    out.agent_files.every((a) => !/\.synthesized\.md$/.test(a.path)),
  );

  // ── Test 2: doc_files include README ──────────────────────────────────
  console.log('\n[2] doc_files include README');
  const readmePaths = out.doc_files.map((d) => d.path);
  assert(
    `doc_files includes README.md (paths: ${readmePaths.join(', ')})`,
    readmePaths.some((p) => /^readme.*\.md$/i.test(p)),
  );
  assert(
    `doc_files.length >= 2 (saw ${out.doc_files.length})`,
    out.doc_files.length >= 2,
  );

  // ── Test 3: raw is byte-equal for an arbitrary doc ───────────────────
  console.log('\n[3] doc_files raw is byte-equal to disk');
  const readme = out.doc_files.find((d) => /^readme\.md$/i.test(d.path));
  if (readme) {
    const onDisk = fs.readFileSync(path.join(BMAD_CACHE, readme.path), 'utf-8');
    eq('README.md raw === fs.readFileSync', onDisk, readme.raw);
  } else {
    console.log('  SKIP  no README.md in doc_files (case mismatch?)');
  }

  // ── Test 4: workflows empty at root ──────────────────────────────────
  console.log('\n[4] workflow_files is empty (BMAD has no root workflows/)');
  eq('workflow_files.length === 0', 0, out.workflow_files.length);

  // ── Test 5: real agent paths (not synthesized) ───────────────────────
  console.log('\n[5] agent paths are real on-disk files; ≥1 bmad-agent-* present');
  assert(
    `at least one bmad-agent-* persona (paths: ${out.agent_files.map((a) => a.path).slice(0, 8).join(', ')})`,
    out.agent_files.some((a) => /bmad-agent-[^/]+\/skill\.md$/i.test(a.path)),
  );
  for (const a of out.agent_files) {
    const onDisk = fs.existsSync(path.join(BMAD_CACHE, a.path));
    assert(`real file on disk: ${a.path}`, onDisk);
  }

  // ── Test 6: content_hash deterministic ───────────────────────────────
  console.log('\n[6] content_hash deterministic across two calls');
  const second = await readSkill(BMAD_CACHE);
  eq('hash matches across calls', out.content_hash, second.content_hash);
  assert('hash is 64-char hex', /^[0-9a-f]{64}$/.test(out.content_hash));

  // ── Diagnostics ──────────────────────────────────────────────────────
  console.log('\n[diagnostics]');
  console.log(`  skill_id        : ${out.skill_id}`);
  console.log(`  content_hash    : ${out.content_hash}`);
  console.log(`  raw_skill_md    : ${out.raw_skill_md.length} bytes`);
  console.log(`  agent_files     : ${out.agent_files.length}`);
  console.log(`  workflow_files  : ${out.workflow_files.length}`);
  console.log(`  doc_files       : ${out.doc_files.length}`);
  console.log(`  sample agents   : ${out.agent_files.slice(0, 3).map((a) => a.path).join(', ')}`);
  console.log(`  sample docs     : ${out.doc_files.slice(0, 5).map((d) => d.path).join(', ')}`);

  console.log(`\n=== parse-bmad.test.ts ===  ${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

main().catch((err) => {
  console.error('Test crashed:', err);
  process.exit(2);
});

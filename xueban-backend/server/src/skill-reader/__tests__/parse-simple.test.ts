/**
 * parse-simple.test.ts — minimal synthetic skill walk-through.
 *
 * Run with:
 *   cd server
 *   npx tsx src/skill-reader/__tests__/parse-simple.test.ts
 *
 * No framework — mirrors timeline-projector.test.ts pattern (the server
 * package doesn't ship vitest). Coverage:
 *
 *   1. SKILL.md + agents/foo.md → 1 agent_file, raw_skill_md non-empty
 *   2. frontmatter on agents/foo.md is parsed
 *   3. content_hash is deterministic across two calls on same disk state
 *   4. content_hash changes when any file content changes
 *   5. Missing dir → empty SkillReadOutput (no throws)
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { readSkill } from '../index';
import { computeContentHash } from '../cache';

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

/** Build a throw-away skill dir under the OS temp root. */
function makeTempSkill(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-skill-reader-test-'));
  fs.mkdirSync(path.join(dir, 'agents'));
  fs.writeFileSync(
    path.join(dir, 'SKILL.md'),
    '---\nname: simple-test\n---\n\n# Simple Test Skill\n\nBody.\n',
    'utf-8',
  );
  fs.writeFileSync(
    path.join(dir, 'agents', 'foo.md'),
    '---\nid: foo\nrole: analyst\n---\n\n# Foo Agent\n\nFoo body.\n',
    'utf-8',
  );
  return dir;
}

async function main() {
  // ── Test 1: basic shape ───────────────────────────────────────────────
  console.log('\n[1] basic shape (SKILL.md + agents/foo.md)');
  {
    const dir = makeTempSkill();
    try {
      const out = await readSkill(dir);
      assert('agent_files length === 1', out.agent_files.length === 1);
      assert('agent_files[0].path is agents/foo.md', out.agent_files[0]?.path === 'agents/foo.md');
      assert('raw_skill_md non-empty', out.raw_skill_md.length > 0);
      eq('workflow_files length === 0', 0, out.workflow_files.length);
      assert('content_hash is hex string', /^[0-9a-f]{64}$/.test(out.content_hash));
      assert('skill_id matches dir basename', out.skill_id === path.basename(dir));
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  // ── Test 2: frontmatter parsed on .md ────────────────────────────────
  console.log('\n[2] frontmatter parsed on agents/*.md');
  {
    const dir = makeTempSkill();
    try {
      const out = await readSkill(dir);
      const fm = out.agent_files[0]?.frontmatter;
      assert('frontmatter not null', fm !== null && fm !== undefined);
      assert('frontmatter.id === foo', fm?.id === 'foo');
      assert('frontmatter.role === analyst', fm?.role === 'analyst');
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  // ── Test 3: content_hash is deterministic ────────────────────────────
  console.log('\n[3] content_hash deterministic across two calls');
  {
    const dir = makeTempSkill();
    try {
      const a = await readSkill(dir);
      const b = await readSkill(dir);
      eq('two calls → same hash', a.content_hash, b.content_hash);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  // ── Test 4: content_hash changes on file mutation ────────────────────
  console.log('\n[4] content_hash busts on mutation');
  {
    const dir = makeTempSkill();
    try {
      const a = await readSkill(dir);
      fs.writeFileSync(
        path.join(dir, 'agents', 'foo.md'),
        '---\nid: foo\nrole: analyst\n---\n\n# Foo Agent v2\n\nMutated.\n',
        'utf-8',
      );
      const b = await readSkill(dir);
      assert('hash differs after mutation', a.content_hash !== b.content_hash);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  // ── Test 5: missing dir → empty output ───────────────────────────────
  console.log('\n[5] missing dir → empty output (no throw)');
  {
    const phantom = path.join(os.tmpdir(), 'sf-skill-reader-does-not-exist-' + Date.now());
    const out = await readSkill(phantom);
    eq('agent_files empty', [], out.agent_files);
    eq('workflow_files empty', [], out.workflow_files);
    eq('doc_files empty', [], out.doc_files);
    eq('raw_skill_md empty', '', out.raw_skill_md);
    assert('content_hash matches empty input hash', out.content_hash === computeContentHash([]));
  }

  // ── Test 6: raw is byte-equal ────────────────────────────────────────
  console.log('\n[6] raw is byte-equal to fs.readFileSync');
  {
    const dir = makeTempSkill();
    try {
      const out = await readSkill(dir);
      const disk = fs.readFileSync(path.join(dir, 'agents', 'foo.md'), 'utf-8');
      eq('agent_files[0].raw === fs.readFileSync(foo.md)', disk, out.agent_files[0]?.raw);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  console.log(`\n=== parse-simple.test.ts ===  ${pass} pass, ${fail} fail`);
  if (fail > 0) process.exit(1);
}

main().catch((err) => {
  console.error('Test crashed:', err);
  process.exit(2);
});

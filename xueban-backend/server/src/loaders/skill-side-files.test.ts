/**
 * skill-side-files.test.ts — standalone tests for loadSkillSideFiles (Story 15.12)
 *
 * Run with:  npx tsx src/loaders/skill-side-files.test.ts   (from server/)
 *
 * Pattern mirrors skill-loader.test.ts — no jest/vitest dependency. Each
 * `check` prints PASS/FAIL; the process exits non-zero if any check fails.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import { loadSkillSideFiles } from './skill-side-files';

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

function makeTmpRoot(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'sf-skill-side-'));
}

function rmrf(p: string) {
  try {
    fs.rmSync(p, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}

function writeFile(root: string, rel: string, content: string | Buffer) {
  const abs = path.join(root, rel);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  fs.writeFileSync(abs, content);
}

/** Capture console.warn calls for assertions, restore on done(). */
function captureWarn(): { warns: string[]; restore: () => void } {
  const warns: string[] = [];
  const original = console.warn;
  console.warn = (...args: unknown[]) => {
    warns.push(args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' '));
  };
  return {
    warns,
    restore: () => {
      console.warn = original;
    },
  };
}

// ─── Test 1: missing skill dir → empty result, no warn (AC5) ────────────────

(function testMissingSkillDir() {
  console.log('\n[1] missing skill dir → empty');
  const root = makeTmpRoot();
  const cap = captureWarn();
  try {
    const res = loadSkillSideFiles('does-not-exist', root);
    check('prompt empty', res.prompt === '');
    check('files empty', res.files.length === 0);
    check('not truncated', res.truncated === false);
    check('no warnings', cap.warns.length === 0, cap.warns);
  } finally {
    cap.restore();
    rmrf(root);
  }
})();

// ─── Test 2: skill dir exists but no assets/references subdirs (AC5) ────────

(function testSkillDirNoSubdirs() {
  console.log('\n[2] skill dir exists, no assets/references → empty');
  const root = makeTmpRoot();
  const cap = captureWarn();
  try {
    fs.mkdirSync(path.join(root, 'sk1'));
    writeFile(root, 'sk1/SKILL.md', '# nothing');
    const res = loadSkillSideFiles('sk1', root);
    check('prompt empty', res.prompt === '');
    check('files empty', res.files.length === 0);
    check('not truncated', res.truncated === false);
    check('no warnings', cap.warns.length === 0, cap.warns);
  } finally {
    cap.restore();
    rmrf(root);
  }
})();

// ─── Test 3: empty subdirs → empty result (AC5) ─────────────────────────────

(function testEmptySubdirs() {
  console.log('\n[3] empty assets/ + references/ → empty result');
  const root = makeTmpRoot();
  try {
    fs.mkdirSync(path.join(root, 'sk1', 'assets'), { recursive: true });
    fs.mkdirSync(path.join(root, 'sk1', 'references'), { recursive: true });
    const res = loadSkillSideFiles('sk1', root);
    check('prompt empty', res.prompt === '');
    check('files empty', res.files.length === 0);
  } finally {
    rmrf(root);
  }
})();

// ─── Test 4: assets only loads, references absent (AC5 partial) ─────────────

(function testAssetsOnly() {
  console.log('\n[4] only assets/ present, no references/');
  const root = makeTmpRoot();
  try {
    writeFile(root, 'sk1/assets/template.html', '<html>hello</html>');
    const res = loadSkillSideFiles('sk1', root);
    check('1 file', res.files.length === 1);
    check('rel path correct', res.files[0]?.relPath === 'assets/template.html');
    check('type html', res.files[0]?.type === 'html');
    check(
      'prompt has Reference header',
      res.prompt.includes('## Reference: assets/template.html'),
    );
    check('prompt has body', res.prompt.includes('<html>hello</html>'));
  } finally {
    rmrf(root);
  }
})();

// ─── Test 5: assets/ before references/ + alpha order (AC1) ─────────────────

(function testOrder() {
  console.log('\n[5] assets/ before references/, alpha within each');
  const root = makeTmpRoot();
  try {
    writeFile(root, 'sk1/assets/b.md', 'asset-b');
    writeFile(root, 'sk1/assets/a.md', 'asset-a');
    writeFile(root, 'sk1/references/y.md', 'ref-y');
    writeFile(root, 'sk1/references/x.md', 'ref-x');
    const res = loadSkillSideFiles('sk1', root);
    check('4 files', res.files.length === 4);
    const order = res.files.map((f) => f.relPath);
    check(
      'order: assets/a, assets/b, refs/x, refs/y',
      JSON.stringify(order) ===
        JSON.stringify([
          'assets/a.md',
          'assets/b.md',
          'references/x.md',
          'references/y.md',
        ]),
      order,
    );
    // Prompt sections should appear in same order, separated by blank line
    const aIdx = res.prompt.indexOf('asset-a');
    const bIdx = res.prompt.indexOf('asset-b');
    const xIdx = res.prompt.indexOf('ref-x');
    const yIdx = res.prompt.indexOf('ref-y');
    check(
      'prompt body order matches',
      aIdx >= 0 && aIdx < bIdx && bIdx < xIdx && xIdx < yIdx,
      { aIdx, bIdx, xIdx, yIdx },
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 6: extension whitelist (AC3) ──────────────────────────────────────

(function testExtensionWhitelist() {
  console.log('\n[6] extension whitelist filters .png / .js / .exe');
  const root = makeTmpRoot();
  const cap = captureWarn();
  try {
    writeFile(root, 'sk1/assets/template.html', '<html/>');
    writeFile(root, 'sk1/assets/data.json', '{"k":1}');
    writeFile(root, 'sk1/assets/logo.png', Buffer.from([0x89, 0x50, 0x4e, 0x47]));
    writeFile(root, 'sk1/assets/script.js', 'console.log(1)');
    writeFile(root, 'sk1/assets/run.exe', Buffer.from([0x4d, 0x5a]));
    writeFile(root, 'sk1/assets/notes.txt', 'plain text');
    writeFile(root, 'sk1/assets/styles.css', 'body{color:red}');
    const res = loadSkillSideFiles('sk1', root);
    const rels = res.files.map((f) => f.relPath).sort();
    check(
      'only whitelisted loaded',
      JSON.stringify(rels) ===
        JSON.stringify([
          'assets/data.json',
          'assets/notes.txt',
          'assets/styles.css',
          'assets/template.html',
        ]),
      rels,
    );
    check('no warn for skipped binaries', cap.warns.length === 0, cap.warns);
  } finally {
    cap.restore();
    rmrf(root);
  }
})();

// ─── Test 7: hidden files / dirs skipped ────────────────────────────────────

(function testHidden() {
  console.log('\n[7] dotfiles and dot-dirs skipped');
  const root = makeTmpRoot();
  try {
    writeFile(root, 'sk1/assets/.DS_Store', 'junk');
    writeFile(root, 'sk1/assets/.hidden.md', 'should not appear');
    writeFile(root, 'sk1/assets/visible.md', 'visible');
    writeFile(root, 'sk1/assets/.cache/inner.md', 'inside hidden dir');
    const res = loadSkillSideFiles('sk1', root);
    check('1 file', res.files.length === 1);
    check('only visible.md', res.files[0]?.relPath === 'assets/visible.md');
  } finally {
    rmrf(root);
  }
})();

// ─── Test 8: single file > 100KB → skip + warn (AC4) ────────────────────────

(function testOversizeSingle() {
  console.log('\n[8] single file > 100KB skipped + warn');
  const root = makeTmpRoot();
  const cap = captureWarn();
  try {
    const big = 'x'.repeat(101 * 1024); // 101 KB
    writeFile(root, 'sk1/assets/big.json', big);
    writeFile(root, 'sk1/assets/small.md', 'tiny');
    const res = loadSkillSideFiles('sk1', root);
    check(
      'big.json not in files',
      !res.files.some((f) => f.relPath === 'assets/big.json'),
      res.files.map((f) => f.relPath),
    );
    check(
      'small.md still loaded',
      res.files.some((f) => f.relPath === 'assets/small.md'),
    );
    check(
      'warn mentions skip + size',
      cap.warns.some(
        (w) =>
          w.includes('skip') &&
          w.includes('big.json') &&
          /102400|100KB/.test(w),
      ),
      cap.warns,
    );
    check('not marked truncated (only individual skip)', res.truncated === false);
  } finally {
    cap.restore();
    rmrf(root);
  }
})();

// ─── Test 9: total > 500KB → truncate + warn (AC4) ──────────────────────────

(function testOversizeTotal() {
  console.log('\n[9] total > 500KB truncates + warn + truncated:true');
  const root = makeTmpRoot();
  const cap = captureWarn();
  try {
    // 10 files × 60KB = 600KB. After ~8 files we hit 480KB; the 9th (60KB)
    // would push to 540KB > 500KB, so loader should truncate at file #9.
    const block = 'a'.repeat(60 * 1024);
    for (let i = 0; i < 10; i++) {
      writeFile(root, `sk1/assets/f${i}.md`, block);
    }
    const res = loadSkillSideFiles('sk1', root);
    check('truncated flag set', res.truncated === true);
    check(
      'fewer than 10 files loaded',
      res.files.length < 10,
      res.files.length,
    );
    // total accepted size ≤ 500KB
    const total = res.files.reduce((acc, f) => acc + f.size, 0);
    check('total <= 500KB', total <= 500 * 1024, total);
    check(
      'warn mentions truncated + total',
      cap.warns.some((w) => w.includes('truncated') && /500|512/.test(w)),
      cap.warns,
    );
  } finally {
    cap.restore();
    rmrf(root);
  }
})();

// ─── Test 10: 5 files × 60KB = 300KB → all load (AC4 boundary) ──────────────

(function testUnderTotalLimit() {
  console.log('\n[10] 5 × 60KB = 300KB → all 5 load, not truncated');
  const root = makeTmpRoot();
  try {
    const block = 'a'.repeat(60 * 1024);
    for (let i = 0; i < 5; i++) {
      writeFile(root, `sk1/assets/f${i}.md`, block);
    }
    const res = loadSkillSideFiles('sk1', root);
    check('5 files', res.files.length === 5);
    check('not truncated', res.truncated === false);
  } finally {
    rmrf(root);
  }
})();

// ─── Test 11: nested subdirs recursive (AC6) ────────────────────────────────

(function testRecursive() {
  console.log('\n[11] nested subdirs recursive, relPath preserves nesting');
  const root = makeTmpRoot();
  try {
    writeFile(root, 'sk1/assets/components/button.html', '<button/>');
    writeFile(root, 'sk1/assets/components/inputs/text.html', '<input/>');
    writeFile(root, 'sk1/references/api/user.md', '# user api');
    const res = loadSkillSideFiles('sk1', root);
    const rels = res.files.map((f) => f.relPath).sort();
    check(
      'all 3 nested files loaded',
      JSON.stringify(rels) ===
        JSON.stringify([
          'assets/components/button.html',
          'assets/components/inputs/text.html',
          'references/api/user.md',
        ]),
      rels,
    );
    check(
      'header preserves nested path',
      res.prompt.includes('## Reference: assets/components/inputs/text.html'),
    );
  } finally {
    rmrf(root);
  }
})();

// ─── Test 12: prompt format — `## Reference: <rel>\n\n```<type>\n<content>\n``` ` ──

(function testPromptFormat() {
  console.log('\n[12] prompt section format');
  const root = makeTmpRoot();
  try {
    writeFile(root, 'sk1/assets/a.md', 'AAA');
    writeFile(root, 'sk1/references/b.md', 'BBB');
    const res = loadSkillSideFiles('sk1', root);
    // Content is wrapped in a fenced block tagged with the file type (see the
    // [14] type-field cases) so the model can tell md from css/html/json.
    const expected = [
      '## Reference: assets/a.md',
      '',
      '```md',
      'AAA',
      '```',
      '',
      '## Reference: references/b.md',
      '',
      '```md',
      'BBB',
      '```',
    ].join('\n');
    check('exact prompt format', res.prompt === expected, {
      got: res.prompt,
      expected,
    });
  } finally {
    rmrf(root);
  }
})();

// ─── Test 13: empty skillId / bad input → empty (defensive) ─────────────────

(function testBadInput() {
  console.log('\n[13] empty / non-string skillId → empty');
  const root = makeTmpRoot();
  try {
    const r1 = loadSkillSideFiles('', root);
    check('empty string → empty', r1.prompt === '' && r1.files.length === 0);
    // @ts-expect-error intentional bad input
    const r2 = loadSkillSideFiles(null, root);
    check('null → empty', r2.prompt === '' && r2.files.length === 0);
  } finally {
    rmrf(root);
  }
})();

// ─── Test 14: returned `type` field maps each extension ─────────────────────

(function testTypeField() {
  console.log('\n[14] type field per file');
  const root = makeTmpRoot();
  try {
    writeFile(root, 'sk1/assets/a.md', 'm');
    writeFile(root, 'sk1/assets/b.html', 'h');
    writeFile(root, 'sk1/assets/c.css', 'c');
    writeFile(root, 'sk1/assets/d.json', '{}');
    writeFile(root, 'sk1/assets/e.txt', 't');
    const res = loadSkillSideFiles('sk1', root);
    const byPath = new Map(res.files.map((f) => [f.relPath, f.type]));
    check('md type', byPath.get('assets/a.md') === 'md');
    check('html type', byPath.get('assets/b.html') === 'html');
    check('css type', byPath.get('assets/c.css') === 'css');
    check('json type', byPath.get('assets/d.json') === 'json');
    check('txt type', byPath.get('assets/e.txt') === 'txt');
  } finally {
    rmrf(root);
  }
})();

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log('\n────────────────────────────────────────');
console.log(`  ${passCount} passed,  ${failCount} failed`);
console.log('────────────────────────────────────────\n');

if (failCount > 0) {
  // vitest 把直接调用 process.exit() 判为「unexpected exit」，整个文件会红成
  // file-level error。vitest 里改成抛错，`npx tsx` 单独跑时保留退出码。
  if (process.env.VITEST) throw new Error(`${failCount} checks failed`);
  process.exit(1);
}

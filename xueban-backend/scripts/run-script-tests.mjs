#!/usr/bin/env node
/**
 * run-script-tests.mjs — 跑 server 下那批「自制 check() 断言器」的 tsx 脚本测试。
 *
 * 为什么需要它：server/src 下有两大类 *.test.ts
 *   1. vitest 风格：import { describe, it } from 'vitest'（或靠 globals:true），
 *      由 vitest 收集断言 —— 这批归 vitest 管。
 *   2. 脚本风格：文件里自己写 function check(label, cond) + console.log PASS/FAIL，
 *      末尾 `if (fail > 0) process.exit(1)`。这批**必须**用 `npx tsx <file>` 跑；
 *      扔进 vitest 会因为 `passWithNoTests` 被当成「零测试的空文件」静默通过，
 *      于是既有断言既不红也不绿，等于没人验。
 *
 * 用法：
 *   node scripts/run-script-tests.mjs              # 跑全部
 *   node scripts/run-script-tests.mjs --list       # 只列清单（供核对）
 *   node scripts/run-script-tests.mjs --only kb    # 只跑路径含 kb 的
 *   node scripts/run-script-tests.mjs --jobs 4     # 并发数（默认 4）
 *
 * 退出码：有任一脚本失败 → 1；全部通过 → 0。
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { spawn } from 'node:child_process';

const REPO_ROOT = resolve(import.meta.dirname, '..');
const SERVER_SRC = join(REPO_ROOT, 'server', 'src');

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const value = (name, dflt) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
};

const ONLY = value('--only', '');
const JOBS = Math.max(1, Number(value('--jobs', '4')) || 4);

/** 递归收集 server/src 下所有 *.test.ts。 */
function walk(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) walk(p, out);
    else if (entry.name.endsWith('.test.ts')) out.push(p);
  }
  return out;
}

/**
 * 判定是否脚本风格。三条独立特征，命中任一即是（已对齐实测：零漏判）：
 *   - 自己定义了 check() 断言器（这批脚本的统一写法）
 *   - 文件头标注 `Run: npx tsx ...`（作者声明的运行方式）
 *   - 末尾有 `process.exit(...)` 收尾（脚本靠退出码报结果）
 * 刻意不按「没 import vitest」判断：很多 vitest 用例靠 globals:true，同样不 import。
 */
function isScriptStyle(file) {
  const text = readFileSync(file, 'utf8');
  if (/from ['"]vitest['"]/.test(text)) return false;
  return /function check\s*\(/.test(text) || /Run:\s+(npx\s+)?tsx/.test(text) || /process\.exit/.test(text);
}

const all = walk(SERVER_SRC).filter(isScriptStyle)
  .map((f) => relative(REPO_ROOT, f).replace(/\\/g, '/'))
  .filter((f) => !ONLY || f.includes(ONLY))
  .sort();

if (flag('--list')) {
  for (const f of all) console.log(f);
  console.log(`\n共 ${all.length} 个脚本式测试`);
  process.exit(0);
}

if (all.length === 0) {
  console.log('没有匹配的脚本式测试');
  process.exit(0);
}

/**
 * 跑一个脚本，返回 { file, ok, code, ms, tail }。
 * 直接使用 server/ 已锁定的 tsx，避免 npx 临时下载或误用另一套 Node 依赖。
 */
const TSX_CLI = join(REPO_ROOT, 'server', 'node_modules', 'tsx', 'dist', 'cli.mjs');

function runOne(file) {
  return new Promise((done) => {
    const started = Date.now();
    const child = spawn(process.execPath, [TSX_CLI, file], {
      cwd: REPO_ROOT,
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
      shell: false,
    });
    let out = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { out += d; });
    child.on('close', (code) => {
      const lines = out.split('\n').filter((l) => l.trim());
      done({
        file,
        ok: code === 0,
        code: code ?? -1,
        ms: Date.now() - started,
        tail: lines.slice(-3).join('\n').replace(/\x1b\[[0-9;]*m/g, ''),
      });
    });
  });
}

console.log(`跑 ${all.length} 个脚本式测试（并发 ${JOBS}）…\n`);

const results = [];
let cursor = 0;
async function worker() {
  while (cursor < all.length) {
    const i = cursor++;
    results[i] = await runOne(all[i]);
  }
}
await Promise.all(Array.from({ length: Math.min(JOBS, all.length) }, worker));

const failed = results.filter((r) => !r.ok);
for (const r of results) {
  const mark = r.ok ? 'PASS' : 'FAIL';
  console.log(`${mark}  ${r.file}  (${(r.ms / 1000).toFixed(1)}s)`);
  if (!r.ok) console.log(`      exit=${r.code}\n      ${r.tail.split('\n').join('\n      ')}`);
}

console.log(`\n脚本式测试：${results.length - failed.length} 通过 / ${failed.length} 失败 / 共 ${results.length}`);
if (failed.length) {
  console.log('\n失败清单：');
  for (const f of failed) console.log(`  - ${f.file}`);
  process.exit(1);
}

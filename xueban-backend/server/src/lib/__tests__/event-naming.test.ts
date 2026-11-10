/**
 * event-naming.test.ts — 阶段 2 统一事件命名表一致性 guard。
 *
 * Run with:  npx tsx src/lib/__tests__/event-naming.test.ts   (from server/)
 *
 * 四向对账，任一漂移即红（docs/EVENT_NAMING.md §5 的机器化）：
 *   A. 生产发射点扫描（对象式 `event: 'x'` + 直发式 `.emit(id, 'x',`）的每个
 *      wire 事件名都出现在命名表 §1 主表
 *   B. 命名表 §1 主表的每个 Node 事件名都真实存在于代码（防表内陈旧/笔误）
 *   C. Python `run_event_log.py` DURABLE_EVENT_TYPES 的 14 类都出现在命名表
 *   D. timeline-projector 的 `message` kind 都出现在命名表 §3
 *
 * 扫描文件清单=run-session 链的生产发射面（与命名表 §1 发射处列一致）。
 * transport/spawners 的内部队列事件（delta/end）不上面，见命名表头注。
 */

import { existsSync, readFileSync } from 'fs';
import * as path from 'path';

/**
 * 定位仓库根：从 cwd 逐级向上找，以 server/package.json 为锚点。
 * 这个脚本既会被 `npx tsx src/...`（cwd=server/）跑，也会被 vitest
 * （cwd=仓库根）跑，写死 `../docs` 只有前者成立，后者会指到仓库外。
 *
 * 注意锚点不能用 docs/EVENT_NAMING.md：docs/ 已从版本控制移除（见下），
 * 干净克隆里没有这个文件，用它当锚点会直接抛异常。
 */
function findRepoRoot(from: string): string {
  let cur = path.resolve(from);
  for (;;) {
    if (existsSync(path.join(cur, 'server', 'package.json'))) return cur;
    const up = path.dirname(cur);
    if (up === cur) throw new Error(`server/package.json not found above ${from}`);
    cur = up;
  }
}

const REPO_ROOT = findRepoRoot(process.cwd());
const SERVER_ROOT = path.join(REPO_ROOT, 'server');

let pass = 0;
let fail = 0;
let skipped = 0;
/** 命名表是否在仓库里（docs/ 已从版本控制移除，干净克隆没有）。在下方赋值。 */
let docTracked = false;

function check(label: string, cond: boolean, detail?: string): void {
  if (!docTracked) {
    skipped++;
    console.log(`  SKIP  ${label}（命名表不在仓库里）`);
    return;
  }
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}${detail ? `\n        ${detail}` : ''}`);
  }
}

/** run-session 链的生产发射面。新增发射文件时同步此清单与命名表。 */
const WIRE_EMITTERS = [
  'src/routes/run-sessions.ts',
  'src/lib/timeline-projector.ts',
  'src/lib/timeline-replay.ts',
  'src/assembler.ts',
  'src/parser.ts',
  'src/lib/tool-approval.ts',
  'src/lib/tool-runner.ts',
];

const docPath = path.join(REPO_ROOT, 'docs', 'EVENT_NAMING.md');

/**
 * docs/ 自 7240d96（chore(repo): slim tracked tree）起不再纳入版本控制——协作者把
 * 历史遗留的 docs/ 从 tracked tree 摘掉，docs/ 本就在 .gitignore 里。干净克隆没有
 * 命名表，四向对账无从对起；此时逐条 SKIP 而不是让 readFileSync 直接抛异常或让
 * A/B 方向对比空表而全红。
 */
docTracked = existsSync(docPath);
const doc = docTracked ? readFileSync(docPath, 'utf-8') : '';

// §1 主表区域（供 B 方向精确解析）。
const sec1 = doc.split('## 1.')[1]?.split('## 2.')[0] ?? '';

// ── A. 代码 → 表 ────────────────────────────────────────────────────────────
const codeEvents = new Set<string>();
for (const file of WIRE_EMITTERS) {
  const text = readFileSync(path.join(SERVER_ROOT, file), 'utf-8');
  for (const m of text.matchAll(/event:\s*'([a-z_-]+)'/g)) codeEvents.add(m[1]!);
  for (const m of text.matchAll(/\.emit\([^,]+,\s*'([a-z_-]+)'/g)) codeEvents.add(m[1]!);
}
// 直发式测试里 run-event-bus 自身方法名/演示字母不会进来（文件清单限定），
// 但 run-sessions.ts 的 bus.emit 第二参出现过的非事件字面量需排除：
const NOT_WIRE = new Set(['finish', 'cancel', 'wait', 'reset', 'ensure', 'get']);
for (const n of NOT_WIRE) codeEvents.delete(n);

const missingInDoc: string[] = [];
for (const name of [...codeEvents].sort()) {
  if (!sec1.includes(`\`${name}\``)) missingInDoc.push(name);
}
check(
  `A. ${codeEvents.size} 个代码 wire 事件全部登记在命名表 §1`,
  missingInDoc.length === 0,
  missingInDoc.length ? `未登记: ${missingInDoc.join(', ')}（规则：先加表行再动代码，docs/EVENT_NAMING.md §5）` : undefined,
);

// ── B. 表 → 代码（防陈旧行/笔误） ───────────────────────────────────────────
const docEvents = new Set<string>();
for (const m of sec1.matchAll(/^\|\s*`([a-z_-]+)`/gm)) docEvents.add(m[1]!);
const staleInDoc: string[] = [];
for (const name of [...docEvents].sort()) {
  if (!codeEvents.has(name)) staleInDoc.push(name);
}
check(
  `B. 命名表 §1 的 ${docEvents.size} 个 Node 事件全部真实存在于代码`,
  staleInDoc.length === 0,
  staleInDoc.length ? `表内有、代码无: ${staleInDoc.join(', ')}` : undefined,
);

// ── C. Python durable → 表 ─────────────────────────────────────────────────
const pySource = readFileSync(
  path.join(REPO_ROOT, 'xueban', 'runtime', 'run_event_log.py'),
  'utf-8',
);
const durableBlock = pySource.match(/DURABLE_EVENT_TYPES[^=]*=\s*frozenset\(([\s\S]*?)\)/)?.[1] ?? '';
const durableTypes = [...durableBlock.matchAll(/"([a-z_.]+)"/g)].map((m) => m[1]!);
const missingDurable = durableTypes.filter((t) => !doc.includes(`\`${t}\``));
check(
  `C. Python ${durableTypes.length} 个 DURABLE_EVENT_TYPES 全部出现在命名表`,
  durableTypes.length > 0 && missingDurable.length === 0,
  missingDurable.length ? `未登记: ${missingDurable.join(', ')}` : undefined,
);

// ── D. message kind → 表 §3 ────────────────────────────────────────────────
const projector = readFileSync(path.join(SERVER_ROOT, 'src', 'lib', 'timeline-projector.ts'), 'utf-8');
const kinds = new Set<string>();
for (const m of projector.matchAll(/kind:\s*'([a-z_-]+)'/g)) kinds.add(m[1]!);
const sec3 = doc.split('## 3.')[1]?.split('## 4.')[0] ?? '';
const missingKinds = [...kinds].filter((k) => !sec3.includes(`\`${k}\``));
check(
  `D. timeline-projector ${kinds.size} 个 message kind 全部登记在命名表 §3`,
  kinds.size > 0 && missingKinds.length === 0,
  missingKinds.length ? `未登记: ${missingKinds.join(', ')}` : undefined,
);

const skipNote = docTracked ? '' : `, ${skipped} skipped（docs/EVENT_NAMING.md 不在仓库里）`;
console.log(`\nevent-naming: ${pass} passed, ${fail} failed${skipNote}`);
if (fail > 0) process.exit(1);

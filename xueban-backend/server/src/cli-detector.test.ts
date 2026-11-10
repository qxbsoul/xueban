/**
 * cli-detector.test.ts — Real PATH scan smoke test (Story 15.19 v2)
 *
 * Run from server/:  npx tsx src/cli-detector.test.ts
 *
 * This is INTENTIONALLY a real scan — not mocked. We validate:
 *   - registry shape (10 entries, ids unique, binaries unique, formats valid)
 *   - detect snapshot has correct cardinality + shape
 *   - cache is reused on second call
 *   - refresh bypasses cache
 *   - env_set logic correctly keys off `process.env[needs_env]`
 *
 * Note: PASS/FAIL is decoupled from whether your machine has any CLI installed.
 * We assert the snapshot SHAPE is correct, not that any CLI is present.
 */

import { detectAll, peekDetectCache, __resetDetectCacheForTest } from './cli-detector';
import { KNOWN_CLIS, findCli } from './cli-registry';

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

async function main(): Promise<void> {
  console.log('\n── registry shape ──');
  check('KNOWN_CLIS length >= 10', KNOWN_CLIS.length >= 10, KNOWN_CLIS.length);

  const ids = KNOWN_CLIS.map((c) => c.id);
  const uniqueIds = new Set(ids);
  check('ids unique', uniqueIds.size === ids.length, ids);

  const binaries = KNOWN_CLIS.map((c) => c.binary);
  const uniqueBins = new Set(binaries);
  check('binaries unique', uniqueBins.size === binaries.length, binaries);

  const validFormats = new Set(['claude-stream-json', 'codex-stream-json', 'gh-copilot', 'plain-line', 'cursor-acp']);
  check(
    'all stream_format in whitelist',
    KNOWN_CLIS.every((c) => validFormats.has(c.stream_format)),
  );

  const claude = findCli('claude');
  check('findCli("claude") returns descriptor', !!claude && claude.binary === 'claude');
  check('findCli("nonexistent") returns undefined', findCli('nonexistent') === undefined);

  console.log('\n── detector real scan ──');
  __resetDetectCacheForTest();
  const t0 = Date.now();
  const snap = await detectAll(false);
  const elapsed = Date.now() - t0;
  console.log(`  (real PATH scan completed in ${elapsed}ms)`);

  check('snap.scanned_at is ISO8601', /^\d{4}-\d{2}-\d{2}T/.test(snap.scanned_at));
  check('snap.items.length === KNOWN_CLIS.length', snap.items.length === KNOWN_CLIS.length);

  // Print detection summary
  console.log('  Detection summary:');
  for (const it of snap.items) {
    const status = it.installed ? `installed @ ${it.path}` : 'missing';
    const env = it.env_set ? '' : ` [env ${it.needs_env} not set]`;
    console.log(`    ${it.id.padEnd(14)} ${status}${env}${it.version ? ` (${it.version})` : ''}`);
  }

  // Shape checks
  for (const it of snap.items) {
    check(`${it.id}: has install_cmd`, typeof it.install_cmd === 'string' && it.install_cmd.length > 0);
    check(`${it.id}: env_set is boolean`, typeof it.env_set === 'boolean');
    if (it.installed) {
      check(`${it.id}: installed has path`, typeof it.path === 'string' && (it.path?.length ?? 0) > 0);
    } else {
      check(`${it.id}: missing has path === null`, it.path === null);
      check(`${it.id}: missing has version === null`, it.version === null);
    }
  }

  console.log('\n── cache reuse ──');
  const peek1 = peekDetectCache();
  check('cache populated after detectAll', peek1 !== null);
  const t1 = Date.now();
  const snap2 = await detectAll(false);
  const cacheElapsed = Date.now() - t1;
  console.log(`  (cached call took ${cacheElapsed}ms)`);
  check('second detectAll returned same scanned_at (cache hit)', snap2.scanned_at === snap.scanned_at);
  check('cached call < 50ms', cacheElapsed < 50, cacheElapsed);

  console.log('\n── force refresh ──');
  // Wait a millisecond so the new scanned_at can differ
  await new Promise((r) => setTimeout(r, 5));
  const snap3 = await detectAll(true);
  check('refresh produces new scanned_at', snap3.scanned_at !== snap.scanned_at);
  check('refresh items.length stable', snap3.items.length === KNOWN_CLIS.length);

  console.log('\n── env_set logic ──');
  // Find a CLI that needs env, and synthesize an env presence test
  const needsEnvCli = KNOWN_CLIS.find((c) => !!c.needs_env);
  if (needsEnvCli) {
    const before = process.env[needsEnvCli.needs_env!];
    process.env[needsEnvCli.needs_env!] = 'test-value-123';
    __resetDetectCacheForTest();
    const snapWith = await detectAll(true);
    const itemWith = snapWith.items.find((i) => i.id === needsEnvCli.id)!;
    check(`${needsEnvCli.id}: env_set true when ${needsEnvCli.needs_env} present`, itemWith.env_set === true);

    delete process.env[needsEnvCli.needs_env!];
    __resetDetectCacheForTest();
    const snapWithout = await detectAll(true);
    const itemWithout = snapWithout.items.find((i) => i.id === needsEnvCli.id)!;
    check(`${needsEnvCli.id}: env_set false when ${needsEnvCli.needs_env} absent`, itemWithout.env_set === false);

    if (before !== undefined) process.env[needsEnvCli.needs_env!] = before;
  } else {
    console.log('  (skipped — no env-gated CLI in registry)');
  }

  console.log(`\n────────────────────────────────────────────`);
  console.log(`Total: ${passCount + failCount} | PASS: ${passCount} | FAIL: ${failCount}`);
  if (failCount > 0) process.exit(1);
}

main().catch((err) => {
  console.error('test crashed:', err);
  process.exit(1);
});

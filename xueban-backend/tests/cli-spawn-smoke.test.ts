/**
 * cli-spawn-smoke.test.ts — Wire-contract guard #2 (2026-05-11)
 *
 * Why this exists:
 *   On 2026-05-11 the cli-registry shipped `claude` without the `--verbose`
 *   and `--permission-mode bypassPermissions` args required to make the
 *   stream-json output actually stream. The `--version` smoke check passed
 *   in CI but the runtime spawn was broken.
 *
 *   This test goes further: for EVERY entry in KNOWN_CLIS, it actually
 *   spawns `<binary> <version_arg>` on the host and reports the result. It
 *   does NOT validate the *streaming* spawn args — that would require a
 *   real session — but a non-zero exit on `--version` is a near-certain
 *   sign that the binary is on PATH but the args are wrong (e.g. someone
 *   typed `-version` instead of `--version`, or the entry was copy-pasted
 *   from a CLI that takes `version` as a subcommand).
 *
 *   ENOENT (binary not on PATH) is SKIP, not FAIL — most dev machines won't
 *   have every CLI installed. Exit code is non-zero only if a CLI is
 *   present but `--version` returns non-zero or empty stdout.
 *
 * Run:
 *   npx tsx tests/cli-spawn-smoke.test.ts
 */

import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';

// server/src/* is compiled as CommonJS (see server/tsconfig.json); use
// createRequire so the named exports survive the CJS↔ESM bridge.
const require = createRequire(import.meta.url);
const registry = require('../server/src/cli-registry.ts') as typeof import('../server/src/cli-registry');
const { KNOWN_CLIS } = registry;
type CliDescriptor = (typeof KNOWN_CLIS)[number];

type Outcome =
  | { kind: 'pass'; stdoutSample: string }
  | { kind: 'skip'; reason: 'ENOENT' }
  | { kind: 'fail'; reason: string; stdout?: string; stderr?: string };

/**
 * Probe a CLI for liveness on PATH.
 *
 * On Windows, npm-global / Scoop / WinGet shims are `.cmd` files which Node's
 * default no-shell spawn can't resolve directly. We try TWO strategies in
 * order:
 *   1. spawnSync(bin, args)              — POSIX-style, works on Linux/Mac.
 *   2. spawnSync('where', [bin])         — Windows-aware PATH lookup. If
 *      `where` succeeds (exit 0), retry strategy 1 with shell:true so we
 *      pick up the .cmd shim. If `where` fails, treat as ENOENT/SKIP.
 *
 * This avoids the false-positive failures we hit when `shell:true` is used
 * for CLIs that aren't installed — cmd.exe returns exit 1 with a GBK-encoded
 * stderr that we can't reliably match against "is not recognized".
 */
function probeBare(cli: CliDescriptor, useShell: boolean): Outcome {
  const result = spawnSync(cli.binary, [cli.version_arg], {
    timeout: 5_000,
    encoding: 'utf8',
    shell: useShell,
  });

  if (result.error) {
    const code = (result.error as NodeJS.ErrnoException).code;
    if (code === 'ENOENT') return { kind: 'skip', reason: 'ENOENT' };
    return { kind: 'fail', reason: `spawn error: ${result.error.message}` };
  }
  if (result.status === null) {
    return { kind: 'fail', reason: `timed out / signal ${result.signal ?? '?'}` };
  }
  if (result.status !== 0) {
    return {
      kind: 'fail',
      reason: `exit ${result.status}`,
      stdout: (result.stdout ?? '').toString().slice(0, 200),
      stderr: (result.stderr ?? '').toString().slice(0, 200),
    };
  }
  const out = (result.stdout ?? '').toString().trim();
  const err = (result.stderr ?? '').toString().trim();
  // Some CLIs print to stderr (e.g. `cursor --version` historically) — accept
  // either as long as we got *something* non-empty.
  const sample = out || err;
  if (!sample) {
    return { kind: 'fail', reason: 'empty stdout+stderr on --version (suspicious)' };
  }
  return { kind: 'pass', stdoutSample: sample.split('\n')[0].slice(0, 80) };
}

/**
 * Windows-aware: check whether `binary` is on PATH using `where` (which
 * resolves `.cmd` / `.exe` / `.bat` shims). Returns true only when at
 * least one match exists.
 */
function existsOnPathWindows(binary: string): boolean {
  const r = spawnSync('where', [binary], { encoding: 'utf8', timeout: 3_000 });
  if (r.error || r.status !== 0) return false;
  return (r.stdout ?? '').trim().length > 0;
}

function probe(cli: CliDescriptor): Outcome {
  // POSIX / generic path first.
  const first = probeBare(cli, false);
  if (first.kind === 'pass') return first;

  // On Windows, no-shell spawn can't see .cmd shims (npm globals). If `where`
  // says the binary exists, retry under a shell to pick up the shim.
  if (process.platform === 'win32') {
    if (existsOnPathWindows(cli.binary)) {
      return probeBare(cli, true);
    }
    // `where` says it doesn't exist → SKIP (not a failure).
    return { kind: 'skip', reason: 'ENOENT' };
  }

  return first;
}

function main() {
  console.log('\n=== CLI spawn smoke tests ===\n');
  console.log('Probing each entry in KNOWN_CLIS with `<binary> <version_arg>` …\n');

  let passed = 0;
  let failed = 0;
  let skipped = 0;
  const fails: Array<{ id: string; reason: string; stdout?: string; stderr?: string }> = [];

  // Pretty column widths
  const idW = 16;
  const binW = 16;

  console.log(
    `${'id'.padEnd(idW)} ${'binary'.padEnd(binW)} status   sample / reason`,
  );
  console.log('─'.repeat(idW + binW + 30));

  for (const cli of KNOWN_CLIS) {
    const out = probe(cli);
    let line: string;
    if (out.kind === 'pass') {
      passed++;
      line = `${cli.id.padEnd(idW)} ${cli.binary.padEnd(binW)} ✓ pass   ${out.stdoutSample}`;
    } else if (out.kind === 'skip') {
      skipped++;
      line = `${cli.id.padEnd(idW)} ${cli.binary.padEnd(binW)} ⊘ skip   not on PATH`;
    } else {
      failed++;
      fails.push({ id: cli.id, reason: out.reason, stdout: out.stdout, stderr: out.stderr });
      line = `${cli.id.padEnd(idW)} ${cli.binary.padEnd(binW)} ✗ fail   ${out.reason}`;
    }
    console.log(line);
  }

  console.log('');
  console.log(`Result: ${passed} pass, ${skipped} skip (not installed), ${failed} fail`);

  if (failed > 0) {
    console.log('\nFailure details (binary present on PATH but `--version` misbehaved):');
    for (const f of fails) {
      console.log(`  [${f.id}] ${f.reason}`);
      if (f.stdout) console.log(`    stdout: ${f.stdout}`);
      if (f.stderr) console.log(`    stderr: ${f.stderr}`);
    }
    console.log(
      '\nHint: a present-but-failing CLI usually means `version_arg` is wrong\n' +
        '(e.g. some CLIs use `version` as a subcommand instead of `--version`).\n' +
        'Fix the entry in server/src/cli-registry.ts.',
    );
    process.exit(1);
  }
  process.exit(0);
}

main();

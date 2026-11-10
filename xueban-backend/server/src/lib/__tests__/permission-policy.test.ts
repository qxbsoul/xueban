/**
 * permission-policy.test.ts — S3 (skill-team-conversion-design-v1.md §5, D6)
 * smoke test for the allow/deny PermissionPolicy.
 *
 * Run with:  npx tsx src/lib/__tests__/permission-policy.test.ts   (from server/)
 *
 * Coverage:
 *   1. default-allow + no overrides → every tool allowed
 *   2. default-deny + no overrides → every tool denied (with reason)
 *   3. per-tool override beats default in both directions
 *   4. modeFor() reflects the same resolution as authorize()
 *   5. fromAllowedTools() = deny-by-default + listed tools allowed
 *   6. fromAllowedTools([]) = deny everything
 *   7. authorize() return shape — { allow: true } vs { deny: string }
 *      narrows correctly under TS discriminated-union rules
 */

import { PermissionPolicy, type PermissionOutcome } from '../permission-policy';

let pass = 0;
let fail = 0;

function check(label: string, expected: unknown, actual: unknown) {
  const eq = JSON.stringify(expected) === JSON.stringify(actual);
  if (eq) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(
      `  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`,
    );
  }
}

// ── 1. default-allow ─────────────────────────────────────────────────────────
{
  const p = new PermissionPolicy('allow');
  check('default-allow: bash mode', 'allow', p.modeFor('bash'));
  check('default-allow: any tool mode', 'allow', p.modeFor('anything_at_all'));
  check('default-allow: authorize bash', { allow: true }, p.authorize('bash'));
}

// ── 2. default-deny ──────────────────────────────────────────────────────────
{
  const p = new PermissionPolicy('deny');
  check('default-deny: bash mode', 'deny', p.modeFor('bash'));
  const out = p.authorize('bash');
  check(
    'default-deny: authorize bash',
    { deny: "tool 'bash' denied by permission policy" },
    out,
  );
}

// ── 3. per-tool override beats default ───────────────────────────────────────
{
  const p = new PermissionPolicy(
    'deny',
    new Map<string, 'allow' | 'deny'>([
      ['bash', 'allow'],
      ['edit', 'deny'],
    ]),
  );
  check('override: bash allowed (overrides deny default)', { allow: true }, p.authorize('bash'));
  check(
    'override: edit denied (matches default)',
    { deny: "tool 'edit' denied by permission policy" },
    p.authorize('edit'),
  );
  check(
    'override: unknown tool falls to default deny',
    { deny: "tool 'mystery' denied by permission policy" },
    p.authorize('mystery'),
  );
}

{
  const p = new PermissionPolicy(
    'allow',
    new Map<string, 'allow' | 'deny'>([['dangerous', 'deny']]),
  );
  check('override: dangerous denied under allow default', 'deny', p.modeFor('dangerous'));
  check('override: safe falls to allow default', 'allow', p.modeFor('safe'));
}

// ── 4. modeFor() consistency with authorize() ────────────────────────────────
{
  const p = new PermissionPolicy(
    'deny',
    new Map<string, 'allow' | 'deny'>([['bash', 'allow']]),
  );
  for (const name of ['bash', 'edit', 'read_file', 'write_file']) {
    const mode = p.modeFor(name);
    const out = p.authorize(name);
    const allowed = 'allow' in out;
    check(`consistency: ${name} mode↔outcome`, mode === 'allow', allowed);
  }
}

// ── 5. fromAllowedTools() — canonical SKILL.md mapping ───────────────────────
{
  const p = PermissionPolicy.fromAllowedTools(['Bash', 'Read', 'Edit']);
  check('fromAllowedTools: Bash allowed', { allow: true }, p.authorize('Bash'));
  check('fromAllowedTools: Read allowed', { allow: true }, p.authorize('Read'));
  check('fromAllowedTools: Edit allowed', { allow: true }, p.authorize('Edit'));
  check(
    'fromAllowedTools: Write denied (not listed)',
    { deny: "tool 'Write' denied by permission policy" },
    p.authorize('Write'),
  );
  check(
    'fromAllowedTools: WebFetch denied (not listed)',
    { deny: "tool 'WebFetch' denied by permission policy" },
    p.authorize('WebFetch'),
  );
}

// ── 6. fromAllowedTools([]) → deny everything ────────────────────────────────
{
  const p = PermissionPolicy.fromAllowedTools([]);
  check(
    'fromAllowedTools empty: bash denied',
    { deny: "tool 'bash' denied by permission policy" },
    p.authorize('bash'),
  );
  check('fromAllowedTools empty: mode = deny', 'deny', p.modeFor('anything'));
}

// ── 6.5 v3 placeholder: authorize accepts optional _input arg ───────────────
// P1 review fix: signature reserves a second arg for the future 'prompt' mode.
// Today the arg is ignored; passing it must not change the outcome.
{
  const p = PermissionPolicy.fromAllowedTools(['bash']);
  check(
    'authorize: passing _input does not change allow outcome',
    { allow: true },
    p.authorize('bash', 'rm -rf /'),
  );
  check(
    'authorize: passing _input does not change deny outcome',
    { deny: "tool 'rm' denied by permission policy" },
    p.authorize('rm', '{}'),
  );
  check(
    'authorize: no second arg still works (back-compat)',
    { allow: true },
    p.authorize('bash'),
  );
}

// ── 7. Discriminated-union narrowing ─────────────────────────────────────────
{
  const p = PermissionPolicy.fromAllowedTools(['bash']);
  const out1: PermissionOutcome = p.authorize('bash');
  const out2: PermissionOutcome = p.authorize('rm');

  // Narrow on `allow` key. Other branches widened in PR-D to include
  // { prompt: string } — guard explicitly for the `deny` shape.
  const renderNonAllow = (o: Exclude<PermissionOutcome, { allow: true }>): string =>
    'deny' in o ? `denied:${o.deny}` : `prompt:${o.prompt}`;
  const got1 = 'allow' in out1 ? `allowed:${out1.allow}` : renderNonAllow(out1);
  const got2 = 'allow' in out2 ? `allowed:${out2.allow}` : renderNonAllow(out2);
  check('narrow: allow branch', 'allowed:true', got1);
  check(
    'narrow: deny branch',
    "denied:tool 'rm' denied by permission policy",
    got2,
  );
}

console.log(`\n${pass} pass, ${fail} fail`);
if (fail > 0) process.exit(1);

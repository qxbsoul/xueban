/**
 * lint.test.ts — Story 15.14 — standalone runner (`npx tsx src/lint.test.ts`).
 *
 * Covers: HTML / YAML / Markdown / CSS — each fixture asserts ≥3 expected
 * findings + summary correctness. Exits with code 1 on any assertion failure
 * so CI / `node --test` style chaining works.
 */

import fs from 'fs';
import path from 'path';
import { lintContent } from './lint';

let failed = 0;

function assert(cond: unknown, msg: string): void {
  if (!cond) {
    console.error(`  FAIL: ${msg}`);
    failed += 1;
  } else {
    console.log(`  ok: ${msg}`);
  }
}

function readFixture(name: string): string {
  return fs.readFileSync(path.join(__dirname, '__fixtures__', 'lint', name), 'utf-8');
}

function findRule(findings: { rule: string; severity: string }[], rule: string) {
  return findings.find(f => f.rule === rule);
}

console.log('--- HTML lint (sample.html) ---');
{
  const html = readFixture('sample.html');
  const r = lintContent('sample.html', html);
  assert(r.language === 'html', 'language=html');
  assert(r.findings.length >= 3, `>=3 findings (got ${r.findings.length})`);
  assert(!!findRule(r.findings, 'duplicate-id'), 'duplicate-id error present');
  assert(!!findRule(r.findings, 'img-missing-alt'), 'img-missing-alt info present');
  assert(!!findRule(r.findings, 'unclosed-tag'), 'unclosed-tag error present');
  assert(r.summary.errors >= 1, `summary.errors >=1 (got ${r.summary.errors})`);
}

console.log('--- YAML lint (team.blueprint.yml) ---');
{
  const yml = readFixture('team.blueprint.yml');
  const r = lintContent('team.blueprint.yml', yml);
  assert(r.language === 'yaml', 'language=yaml');
  assert(r.findings.length >= 2, `>=2 findings (got ${r.findings.length})`);
  const missing = r.findings.filter(f => f.rule === 'missing-required-field');
  assert(missing.length >= 1, 'missing-required-field for policy_matrix');
  assert(!!findRule(r.findings, 'empty-collection'), 'empty-collection warning for empty agents');
  assert(r.summary.errors >= 1, 'errors >=1');
}

console.log('--- YAML parse-error fixture (inline) ---');
{
  const bad = 'agents: [\n  - id: a\n  - id: b\n# unterminated';
  const r = lintContent('broken.yml', bad);
  assert(r.language === 'yaml', 'language=yaml');
  assert(!!findRule(r.findings, 'parse-error'), 'parse-error finding present');
  assert(r.summary.errors >= 1, 'errors >=1');
}

console.log('--- Markdown lint (sample-report.md) ---');
{
  const md = readFixture('sample-report.md');
  const r = lintContent('sample-report.md', md);
  assert(r.language === 'markdown', 'language=markdown');
  assert(!!findRule(r.findings, 'heading-level-skip'), 'heading-level-skip warning');
  assert(!!findRule(r.findings, 'sf-tag-malformed'), 'sf-tag-malformed (bad body or missing name)');
  assert(!!findRule(r.findings, 'sf-tag-unclosed'), 'sf-tag-unclosed for review');
  assert(r._meta?.sf_steps_seen?.includes('discover') ?? false, '_meta.sf_steps_seen has discover');
  assert(r._meta?.sf_steps_completed?.includes('discover') ?? false, '_meta.sf_steps_completed has discover');
  assert(r._meta?.sf_steps_seen?.includes('draft') ?? false, '_meta.sf_steps_seen has draft');
  assert(r._meta?.sf_steps_completed?.includes('draft') === false, 'draft NOT in completed');
  assert(r.findings.length >= 3, `>=3 findings (got ${r.findings.length})`);
}

console.log('--- CSS lint (styles.css) ---');
{
  const css = readFixture('styles.css');
  const r = lintContent('styles.css', css);
  assert(r.language === 'css', 'language=css');
  assert(!!findRule(r.findings, 'important-overuse'), 'important-overuse info');
  assert(!!findRule(r.findings, 'selector-too-deep'), 'selector-too-deep info');
  assert(r.summary.infos >= 2, `infos >=2 (got ${r.summary.infos})`);
  assert(r.summary.errors === 0, 'no errors (CSS weakened to info-only)');
}

console.log('--- unknown extension ---');
{
  const r = lintContent('mystery.txt', 'just text');
  assert(r.language === 'unknown', 'language=unknown');
  assert(r.findings.length === 0, 'no findings');
  assert(r.summary.errors + r.summary.warnings + r.summary.infos === 0, 'all-zero summary');
}

console.log('---');
if (failed > 0) {
  console.error(`${failed} assertion(s) failed`);
  process.exit(1);
} else {
  console.log('All lint assertions passed.');
}

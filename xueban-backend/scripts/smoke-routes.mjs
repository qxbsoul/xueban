// Headless browser smoke check — visits each route and prints console errors.
// Usage: node scripts/smoke-routes.mjs
import { chromium } from 'playwright';

const ROUTES = [
  '/',
  '/start',
  '/chat/default',
  '/agent-dm/agent-001',
  '/teams',
  '/agents',
  '/templates',
  '/settings',
];

const BASE = process.env.SF_BASE || 'http://localhost:3004';
const WAIT_MS = 2000;

async function smokeOne(browser, route) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const errors = [];
  const warnings = [];
  // Skip CORS / network errors (environmental — backend not running) so we
  // only flag real JS bugs.
  const isEnvErr = (s) =>
    /CORS policy|net::ERR_FAILED|net::ERR_CONNECTION|404 \(Not Found\)|status of 404|Failed to load resource/i.test(s);
  page.on('console', (msg) => {
    const t = msg.type();
    const text = msg.text();
    if (isEnvErr(text)) return;
    if (t === 'error') errors.push(text);
    else if (t === 'warning') warnings.push(text);
  });
  page.on('pageerror', (err) => {
    if (isEnvErr(err.message)) return;
    errors.push(`[pageerror] ${err.message}`);
  });
  let status = 'OK';
  try {
    const resp = await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 15000 });
    if (!resp || !resp.ok()) status = `HTTP ${resp ? resp.status() : 'no-response'}`;
  } catch (e) {
    status = `nav-error: ${e.message}`;
  }
  await page.waitForTimeout(WAIT_MS);
  await ctx.close();
  return { route, status, errors, warnings };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const results = [];
  for (const r of ROUTES) {
    const out = await smokeOne(browser, r);
    results.push(out);
    const ec = out.errors.length;
    const wc = out.warnings.length;
    console.log(`${ec === 0 ? 'PASS' : 'FAIL'}  ${out.route.padEnd(24)}  ${out.status}  errors=${ec}  warnings=${wc}`);
    out.errors.forEach((e, i) => console.log(`    err[${i}]: ${e.slice(0, 200)}`));
  }
  await browser.close();

  const totalErrors = results.reduce((s, r) => s + r.errors.length, 0);
  const failed = results.filter((r) => r.errors.length > 0).length;
  console.log('---');
  console.log(`Routes: ${results.length}  Passed: ${results.length - failed}  Failed: ${failed}  Total errors: ${totalErrors}`);
  process.exit(totalErrors > 0 ? 1 : 0);
})();

/**
 * Browser verification harness for run-session-v2 (agent-4 Phase B).
 *
 * - Opens a real Chromium via Playwright
 * - Visits /run-session/<id>?goal=... twice (dark + light themes)
 * - Captures console errors, takes screenshots, walks the tab UI,
 *   and writes a JSON report.
 *
 * Usage: node scripts/verify-run-session.mjs <session_id> <frontend_url>
 */
import { chromium } from 'playwright';
import { writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

const sessionId = process.argv[2] ?? '1644debc-4780-4585-9f8d-38dd7f2c6828';
const baseUrl = process.argv[3] ?? 'http://127.0.0.1:3008';
const outDir = 'docs/design/assets/intent-workflow-v1/verify-2026-05-18';

async function createFreshSession() {
  const r = await fetch('http://127.0.0.1:8002/api/run-sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      goal: '用 BMAD 方法组建全栈产品团队：PM、架构师、全栈工程师、QA',
    }),
  });
  const j = await r.json();
  return j.session_id;
}

async function run() {
  await mkdir(outDir, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const results = { generatedAt: new Date().toISOString(), themes: {} };

  for (const theme of ['night', 'day']) {
    // Fresh session per theme so SSE stream is undrained when browser subscribes
    const freshId = await createFreshSession();
    console.log(`[${theme}] fresh session=${freshId}`);
    const ctx = await browser.newContext({
      viewport: { width: 1480, height: 920 },
    });
    const page = await ctx.newPage();
    const consoleErrors = [];
    const pageErrors = [];
    const consoleAll = [];
    page.on('console', (msg) => {
      const text = `[${msg.type()}] ${msg.text()}`;
      consoleAll.push(text);
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    page.on('pageerror', (err) => pageErrors.push(String(err)));

    // Preset theme via localStorage before first paint + capture EventSource frames
    await page.addInitScript((t) => {
      try {
        localStorage.setItem('sf.theme', t);
        document.documentElement.setAttribute('data-theme', t);
      } catch {}
      // Wrap EventSource to log every dispatch
      const RealES = window.EventSource;
      class LoggingES extends RealES {
        constructor(url, opts) {
          super(url, opts);
          console.log(`[ES-NEW] ${url}`);
          this.addEventListener('open',  () => console.log('[ES-OPEN]'));
          this.addEventListener('error', () => console.log(`[ES-ERR] readyState=${this.readyState}`));
          this.addEventListener('message', (e) => console.log(`[ES-MSG] ${e.data?.slice?.(0,80)}`));
        }
        addEventListener(name, cb, opts) {
          if (!['message','open','error'].includes(name)) {
            console.log(`[ES-LISTEN] ${name}`);
          }
          return super.addEventListener(name, function (...args) {
            console.log(`[ES-EVENT] ${name}`);
            return cb.apply(this, args);
          }, opts);
        }
      }
      window.EventSource = LoggingES;
    }, theme);

    const url = `${baseUrl}/run-session/${freshId}?goal=${encodeURIComponent('用 BMAD 方法组建全栈产品团队：PM、架构师、全栈工程师、QA')}`;
    console.log(`[${theme}] navigate → ${url}`);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(6000);

    // Probe DOM landmarks (initial / overview tab)
    const landmarks = {};
    landmarks.followChip = await page.locator('[data-testid="run-session-follow-chip"]').count();
    landmarks.goToChat = await page.locator('[data-testid="run-session-go-to-chat"]').count();
    landmarks.tabButtons = await page.locator('[role="tab"], button[data-tab]').count();
    landmarks.thinkCard = await page.locator('[data-component="think-card"]').count();
    // FollowChip state
    landmarks.followChipMode = await page.locator('[data-testid="run-session-follow-chip"]').getAttribute('data-mode').catch(() => null);

    // Screenshot 1: default landing on this route (theme = current)
    await page.screenshot({ path: join(outDir, `${theme}-default.png`), fullPage: false });

    // Switch to Team tab
    const teamTab = page.locator('button:has-text("Team"), button:has-text("团队")').first();
    if (await teamTab.count()) {
      await teamTab.click().catch(() => {});
      await page.waitForTimeout(700);
      landmarks.teamDagNodes = await page.locator('.sf-node, [data-component="blueprint-canvas"] .sf-node').count();
      landmarks.teamRaciMatrix = await page.locator('[data-component="policy-matrix-mini"], [data-testid="policy-matrix-mini"]').count();
      await page.screenshot({ path: join(outDir, `${theme}-team.png`), fullPage: false });
    }

    // Switch to Agent tab
    const agentTab = page.locator('button:has-text("Agent"), button:has-text("成员")').first();
    if (await agentTab.count()) {
      await agentTab.click().catch(() => {});
      await page.waitForTimeout(4000); // wait for any pending nodes to flow in
      landmarks.agentRoster = await page.locator('[data-testid="agent-roster"]').count();
      landmarks.agentPanel = await page.locator('[data-component="agent-panel"]').count();
      landmarks.agentDetail = await page.locator('[data-component="agent-detail"]').count();
      landmarks.personaCard = await page.locator('text=SYSTEM PROMPT').count();
      landmarks.personaPlaceholder = await page.locator('text=未设置 system prompt').count();
      landmarks.toolsHeader = await page.locator('text=/^Tools$/').count();
      // Capture innerHTML of agent panel for debugging
      const apHtml = await page.locator('[data-component="agent-panel"]').innerHTML().catch(() => '');
      landmarks.agentPanelInnerHTML = apHtml.slice(0, 400);
      // Check if AgentEmptyState rendered
      landmarks.agentEmptyText = await page.locator('text=/还没有 agent|等待|没有/').count();
      // After clicking, follow chip should be locked
      landmarks.followChipAfterTabClick = await page.locator('[data-testid="run-session-follow-chip"]').getAttribute('data-mode').catch(() => null);
      await page.screenshot({ path: join(outDir, `${theme}-agent.png`), fullPage: false });
    }

    // Click follow chip again to resume auto
    const chip = page.locator('[data-testid="run-session-follow-chip"]').first();
    if (await chip.count()) {
      await chip.click().catch(() => {});
      await page.waitForTimeout(300);
      landmarks.followChipAfterResume = await page.locator('[data-testid="run-session-follow-chip"]').getAttribute('data-mode').catch(() => null);
    }

    // Switch to Preview tab
    const previewTab = page.locator('button:has-text("Preview"), button:has-text("预览")').first();
    if (await previewTab.count()) {
      await previewTab.click().catch(() => {});
      await page.waitForTimeout(500);
      await page.screenshot({ path: join(outDir, `${theme}-preview.png`), fullPage: false });
    }

    // Back to Agent and check headline
    if (await agentTab.count()) {
      await agentTab.click().catch(() => {});
      await page.waitForTimeout(500);
    }

    // Check header for absence of Pause / Resume / Editor buttons
    landmarks.headerHasPause = (await page.locator('button:has-text("Pause"), button:has-text("暂停")').count()) > 0;
    landmarks.headerHasResume = (await page.locator('button:has-text("Resume"), button:has-text("恢复")').count()) > 0;
    landmarks.headerHasEditor = (await page.locator('button:has-text("Editor"), button:has-text("编辑器")').count()) > 0;

    // Try ⌘K / Ctrl+K
    let pickerOpened = false;
    try {
      await page.keyboard.press('Control+K');
      await page.waitForTimeout(400);
      pickerOpened = (await page.locator('[data-testid="agent-picker-modal"], [data-component="agent-picker-modal"]').count()) > 0
        || (await page.locator('input[placeholder*="搜索"]').count()) > 0;
      // close
      await page.keyboard.press('Escape');
    } catch {}

    results.themes[theme] = {
      sessionId: freshId,
      landmarks,
      pickerOpened,
      consoleErrors,
      pageErrors,
      consoleAll: consoleAll.slice(0, 150),
    };

    await ctx.close();
  }

  await browser.close();
  await writeFile(
    join(outDir, 'verify-report.json'),
    JSON.stringify(results, null, 2),
    'utf8',
  );
  console.log(JSON.stringify(results, null, 2));
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});

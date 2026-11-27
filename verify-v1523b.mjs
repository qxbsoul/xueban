import { chromium } from './xueban-web/node_modules/playwright-core/index.mjs';

const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
const errors = [];
try {
  const context = await browser.newContext({ viewport: { width: 1500, height: 1200 } });
  await context.addInitScript(value => localStorage.setItem('sf_xueban_session', value), process.env.QA_TOKEN || '');
  const page = await context.newPage();
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', async r => { if (r.url().includes('/analysis/errors') || r.url().includes('/analysis/compare')) { if (r.status() >= 400) errors.push(`HTTP ${r.status()} ${r.url()}`); } });
  await page.goto('http://127.0.0.1:3007/app', { waitUntil: 'domcontentloaded' });
  await page.locator('.teacher-app').waitFor({ timeout: 30000 });
  await page.locator('.nav-item').filter({ hasText: '学情分析' }).click();
  await page.locator('.ta-class-card').first().waitFor({ timeout: 30000 });
  await page.locator('.ta-class-card').first().click();
  await page.waitForTimeout(1000);

  await page.getByText('班级比较', { exact: true }).first().click();
  await page.waitForTimeout(6000);
  const compareText = (await page.locator('.teacher-main, .teacher-content, main').first().innerText()).replace(/\s+/g, ' ');
  const compareFacts = { hasNoAdvantage: compareText.includes('无明显优势'), hasSampleWarning: compareText.includes('有效学生少于'), hasDifference: compareText.includes('班级差异说明'), hasInsufficient: compareText.includes('样本不足'), snippet: compareText.slice(0, 1200) };

  await page.getByText('错误类型分析', { exact: true }).first().click();
  await page.waitForTimeout(6000);
  const errorText = (await page.locator('.teacher-main, .teacher-content, main').first().innerText()).replace(/\s+/g, ' ');
  const errorFacts = { hasWeightedHeader: errorText.includes('加权次数'), hasActual: errorText.includes('实际次数'), hasUnrecognized: errorText.includes('未识别错误'), hasClassColumn: errorText.includes('涉及知识点 班级'), snippet: errorText.slice(0, 1200) };

  console.log(JSON.stringify({ compareFacts, errorFacts, errors }, null, 2));
} finally { await browser.close(); }

/**
 * markdown.test.ts — Story 15.15 standalone smoke test for HTML → Markdown.
 *
 * Run with:  npx tsx src/exporters/markdown.test.ts   (from server/)
 *
 * No external test framework — matches the parser.test.ts / export.test.ts
 * style. Each `check()` increments pass/fail counters; non-zero failures
 * exits the process with code 1.
 */
import fs from 'fs';
import os from 'os';
import path from 'path';
import {
  htmlToMarkdown,
  htmlFileToMarkdown,
} from './markdown';

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

async function main() {
  // ── headings ──────────────────────────────────────────────────────────────
  {
    const md = htmlToMarkdown('<h1>Title</h1><h2>Sub</h2><h3>Deeper</h3>');
    check('h1 → "# Title"', md.includes('# Title'), md);
    check('h2 → "## Sub"', md.includes('## Sub'), md);
    check('h3 → "### Deeper"', md.includes('### Deeper'), md);
  }

  // ── lists (atx + bullet `-`) ──────────────────────────────────────────────
  {
    const md = htmlToMarkdown('<ul><li>One</li><li>Two</li></ul>');
    check('ul item 1 has "- One"', /-\s+One/.test(md), md);
    check('ul item 2 has "- Two"', /-\s+Two/.test(md), md);
  }
  {
    const md = htmlToMarkdown('<ol><li>First</li><li>Second</li></ol>');
    check('ol item 1 has "1. First"', /1\.\s+First/.test(md), md);
    check('ol item 2 has "2. Second"', /2\.\s+Second/.test(md), md);
  }

  // ── links ────────────────────────────────────────────────────────────────
  {
    const md = htmlToMarkdown('<a href="https://example.com">click</a>');
    check(
      'anchor → [click](https://example.com)',
      md.includes('[click](https://example.com)'),
      md,
    );
  }

  // ── code blocks (fenced) ─────────────────────────────────────────────────
  {
    const md = htmlToMarkdown(
      '<pre><code>const x = 1;\nconst y = 2;</code></pre>',
    );
    check('code block uses fenced ``` style', md.includes('```'), md);
    check('code block preserves content', md.includes('const x = 1;'), md);
  }

  // ── inline code ──────────────────────────────────────────────────────────
  {
    const md = htmlToMarkdown('<p>Use <code>npm install</code> first.</p>');
    check(
      'inline code wrapped in backticks',
      md.includes('`npm install`'),
      md,
    );
  }

  // ── strip script/style/noscript ──────────────────────────────────────────
  {
    const md = htmlToMarkdown(
      '<style>.x{color:red}</style><h1>Real</h1><script>alert(1)</script>',
    );
    check('drops <style> contents', !md.includes('color:red'), md);
    check('drops <script> contents', !md.includes('alert(1)'), md);
    check('keeps real content', md.includes('# Real'), md);
  }

  // ── full document round-trip via file I/O ────────────────────────────────
  {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-md-test-'));
    const html = `<!doctype html>
<html><body>
  <h1>Report</h1>
  <p>Hello <strong>world</strong>.</p>
  <ul><li>Alpha</li><li>Beta</li></ul>
  <pre><code>x = 42</code></pre>
</body></html>`;
    const file = path.join(tmp, 'report.html');
    fs.writeFileSync(file, html, 'utf-8');
    try {
      const md = await htmlFileToMarkdown(file);
      check('file: contains "# Report"', md.includes('# Report'), md);
      check(
        'file: bold → **world**',
        md.includes('**world**'),
        md,
      );
      check('file: list bullets present', /-\s+Alpha/.test(md), md);
      check('file: fenced code present', md.includes('```'), md);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  }

  // ── empty / minimal HTML ─────────────────────────────────────────────────
  {
    check('empty string is empty markdown', htmlToMarkdown('') === '', '');
    const md = htmlToMarkdown('<p></p>');
    check('empty paragraph → blank/whitespace', md.trim() === '', md);
  }

  console.log(`\n${passCount} passed, ${failCount} failed`);
  if (failCount > 0) process.exit(1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

/**
 * exporters/markdown.ts — Story 15.15
 *
 * HTML → Markdown via turndown.
 *
 * Why turndown: small (~200 KB), zero-runtime-deps, and produces structurally
 * faithful Markdown for headings/lists/code blocks/links. We strip
 * <script>/<style>/<noscript> so generated artifact CSS / JS doesn't bleed
 * into the output as huge text blobs.
 *
 * Heading style is `atx` (`#` markers) and code blocks use fenced ```` ``` ````
 * style — both standard for modern Markdown consumers (Obsidian, Notion-imported
 * MD, GitHub). Bullet lists use `-` for stability.
 */
import TurndownService from 'turndown';
import fs from 'fs/promises';

const td = new TurndownService({
  headingStyle: 'atx',
  codeBlockStyle: 'fenced',
  bulletListMarker: '-',
});

// Drop blocks that would otherwise dump CSS/JS into the markdown.
td.remove(['script', 'style', 'noscript']);

/** Convert a raw HTML string to Markdown. Pure (no I/O) for easy unit testing. */
export function htmlToMarkdown(html: string): string {
  return td.turndown(html);
}

/** Read an HTML file and return its Markdown rendering. */
export async function htmlFileToMarkdown(htmlPath: string): Promise<string> {
  const html = await fs.readFile(htmlPath, 'utf-8');
  return htmlToMarkdown(html);
}

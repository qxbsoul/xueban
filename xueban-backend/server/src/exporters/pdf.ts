/**
 * exporters/pdf.ts — Story 15.15
 *
 * HTML → PDF conversion via puppeteer-core + @sparticuz/chromium.
 *
 * Why @sparticuz/chromium: it ships a self-contained, gz-compressed Chromium
 * binary that runs without requiring a system Chrome / Chromium install. Cost:
 * ~50 MB tarball, ~150 MB unpacked. Acceptable for a hackathon-grade product.
 *
 * Failure mode: if the chromium binary fails to launch (corrupted download,
 * missing glibc, sandbox refusal, etc.) the caller should map the thrown
 * error to a 503 PDF_ENGINE_UNAVAILABLE response so the rest of the API stays
 * functional. We do NOT memoise the launch promise across failures — a failed
 * attempt resets `browserPromise` to `null` so the next request retries
 * cleanly (otherwise a single transient launch failure would poison the
 * process for its lifetime).
 */
import puppeteer, { type Browser } from 'puppeteer-core';
import chromium from '@sparticuz/chromium';
import fs from 'fs/promises';

export type PageSize = 'A4' | 'Letter';
export interface Viewport {
  width: number;
  height: number;
}

const VALID_PAGES: readonly PageSize[] = ['A4', 'Letter'] as const;

/** Default print viewport — matches ArtifactPreview iframe at 1280×800. */
export const DEFAULT_VIEWPORT: Viewport = { width: 1280, height: 800 };

/**
 * Parse a `WIDTHxHEIGHT` query-string viewport into a {width, height} object.
 *
 *  - `undefined` / `''`         → DEFAULT_VIEWPORT (no override requested)
 *  - matches `^\d{2,4}x\d{2,4}$` and within reasonable bounds → parsed
 *  - anything else              → `null` (caller returns 400 INVALID_VIEWPORT)
 *
 * Bounds (320..3840 × 240..2160) cover real device sizes from a small phone
 * up to 4K and prevent accidental DoS from `99999x99999` viewports.
 */
export function parseViewport(raw: string | undefined): Viewport | null {
  if (!raw) return DEFAULT_VIEWPORT;
  const m = /^(\d{2,4})x(\d{2,4})$/.exec(raw);
  if (!m) return null;
  const w = Number(m[1]);
  const h = Number(m[2]);
  if (!Number.isFinite(w) || !Number.isFinite(h)) return null;
  if (w < 320 || w > 3840 || h < 240 || h > 2160) return null;
  return { width: w, height: h };
}

/** True when raw is undefined (default A4) or one of the allow-listed sizes. */
export function isValidPage(
  raw: string | undefined,
): raw is PageSize | undefined {
  return raw === undefined || VALID_PAGES.includes(raw as PageSize);
}

let browserPromise: Promise<Browser> | null = null;

async function getBrowser(): Promise<Browser> {
  if (!browserPromise) {
    browserPromise = puppeteer
      .launch({
        args: chromium.args,
        executablePath: await chromium.executablePath(),
        headless: true,
      })
      .catch((err: unknown) => {
        // Don't poison future attempts with a rejected memoised promise.
        browserPromise = null;
        throw err;
      });
  }
  return browserPromise;
}

/**
 * Read an HTML file and render it to a PDF buffer.
 *
 * Throws on chromium launch / navigation failure — caller is responsible for
 * mapping to 503.
 */
export async function htmlFileToPdf(
  htmlPath: string,
  page: PageSize = 'A4',
  viewport: Viewport = DEFAULT_VIEWPORT,
): Promise<Buffer> {
  const html = await fs.readFile(htmlPath, 'utf-8');
  const browser = await getBrowser();
  const tab = await browser.newPage();
  try {
    await tab.setViewport(viewport);
    await tab.setContent(html, { waitUntil: 'networkidle0', timeout: 15_000 });
    const out = await tab.pdf({
      format: page,
      printBackground: true,
      preferCSSPageSize: false,
    });
    return Buffer.from(out);
  } finally {
    await tab.close().catch(() => {
      /* ignore close errors so we don't mask a real upstream failure */
    });
  }
}

/**
 * Test-only: forget the cached browser launch promise so tests can stub the
 * launcher and re-run independently. Not exported through any public surface.
 */
export function __resetBrowserForTests(): void {
  browserPromise = null;
}

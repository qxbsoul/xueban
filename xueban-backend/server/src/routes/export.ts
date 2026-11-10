/**
 * routes/export.ts — Story 15.6 + Story 15.15
 *
 * GET /api/export/:session_id/zip
 *   Streams a ZIP archive of the session's artifact directory under
 *   `.xueban/projects/<session_id>/`. Single files are already served as
 *   static assets via `/projects/<id>/<file>`; this endpoint exists only so
 *   users can grab the whole session at once.
 *
 * GET /api/export/:session_id/pdf?filename=...&page=A4|Letter&viewport=WxH
 *   (15.15) Renders an HTML artifact to PDF via puppeteer-core + chromium.
 *   503 PDF_ENGINE_UNAVAILABLE on chromium launch failure (server stays up).
 *
 * GET /api/export/:session_id/markdown?filename=...
 *   (15.15) Converts an HTML artifact to Markdown via turndown.
 *
 * Security (applies to ALL routes):
 *   - `session_id` must match a strict UUID v4 pattern. This rules out path-
 *     traversal segments like `..`, `.`, and `/`. Note: `crypto.randomUUID()`
 *     emits RFC 4122 v4 UUIDs, so legitimate session IDs always satisfy this.
 *   - `filename` (15.15): must be a basename (no path separators, no `..`,
 *     not absolute). After `path.resolve()` we verify the result still lives
 *     under the project directory — defence in depth.
 *   - We `path.resolve()` the project directory and verify it stays under the
 *     intended `projectsRoot` as belt-and-braces in case the regex is ever
 *     relaxed.
 *
 * Streaming (zip only):
 *   - `archive.pipe(res)` streams chunks; large directories don't buffer.
 *   - On client disconnect, we abort the archive to free file handles.
 */
import { Router, Request, Response } from 'express';
import fs from 'fs';
import path from 'path';
import archiver from 'archiver';
import {
  htmlFileToPdf,
  parseViewport,
  isValidPage,
  type PageSize,
} from '../exporters/pdf';
import { htmlFileToMarkdown } from '../exporters/markdown';

const router = Router();

// RFC 4122 v4 UUID — same shape `crypto.randomUUID()` produces.
const UUID_V4_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function projectsRoot(): string {
  return path.resolve(process.cwd(), '.xueban', 'projects');
}

/** Story 15.15 — resolve <session_id>/<filename> with full sandbox checks.
 *
 * Returns either `{ ok: true, absPath, projectDir }` or
 * `{ ok: false, status, error }`. `error` is the JSON body to send.
 *
 * Layered checks (each independently sufficient):
 *   1. UUID v4 regex on session_id — blocks `..` segments outright.
 *   2. Basename equality — `path.basename(filename) === filename` rejects any
 *      path separator or path-traversal payload.
 *   3. Disallow absolute paths and `..` substrings explicitly.
 *   4. After resolve, assert the absolute file path is still inside the
 *      project directory. This catches bugs in steps 1–3 if any are ever
 *      loosened.
 *   5. Filesystem stat — 404 if the project dir or file is missing.
 */
type ResolveResult =
  | { ok: true; absPath: string; projectDir: string }
  | { ok: false; status: number; error: { code: string; message: string } };

function resolveSessionFile(
  session_id: string,
  filename: string,
): ResolveResult {
  if (!UUID_V4_RE.test(session_id)) {
    return {
      ok: false,
      status: 400,
      error: { code: 'INVALID_SESSION_ID', message: 'Invalid session_id' },
    };
  }

  if (
    !filename ||
    filename.includes('..') ||
    path.isAbsolute(filename) ||
    path.basename(filename) !== filename
  ) {
    return {
      ok: false,
      status: 400,
      error: { code: 'INVALID_FILENAME', message: 'Invalid filename' },
    };
  }

  const root = projectsRoot();
  const projectDir = path.resolve(root, session_id);

  if (
    projectDir !== path.join(root, session_id) ||
    !projectDir.startsWith(root + path.sep)
  ) {
    return {
      ok: false,
      status: 400,
      error: { code: 'INVALID_SESSION_ID', message: 'Invalid session_id' },
    };
  }

  const absPath = path.resolve(projectDir, filename);
  if (!absPath.startsWith(projectDir + path.sep)) {
    return {
      ok: false,
      status: 400,
      error: { code: 'INVALID_FILENAME', message: 'Invalid filename' },
    };
  }

  // 404 NOT_FOUND covers both "session does not exist" and "file does not
  // exist" — we deliberately don't distinguish so callers can't enumerate
  // valid session_ids.
  let stat: fs.Stats;
  try {
    stat = fs.statSync(absPath);
  } catch {
    return {
      ok: false,
      status: 404,
      error: { code: 'NOT_FOUND', message: 'Not found' },
    };
  }
  if (!stat.isFile()) {
    return {
      ok: false,
      status: 404,
      error: { code: 'NOT_FOUND', message: 'Not found' },
    };
  }

  return { ok: true, absPath, projectDir };
}

router.get('/:session_id/zip', (req: Request, res: Response) => {
  const { session_id } = req.params;

  if (!UUID_V4_RE.test(session_id)) {
    res.status(400).json({ error: 'Invalid session_id' });
    return;
  }

  const root = projectsRoot();
  const projectDir = path.resolve(root, session_id);

  // Defence-in-depth: ensure the resolved path stays inside projectsRoot.
  if (
    projectDir !== path.join(root, session_id) ||
    !projectDir.startsWith(root + path.sep)
  ) {
    res.status(400).json({ error: 'Invalid session_id' });
    return;
  }

  let stat: fs.Stats;
  try {
    stat = fs.statSync(projectDir);
  } catch {
    res.status(404).json({ error: 'Session not found' });
    return;
  }
  if (!stat.isDirectory()) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  const zipName = `session-${session_id.slice(0, 8)}.zip`;
  res.setHeader('Content-Type', 'application/zip');
  res.setHeader('Content-Disposition', `attachment; filename="${zipName}"`);

  const archive = archiver('zip', { zlib: { level: 9 } });

  archive.on('warning', (err: Error & { code?: string }) => {
    // ENOENT etc. — log but keep streaming.
    console.warn('[export] archive warning:', err.message);
  });
  archive.on('error', (err: Error) => {
    console.error('[export] archive error:', err);
    if (!res.headersSent) {
      res.status(500).json({ error: 'Archive failed' });
      return;
    }
    try {
      res.end();
    } catch {
      /* noop */
    }
  });

  // Cancel archiving if the client disconnects mid-stream.
  res.on('close', () => {
    if (!res.writableEnded) {
      try {
        archive.abort();
      } catch {
        /* noop */
      }
    }
  });

  archive.pipe(res);
  archive.directory(projectDir, false);
  archive.finalize();
});

// ─────────────────────────────────────────────────────────────────────────────
// Story 15.15 — PDF + Markdown endpoints
// ─────────────────────────────────────────────────────────────────────────────

router.get('/:session_id/pdf', async (req: Request, res: Response) => {
  const { session_id } = req.params;
  const filename = String(req.query.filename ?? '');
  const pageRaw = req.query.page as string | undefined;
  const viewportRaw = req.query.viewport as string | undefined;

  // Validate query params BEFORE the sandbox check so callers get a precise
  // error instead of a generic 400 INVALID_FILENAME.
  if (!isValidPage(pageRaw)) {
    res.status(400).json({
      error: {
        code: 'INVALID_PAGE_SIZE',
        message: 'page must be A4 or Letter',
      },
    });
    return;
  }
  const viewport = parseViewport(viewportRaw);
  if (viewport === null) {
    res.status(400).json({
      error: {
        code: 'INVALID_VIEWPORT',
        message: 'viewport must be WIDTHxHEIGHT (e.g. 1280x800)',
      },
    });
    return;
  }
  if (!filename.toLowerCase().endsWith('.html')) {
    res.status(400).json({
      error: {
        code: 'INVALID_SOURCE_TYPE',
        message: 'PDF export requires an HTML source file',
      },
    });
    return;
  }

  const resolved = resolveSessionFile(session_id, filename);
  if (!resolved.ok) {
    res.status(resolved.status).json({ error: resolved.error });
    return;
  }

  try {
    const pdf = await htmlFileToPdf(
      resolved.absPath,
      (pageRaw ?? 'A4') as PageSize,
      viewport,
    );
    const outName = path.basename(filename, path.extname(filename)) + '.pdf';
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader(
      'Content-Disposition',
      `attachment; filename="${outName}"`,
    );
    res.end(pdf);
  } catch (err) {
    // Chromium launch / navigation failure — emit 503 so server stays up
    // and the client can show a "engine unavailable" toast. Don't leak the
    // error message (might contain absolute paths or env details).
    console.error('[export.pdf] engine failure:', err);
    res.status(503).json({
      error: {
        code: 'PDF_ENGINE_UNAVAILABLE',
        message:
          'PDF export engine unavailable. Please reinstall: npm rebuild @sparticuz/chromium',
      },
    });
  }
});

router.get('/:session_id/markdown', async (req: Request, res: Response) => {
  const { session_id } = req.params;
  const filename = String(req.query.filename ?? '');

  if (!filename.toLowerCase().endsWith('.html')) {
    res.status(400).json({
      error: {
        code: 'INVALID_SOURCE_TYPE',
        message: 'Markdown export requires an HTML source file',
      },
    });
    return;
  }

  const resolved = resolveSessionFile(session_id, filename);
  if (!resolved.ok) {
    res.status(resolved.status).json({ error: resolved.error });
    return;
  }

  try {
    const md = await htmlFileToMarkdown(resolved.absPath);
    const outName = path.basename(filename, path.extname(filename)) + '.md';
    res.setHeader('Content-Type', 'text/markdown; charset=utf-8');
    res.setHeader(
      'Content-Disposition',
      `attachment; filename="${outName}"`,
    );
    res.end(md);
  } catch (err) {
    console.error('[export.md] failure:', err);
    res.status(500).json({
      error: {
        code: 'EXPORT_FAILED',
        message: 'Markdown conversion failed',
      },
    });
  }
});

export default router;
export { UUID_V4_RE };

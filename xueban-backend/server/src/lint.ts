/**
 * lint.ts — Story 15.14 — Static lint for Xueban artifacts
 *
 * 4 languages:
 *   - HTML (htmlparser2): duplicate-id (error) / unclosed-tag (error) / img-missing-alt (info) / missing-lang-attr (info)
 *   - YAML (js-yaml): parse-error (error) / missing-required-field (error for *.blueprint.yml) / empty-collection (warning)
 *   - Markdown (regex): heading-level-skip (warning) / empty-section (info) / sf-tag-malformed (error) / sf-tag-unclosed (error)
 *   - CSS (regex): important-overuse (info) / selector-too-deep (info)
 *
 * Story 15.14 範圍調整 (2026-05-11): YAML schema + <sf:step> 配對是強約束；HTML/CSS 弱化為 info-only.
 *
 * Path resolution: artifacts live under .xueban/projects/<session_id>/<filename>
 *   (matches assembler.ts which writes via input.cwd = .xueban/projects/<session_id>).
 */

import fs from 'fs';
import path from 'path';
import { Parser } from 'htmlparser2';
import yaml from 'js-yaml';

export type LintLanguage = 'html' | 'css' | 'yaml' | 'markdown' | 'unknown';
export type LintSeverity = 'error' | 'warning' | 'info';

export interface LintFinding {
  rule: string;
  severity: LintSeverity;
  message: string;
  line?: number;
  column?: number;
  count?: number;
}

export interface LintResult {
  filename: string;
  type: LintLanguage;
  /** Alias of {@link type}. The Story spec uses `language`; we expose both for API stability. */
  language: LintLanguage;
  findings: LintFinding[];
  summary: { errors: number; warnings: number; infos: number };
  _meta?: {
    sf_steps_seen?: string[];
    sf_steps_completed?: string[];
  };
}

/** Project directory resolution — matches assembler.ts (.xueban/projects/<id>). */
export function projectArtifactPath(sessionId: string, filename: string): string {
  return path.join(process.cwd(), '.xueban', 'projects', sessionId, filename);
}

/** Allow alphanumerics, dash, underscore — matches uuid v4 pattern + extra safety. */
const SAFE_ID_RE = /^[a-zA-Z0-9_-]+$/;

export function isSafeSessionId(id: string): boolean {
  return typeof id === 'string' && id.length > 0 && id.length <= 128 && SAFE_ID_RE.test(id);
}

export function isSafeFilename(name: string): boolean {
  if (typeof name !== 'string' || name.length === 0 || name.length > 256) return false;
  if (name.includes('..') || name.includes('/') || name.includes('\\')) return false;
  // Disallow leading dot to prevent reads of dot-files (.env etc).
  if (name.startsWith('.')) return false;
  return true;
}

function summarize(findings: LintFinding[]) {
  return {
    errors: findings.filter(f => f.severity === 'error').length,
    warnings: findings.filter(f => f.severity === 'warning').length,
    infos: findings.filter(f => f.severity === 'info').length,
  };
}

function detectLanguage(filename: string, explicit?: string): LintLanguage {
  if (explicit) {
    const t = explicit.toLowerCase();
    if (t === 'html' || t === 'css' || t === 'yaml' || t === 'markdown') return t;
  }
  const ext = path.extname(filename).toLowerCase();
  if (ext === '.html' || ext === '.htm') return 'html';
  if (ext === '.css') return 'css';
  if (ext === '.yml' || ext === '.yaml') return 'yaml';
  if (ext === '.md' || ext === '.markdown') return 'markdown';
  return 'unknown';
}

/** Public surface. Reads the file from disk, dispatches by extension, returns LintResult. */
// 2026-05-11 review F3: 上限 512KB 防 lintHtml O(N·M) 全文扫 + readFileSync OOM。
// OpenDesign 同模式（artifact 上限统一兜底）。超限 → 单 info finding，不解析。
const MAX_ARTIFACT_BYTES = 512 * 1024;

export function runLint(sessionId: string, filename: string, type?: string): LintResult {
  if (!isSafeSessionId(sessionId)) throw Object.assign(new Error('INVALID_SESSION_ID'), { code: 'INVALID_SESSION_ID' });
  if (!isSafeFilename(filename)) throw Object.assign(new Error('INVALID_FILENAME'), { code: 'INVALID_FILENAME' });

  const filePath = projectArtifactPath(sessionId, filename);
  if (!fs.existsSync(filePath)) {
    throw Object.assign(new Error('ARTIFACT_NOT_FOUND'), { code: 'ARTIFACT_NOT_FOUND' });
  }
  // F3: stat-then-read 防整文件读到内存。
  const stat = fs.statSync(filePath);
  if (stat.size > MAX_ARTIFACT_BYTES) {
    const lang = detectLanguage(filename, type);
    return {
      filename,
      type: lang,
      language: lang,
      findings: [
        {
          severity: 'info',
          rule: 'artifact-too-large',
          line: 1,
          column: 1,
          message: `Artifact size ${stat.size}B exceeds lint cap ${MAX_ARTIFACT_BYTES}B — skipped`,
        },
      ],
      summary: { errors: 0, warnings: 0, infos: 1 },
    };
  }
  const content = fs.readFileSync(filePath, 'utf-8');
  return lintContent(filename, content, type);
}

/** Pure content lint (no filesystem) — used by tests + the route layer alike. */
export function lintContent(filename: string, content: string, type?: string): LintResult {
  const language = detectLanguage(filename, type);
  let result: LintResult;
  if (language === 'html') result = lintHtml(filename, content);
  else if (language === 'css') result = lintCss(filename, content);
  else if (language === 'yaml') result = lintYaml(filename, content);
  else if (language === 'markdown') result = lintMarkdown(filename, content);
  else {
    result = {
      filename,
      type: 'unknown',
      language: 'unknown',
      findings: [],
      summary: { errors: 0, warnings: 0, infos: 0 },
    };
  }
  result.summary = summarize(result.findings);
  return result;
}

// ─── HTML lint ────────────────────────────────────────────────────────────────

function lintHtml(filename: string, content: string): LintResult {
  const findings: LintFinding[] = [];
  const idMap = new Map<string, number>();
  let htmlHasLang = false;
  // Track tag stack for unclosed-tag detection. htmlparser2 onclosetag fires
  // even for self-closing void tags, but we only count *block* mismatches.
  const VOID_TAGS = new Set([
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
    'link', 'meta', 'param', 'source', 'track', 'wbr',
  ]);
  const stack: Array<{ name: string; line: number }> = [];

  // Fast line counter — htmlparser2 doesn't surface positions in v9, so we track
  // newlines manually as we feed slices to the parser.
  let cursor = 0;
  const lineOffsets: number[] = [0];
  for (let i = 0; i < content.length; i++) {
    if (content.charCodeAt(i) === 10) lineOffsets.push(i + 1);
  }
  const lineAt = (offset: number) => {
    // Binary search would be faster, but linear is fine for typical artifacts.
    let lo = 0, hi = lineOffsets.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (lineOffsets[mid] <= offset) lo = mid; else hi = mid - 1;
    }
    return lo + 1;
  };

  // Track open positions for line numbering: we look for the latest "<name" before cursor.
  const findOpenLine = (name: string): number => {
    // Scan backwards from cursor for "<name" to grab the line.
    const re = new RegExp(`<${name}\\b`, 'gi');
    let m: RegExpExecArray | null;
    let lastIdx = -1;
    while ((m = re.exec(content)) !== null) {
      if (m.index < cursor) lastIdx = m.index;
      else break;
    }
    return lastIdx >= 0 ? lineAt(lastIdx) : 1;
  };

  const parser = new Parser(
    {
      onopentag(name, attribs) {
        const line = findOpenLine(name);
        if (name === 'html' && attribs.lang) htmlHasLang = true;
        if (name === 'img' && !attribs.alt) {
          findings.push({
            rule: 'img-missing-alt',
            severity: 'info',
            message: '<img> tag missing alt attribute',
            line,
          });
        }
        if (attribs.id) {
          const prev = idMap.get(attribs.id);
          if (prev !== undefined) {
            findings.push({
              rule: 'duplicate-id',
              severity: 'error',
              message: `Duplicate id '${attribs.id}' (earlier at line ${prev})`,
              line,
            });
          } else {
            idMap.set(attribs.id, line);
          }
        }
        if (!VOID_TAGS.has(name)) {
          stack.push({ name, line });
        }
      },
      onclosetag(name) {
        if (VOID_TAGS.has(name)) return;
        // Pop matching from stack.
        for (let i = stack.length - 1; i >= 0; i--) {
          if (stack[i].name === name) {
            stack.splice(i, 1);
            return;
          }
        }
      },
      ontext(text) {
        cursor += text.length;
      },
    },
    { decodeEntities: false, lowerCaseTags: true, lowerCaseAttributeNames: true },
  );

  // Drive cursor: write the whole content (htmlparser2 will call ontext for text runs;
  // we approximate cursor based on text-length sums, sufficient for line numbers).
  parser.write(content);
  parser.end();

  // ── unclosed-tag detection ─────────────────────────────────────────────────
  // htmlparser2 auto-closes mismatched tags so the stack is empty at end. We
  // do an independent regex count: for each non-void tag with at least one
  // open occurrence, compare open vs close counts. Mismatch => report.
  // This catches the common LLM failure of `<span>...` with no `</span>`.
  const openCounts = new Map<string, number>();
  const closeCounts = new Map<string, number>();
  // Strip script/style block contents to avoid false positives from JS/CSS code.
  const sanitized = content
    .replace(/<script\b[\s\S]*?<\/script\s*>/gi, '')
    .replace(/<style\b[\s\S]*?<\/style\s*>/gi, '')
    .replace(/<!--[\s\S]*?-->/g, '');
  const openTagRe = /<([a-zA-Z][a-zA-Z0-9-]*)(\s[^>]*?)?\/?>/g;
  const closeTagRe = /<\/([a-zA-Z][a-zA-Z0-9-]*)\s*>/g;
  let tm: RegExpExecArray | null;
  while ((tm = openTagRe.exec(sanitized)) !== null) {
    const name = tm[1].toLowerCase();
    if (VOID_TAGS.has(name)) continue;
    // Self-closing in source (e.g. <br />)?
    if (tm[0].endsWith('/>')) continue;
    openCounts.set(name, (openCounts.get(name) ?? 0) + 1);
  }
  while ((tm = closeTagRe.exec(sanitized)) !== null) {
    const name = tm[1].toLowerCase();
    closeCounts.set(name, (closeCounts.get(name) ?? 0) + 1);
  }
  for (const [name, opened] of openCounts.entries()) {
    const closed = closeCounts.get(name) ?? 0;
    if (opened > closed) {
      findings.push({
        rule: 'unclosed-tag',
        severity: 'error',
        message: `<${name}> opened ${opened} time(s) but closed ${closed} time(s)`,
      });
    }
  }

  if (!htmlHasLang) {
    // Only flag if there's an <html> element at all. If not, still useful for fragments
    // — degrade severity to info as Story spec says.
    findings.push({
      rule: 'missing-lang-attr',
      severity: 'info',
      message: '<html> missing lang attribute (or no <html> element)',
    });
  }

  return {
    filename,
    type: 'html',
    language: 'html',
    findings,
    summary: { errors: 0, warnings: 0, infos: 0 },
  };
}

// ─── CSS lint ─────────────────────────────────────────────────────────────────

function lintCss(filename: string, content: string): LintResult {
  const findings: LintFinding[] = [];
  const importantCount = (content.match(/!important/gi) || []).length;
  if (importantCount >= 5) {
    findings.push({
      rule: 'important-overuse',
      severity: 'info',
      message: `${importantCount} !important declarations`,
      count: importantCount,
    });
  }
  let depth = 0;
  let maxDepth = 0;
  for (let i = 0; i < content.length; i++) {
    const ch = content.charCodeAt(i);
    if (ch === 123 /* { */) {
      depth += 1;
      if (depth > maxDepth) maxDepth = depth;
    } else if (ch === 125 /* } */) {
      depth = Math.max(0, depth - 1);
    }
  }
  if (maxDepth >= 4) {
    findings.push({
      rule: 'selector-too-deep',
      severity: 'info',
      message: `selector nested ${maxDepth} levels deep`,
      count: maxDepth,
    });
  }
  return {
    filename,
    type: 'css',
    language: 'css',
    findings,
    summary: { errors: 0, warnings: 0, infos: 0 },
  };
}

// ─── YAML lint ────────────────────────────────────────────────────────────────

function lintYaml(filename: string, content: string): LintResult {
  const findings: LintFinding[] = [];
  let parsed: unknown = undefined;
  try {
    parsed = yaml.load(content);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    const mark = (err as { mark?: { line?: number } } | undefined)?.mark;
    findings.push({
      rule: 'parse-error',
      severity: 'error',
      message: `YAML parse error: ${msg.split('\n')[0]}`,
      line: mark?.line !== undefined ? mark.line + 1 : undefined,
    });
    // Cannot continue schema check when parse fails.
    return {
      filename,
      type: 'yaml',
      language: 'yaml',
      findings,
      summary: { errors: 0, warnings: 0, infos: 0 },
    };
  }

  // *.blueprint.yml schema: top-level agents / skills / policy_matrix required.
  const lower = filename.toLowerCase();
  const isBlueprint = lower.endsWith('.blueprint.yml') || lower.endsWith('.blueprint.yaml');
  if (isBlueprint) {
    const obj = (parsed && typeof parsed === 'object' && !Array.isArray(parsed))
      ? (parsed as Record<string, unknown>)
      : null;
    if (!obj) {
      findings.push({
        rule: 'parse-error',
        severity: 'error',
        message: 'blueprint root must be a mapping/object',
      });
    } else {
      for (const key of ['agents', 'skills', 'policy_matrix']) {
        if (!(key in obj)) {
          findings.push({
            rule: 'missing-required-field',
            severity: 'error',
            message: `blueprint missing top-level '${key}'`,
          });
        }
      }
      // empty-collection: agents / skills present but empty array / null.
      for (const key of ['agents', 'skills']) {
        if (key in obj) {
          const v = obj[key];
          const isEmptyArray = Array.isArray(v) && v.length === 0;
          const isNull = v === null || v === undefined;
          if (isEmptyArray || isNull) {
            findings.push({
              rule: 'empty-collection',
              severity: 'warning',
              message: `blueprint '${key}' list is empty`,
            });
          }
        }
      }
    }
  }

  return {
    filename,
    type: 'yaml',
    language: 'yaml',
    findings,
    summary: { errors: 0, warnings: 0, infos: 0 },
  };
}

// ─── Markdown lint ────────────────────────────────────────────────────────────

function lintMarkdown(filename: string, content: string): LintResult {
  const findings: LintFinding[] = [];
  const lines = content.split(/\r?\n/);

  // Heading + empty-section state machine.
  let prevLevel = 0;
  let lastHeaderLine = -1;
  let lastHeaderHadContent = true;

  // Track if we're inside a fenced code block — headings inside should be ignored.
  let inFence = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^```/.test(line)) {
      inFence = !inFence;
      continue;
    }
    if (inFence) continue;
    const m = line.match(/^(#{1,6})\s+/);
    if (m) {
      const level = m[1].length;
      if (prevLevel > 0 && level > prevLevel + 1) {
        findings.push({
          rule: 'heading-level-skip',
          severity: 'warning',
          message: `heading skips from h${prevLevel} to h${level}`,
          line: i + 1,
        });
      }
      if (lastHeaderLine >= 0 && !lastHeaderHadContent) {
        findings.push({
          rule: 'empty-section',
          severity: 'info',
          message: 'heading with no content below',
          line: lastHeaderLine + 1,
        });
      }
      prevLevel = level;
      lastHeaderLine = i;
      lastHeaderHadContent = false;
    } else if (line.trim().length > 0) {
      lastHeaderHadContent = true;
    }
  }

  // <sf:step> pairing — first pass: collect ALL <sf:step ...>...</sf:step> occurrences
  // (even malformed ones), classify as malformed / valid.
  const stepStates = new Map<string, string[]>();
  const seenStepsOrdered: string[] = [];

  // Match any <sf:step (attrs)?>(content)</sf:step> ; case-insensitive; non-greedy.
  const allStepRe = /<sf:step\b([^>]*)>([\s\S]*?)<\/sf:step\s*>/gi;
  let mm: RegExpExecArray | null;
  while ((mm = allStepRe.exec(content)) !== null) {
    const attrs = mm[1] ?? '';
    const inner = (mm[2] ?? '').trim().toLowerCase();
    const lineNo = content.slice(0, mm.index).split(/\r?\n/).length;
    const nameMatch = attrs.match(/name\s*=\s*["']([^"']+)["']/);
    if (!nameMatch) {
      findings.push({
        rule: 'sf-tag-malformed',
        severity: 'error',
        message: '<sf:step> missing name attribute',
        line: lineNo,
      });
      continue;
    }
    const name = nameMatch[1];
    if (inner !== 'running' && inner !== 'done') {
      findings.push({
        rule: 'sf-tag-malformed',
        severity: 'error',
        message: `<sf:step name="${name}"> body must be 'running' or 'done', got '${inner.slice(0, 30)}'`,
        line: lineNo,
      });
      continue;
    }
    const arr = stepStates.get(name) ?? [];
    if (arr.length === 0) seenStepsOrdered.push(name);
    arr.push(inner);
    stepStates.set(name, arr);
  }

  // Pairing: every name needs at least one 'done'.
  const stepsCompleted: string[] = [];
  for (const [name, states] of stepStates.entries()) {
    if (!states.includes('done')) {
      findings.push({
        rule: 'sf-tag-unclosed',
        severity: 'error',
        message: `<sf:step name="${name}"> never closed with done`,
      });
    } else {
      stepsCompleted.push(name);
    }
  }

  return {
    filename,
    type: 'markdown',
    language: 'markdown',
    findings,
    summary: { errors: 0, warnings: 0, infos: 0 },
    _meta: {
      sf_steps_seen: seenStepsOrdered,
      sf_steps_completed: stepsCompleted,
    },
  };
}

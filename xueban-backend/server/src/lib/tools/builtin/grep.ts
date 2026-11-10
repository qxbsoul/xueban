/**
 * grep.ts — builtin `grep` tool.
 *
 * Searches workspace files for a regex pattern. Default mode ALLOW.
 *
 * We don't bundle ripgrep (no native binary path on Windows in this repo),
 * so we implement a JS scan with a strict budget: MAX_GREP_BYTES (10 MiB)
 * across all files combined. Per-file we cap line length so a giant minified
 * blob can't blow up memory. Binary files are skipped (we sniff for NUL
 * bytes in the first 8 KB).
 *
 * Output is similar to `rg --no-heading --line-number`:
 *   { pattern, path, matches: [{ file, line, text }] }
 *
 * Sandbox:
 *   - `path` is workspace-relative; defaults to ".".
 *   - We never follow symlinks during the walk (`fs.readdir withFileTypes`
 *     returns symlink Dirents which we skip).
 *   - Deny-list `.git`, `node_modules` paths during walk.
 *   - Honours ctx.signal between files.
 */

import fs from 'fs';
import path from 'path';
import type { ToolSpec } from '../../tool-spec';
import { resolveSandboxedPath, MAX_GREP_BYTES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const grepTool: ToolSpec = {
  name: 'grep',
  description:
    'Search workspace text files for a regex pattern. Returns up to 200 matches with file/line/text. ' +
    'Binary files are skipped. Total scan budget is 10 MiB.',
  input_schema: {
    type: 'object',
    properties: {
      pattern: { type: 'string', description: 'JavaScript-flavoured regex.' },
      path: {
        type: 'string',
        description: 'Optional workspace-relative directory or file to scan. Defaults to workspace root.',
      },
      flags: {
        type: 'string',
        description: 'Optional regex flags (e.g. "i" for case-insensitive). Defaults to none.',
      },
      max_matches: {
        type: 'number',
        description: 'Cap on returned matches (default 200, hard max 1000).',
      },
    },
    required: ['pattern'],
    additionalProperties: false,
  },
  source: 'base',
};

interface GrepInput {
  pattern: string;
  path?: string;
  flags?: string;
  max_matches?: number;
}

function isGrepInput(x: unknown): x is GrepInput {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as { pattern: unknown }).pattern === 'string'
  );
}

const PER_FILE_LINE_CAP = 4096;
const HARD_MATCH_CAP = 1000;

function looksBinary(buf: Buffer): boolean {
  const n = Math.min(buf.length, 8192);
  for (let i = 0; i < n; i++) if (buf[i] === 0) return true;
  return false;
}

export const grepExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isGrepInput(input)) {
    return { output: { error: 'grep: input must be { pattern, path?, flags?, max_matches? }' }, isError: true };
  }

  // Validate flags conservatively — only allow standard JS flags.
  const flags = (input.flags ?? '').replace(/[^gimsuy]/g, '');
  let re: RegExp;
  try {
    re = new RegExp(input.pattern, flags);
  } catch (err) {
    return { output: { error: `grep: invalid pattern: ${(err as Error).message}` }, isError: true };
  }

  const startRel = input.path ?? '.';
  const resolved = resolveSandboxedPath(ctx.workspace, startRel, { mustExist: true });
  if (!resolved.ok) {
    return { output: { error: `grep denied: ${resolved.reason}` }, isError: true };
  }

  const maxMatches = Math.min(input.max_matches ?? 200, HARD_MATCH_CAP);
  const matches: Array<{ file: string; line: number; text: string }> = [];
  let bytesScanned = 0;
  let filesScanned = 0;

  // Build the file list — single file or recursive walk
  let files: string[];
  try {
    const st = await fs.promises.stat(resolved.absPath);
    if (st.isFile()) {
      files = [resolved.absPath];
    } else {
      const dents = await fs.promises.readdir(resolved.absPath, {
        recursive: true,
        withFileTypes: true,
      });
      files = [];
      for (const d of dents) {
        if (!d.isFile()) continue;
        const parent = (d as unknown as { parentPath?: string; path?: string }).parentPath
          ?? (d as unknown as { path?: string }).path
          ?? resolved.absPath;
        const abs = path.join(parent, d.name);
        const relFromWs = path.relative(ctx.workspace, abs);
        if (relFromWs.split(/[\\/]/).some((c) => c === '.git' || c === 'node_modules')) continue;
        files.push(abs);
      }
    }
  } catch (err) {
    return { output: { error: `grep walk failed: ${(err as Error).message}` }, isError: true };
  }

  for (const abs of files) {
    if (ctx.signal.aborted) {
      return { output: { error: 'grep aborted' }, isError: true };
    }
    if (matches.length >= maxMatches) break;
    if (bytesScanned >= MAX_GREP_BYTES) break;

    let buf: Buffer;
    try {
      buf = await fs.promises.readFile(abs);
    } catch {
      continue;
    }
    if (looksBinary(buf)) continue;
    bytesScanned += buf.length;
    filesScanned++;

    const text = buf.toString('utf8');
    const lines = text.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      const snippet = ln.length > PER_FILE_LINE_CAP ? ln.slice(0, PER_FILE_LINE_CAP) + '…' : ln;
      if (re.test(snippet)) {
        matches.push({
          file: path.relative(ctx.workspace, abs).replace(/\\/g, '/'),
          line: i + 1,
          text: snippet,
        });
        if (matches.length >= maxMatches) break;
      }
      // re.lastIndex reset for non-global regex
      re.lastIndex = 0;
    }
  }

  return {
    output: {
      pattern: input.pattern,
      flags,
      path: startRel,
      files_scanned: filesScanned,
      bytes_scanned: bytesScanned,
      truncated: matches.length >= maxMatches || bytesScanned >= MAX_GREP_BYTES,
      matches,
    },
  };
};

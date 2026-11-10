/**
 * list-dir.ts — builtin `list_dir` tool.
 *
 * Lists immediate children of a directory inside the workspace. Default mode
 * is ALLOW (read-tier). Caps the result at MAX_DIR_ENTRIES (200) — when the
 * directory is bigger we return the first 200 and flag `truncated: true` so
 * the LLM doesn't pretend it saw everything.
 *
 * Entries shaped as `{ name, type }` where type ∈ `'file' | 'dir' | 'other'`.
 * Hidden entries (leading `.`) are included — the LLM may need to see
 * `.xueban/` etc. The DENY_COMPONENTS check in resolveSandboxedPath
 * already blocks `.git` and `node_modules` so listing them is impossible.
 */

import fs from 'fs';
import path from 'path';
import type { ToolSpec } from '../../tool-spec';
import { resolveSandboxedPath, MAX_DIR_ENTRIES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const listDirTool: ToolSpec = {
  name: 'list_dir',
  description:
    'List the immediate children of a directory inside the workspace. ' +
    'Returns up to 200 entries (truncated flag set when there are more). ' +
    'Use "." for the workspace root.',
  input_schema: {
    type: 'object',
    properties: {
      path: {
        type: 'string',
        description: 'Workspace-relative directory path. Use "." for workspace root.',
      },
    },
    required: ['path'],
    additionalProperties: false,
  },
  source: 'base',
};

interface ListDirInput {
  path: string;
}

function isListDirInput(x: unknown): x is ListDirInput {
  return typeof x === 'object' && x !== null && typeof (x as { path: unknown }).path === 'string';
}

export const listDirExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isListDirInput(input)) {
    return { output: { error: 'list_dir: input must be { path: string }' }, isError: true };
  }

  const resolved = resolveSandboxedPath(ctx.workspace, input.path, { mustExist: true });
  if (!resolved.ok) {
    return { output: { error: `list_dir denied: ${resolved.reason}` }, isError: true };
  }

  try {
    const stat = await fs.promises.stat(resolved.absPath);
    if (!stat.isDirectory()) {
      return {
        output: { error: `list_dir: path is not a directory: ${input.path}` },
        isError: true,
      };
    }

    const all = await fs.promises.readdir(resolved.absPath, { withFileTypes: true });
    const truncated = all.length > MAX_DIR_ENTRIES;
    const slice = all.slice(0, MAX_DIR_ENTRIES);

    const entries = slice.map((d) => ({
      name: d.name,
      type: d.isDirectory() ? 'dir' : d.isFile() ? 'file' : 'other',
    }));

    if (ctx.signal.aborted) {
      return { output: { error: 'list_dir aborted' }, isError: true };
    }

    return {
      output: {
        path: input.path,
        count: entries.length,
        total: all.length,
        truncated,
        entries,
      },
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `list_dir failed: ${msg}` }, isError: true };
  }
  // path unused except by sandbox util; suppress lint if any
  void path;
};

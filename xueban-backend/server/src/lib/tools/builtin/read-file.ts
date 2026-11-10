/**
 * read-file.ts — builtin `read_file` tool.
 *
 * Reads a single text file inside the run workspace and returns its content.
 * Default permission mode: ALLOW (read-tier).
 *
 * Sandbox contract (see __tests__/sandbox.test.ts):
 *   - input.path is resolved relative to ctx.workspace.
 *   - The resolved real path MUST be a descendant of the resolved workspace
 *     real path. Anything that escapes (`..`, absolute paths, symlink targets
 *     pointing outside) is rejected with `isError: true` and a deny reason.
 *   - Hard 5 MiB cap on file size — larger files return a friendly truncation
 *     notice rather than blowing the LLM's context.
 *   - Deny-list directories (`.git`, `node_modules`) are blocked even when
 *     they live inside the workspace.
 *
 * Output shape: `{ path, bytes, content }`. `content` is the raw UTF-8 text.
 * `path` echoes the input (relative) so the LLM can reference it later.
 */

import fs from 'fs';
import path from 'path';
import type { ToolSpec } from '../../tool-spec';
import { resolveSandboxedPath, MAX_READ_BYTES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const readFileTool: ToolSpec = {
  name: 'read_file',
  description:
    'Read the contents of a text file inside the run workspace. Path is resolved relative to the workspace root. ' +
    'Files larger than 5 MiB are truncated. Use list_dir or glob_files to discover paths first.',
  input_schema: {
    type: 'object',
    properties: {
      path: {
        type: 'string',
        description: 'Workspace-relative path. Must not escape the workspace (no `..`, no absolute paths).',
      },
    },
    required: ['path'],
    additionalProperties: false,
  },
  source: 'base',
};

interface ReadFileInput {
  path: string;
}

function isReadFileInput(x: unknown): x is ReadFileInput {
  return typeof x === 'object' && x !== null && typeof (x as { path: unknown }).path === 'string';
}

export const readFileExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isReadFileInput(input)) {
    return { output: { error: 'read_file: input must be { path: string }' }, isError: true };
  }

  const resolved = resolveSandboxedPath(ctx.workspace, input.path);
  if (!resolved.ok) {
    return { output: { error: `read_file denied: ${resolved.reason}` }, isError: true };
  }

  try {
    const stat = await fs.promises.stat(resolved.absPath);
    if (stat.isDirectory()) {
      return {
        output: { error: `read_file: path is a directory, use list_dir: ${input.path}` },
        isError: true,
      };
    }
    if (stat.size > MAX_READ_BYTES) {
      const fd = await fs.promises.open(resolved.absPath, 'r');
      try {
        const buf = Buffer.alloc(MAX_READ_BYTES);
        await fd.read(buf, 0, MAX_READ_BYTES, 0);
        return {
          output: {
            path: input.path,
            bytes: stat.size,
            truncated: true,
            content: buf.toString('utf8'),
            note: `file size ${stat.size} exceeds ${MAX_READ_BYTES}; truncated to first ${MAX_READ_BYTES} bytes`,
          },
        };
      } finally {
        await fd.close();
      }
    }
    const content = await fs.promises.readFile(resolved.absPath, 'utf8');
    return { output: { path: input.path, bytes: stat.size, content } };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `read_file failed: ${msg}` }, isError: true };
  } finally {
    // signal-aware: nothing async to abort, but check after I/O for fast-fail
    if (ctx.signal.aborted) {
      // best-effort; output already prepared
    }
  }
};

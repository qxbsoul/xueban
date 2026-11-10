/**
 * edit-file.ts — builtin `edit_file` tool.
 *
 * Surgical edit: replace `old_string` with `new_string` in an existing file
 * inside the workspace. Same default-PROMPT mode as write_file. Mirrors the
 * Claude Code / SDK Edit tool semantics:
 *
 *   - `old_string` must appear EXACTLY once (so the edit is unambiguous).
 *     Zero matches → error. Multiple matches → error unless `replace_all`
 *     is set, in which case all occurrences are replaced.
 *   - The file must exist — this tool does NOT create files (use write_file).
 *   - `old_string` and `new_string` must differ.
 *
 * Output: `{ path, bytes, replacements }`.
 */

import fs from 'fs';
import type { ToolSpec } from '../../tool-spec';
import { resolveSandboxedPath, MAX_READ_BYTES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const editFileTool: ToolSpec = {
  name: 'edit_file',
  description:
    'Replace old_string with new_string in an existing workspace file. ' +
    'old_string must be unique unless replace_all=true. File must exist. ' +
    'Default permission mode is PROMPT.',
  input_schema: {
    type: 'object',
    properties: {
      path: { type: 'string', description: 'Workspace-relative path to an existing file.' },
      old_string: { type: 'string', description: 'Exact substring to find. Must appear once unless replace_all.' },
      new_string: { type: 'string', description: 'Replacement text. Must differ from old_string.' },
      replace_all: { type: 'boolean', description: 'Replace every occurrence (default false).' },
    },
    required: ['path', 'old_string', 'new_string'],
    additionalProperties: false,
  },
  source: 'base',
};

interface EditFileInput {
  path: string;
  old_string: string;
  new_string: string;
  replace_all?: boolean;
}

function isEditFileInput(x: unknown): x is EditFileInput {
  if (typeof x !== 'object' || x === null) return false;
  const o = x as { path?: unknown; old_string?: unknown; new_string?: unknown; replace_all?: unknown };
  return (
    typeof o.path === 'string' &&
    typeof o.old_string === 'string' &&
    typeof o.new_string === 'string' &&
    (o.replace_all === undefined || typeof o.replace_all === 'boolean')
  );
}

function countOccurrences(haystack: string, needle: string): number {
  if (needle.length === 0) return 0;
  let n = 0;
  let i = 0;
  while ((i = haystack.indexOf(needle, i)) !== -1) {
    n++;
    i += needle.length;
  }
  return n;
}

export const editFileExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isEditFileInput(input)) {
    return {
      output: { error: 'edit_file: input must be { path, old_string, new_string, replace_all? }' },
      isError: true,
    };
  }
  if (input.old_string === input.new_string) {
    return { output: { error: 'edit_file: old_string and new_string must differ' }, isError: true };
  }
  if (input.old_string.length === 0) {
    return { output: { error: 'edit_file: old_string must be non-empty' }, isError: true };
  }

  const resolved = resolveSandboxedPath(ctx.workspace, input.path, { mustExist: true });
  if (!resolved.ok) {
    return { output: { error: `edit_file denied: ${resolved.reason}` }, isError: true };
  }

  let original: string;
  try {
    const st = await fs.promises.stat(resolved.absPath);
    if (!st.isFile()) {
      return { output: { error: `edit_file: not a regular file: ${input.path}` }, isError: true };
    }
    if (st.size > MAX_READ_BYTES) {
      return {
        output: { error: `edit_file: file exceeds ${MAX_READ_BYTES} bytes` },
        isError: true,
      };
    }
    original = await fs.promises.readFile(resolved.absPath, 'utf8');
  } catch (err) {
    return { output: { error: `edit_file read failed: ${(err as Error).message}` }, isError: true };
  }

  const count = countOccurrences(original, input.old_string);
  if (count === 0) {
    return { output: { error: `edit_file: old_string not found in ${input.path}` }, isError: true };
  }
  if (count > 1 && !input.replace_all) {
    return {
      output: {
        error: `edit_file: old_string occurs ${count} times in ${input.path}; pass replace_all:true or expand context`,
      },
      isError: true,
    };
  }

  const updated = input.replace_all
    ? original.split(input.old_string).join(input.new_string)
    : original.replace(input.old_string, input.new_string);

  if (Buffer.byteLength(updated, 'utf8') > MAX_READ_BYTES) {
    return {
      output: { error: `edit_file: result would exceed ${MAX_READ_BYTES} bytes` },
      isError: true,
    };
  }

  if (ctx.signal.aborted) {
    return { output: { error: 'edit_file aborted before write' }, isError: true };
  }

  try {
    await fs.promises.writeFile(resolved.absPath, updated, 'utf8');
    return {
      output: {
        path: input.path,
        bytes: Buffer.byteLength(updated, 'utf8'),
        replacements: count,
      },
    };
  } catch (err) {
    return { output: { error: `edit_file write failed: ${(err as Error).message}` }, isError: true };
  }
};

/**
 * write-file.ts — builtin `write_file` tool.
 *
 * Creates or overwrites a file inside the workspace. Default mode is PROMPT
 * (v1 behaviour: deny + SSE notify — the actual prompt UX is Lane 1's
 * territory). The executor itself does NOT consult the permission policy;
 * that's tool-runner.ts's job. By the time the executor runs the policy has
 * already said yes.
 *
 * Sandbox:
 *   - resolveSandboxedPath with `mustExist: false` (we're creating the file).
 *   - The target's PARENT must already exist OR be within workspace (we
 *     mkdir -p as needed, capped at 8 levels to avoid filesystem abuse).
 *   - Refuses to write to .git or node_modules (handled by sandbox util).
 *   - Hard 5 MiB content cap on `content` input.
 *
 * Output: `{ path, bytes, created }` where `created` is true on new files,
 * false on overwrite.
 */

import fs from 'fs';
import path from 'path';
import type { ToolSpec } from '../../tool-spec';
import { resolveSandboxedPath, MAX_READ_BYTES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const writeFileTool: ToolSpec = {
  name: 'write_file',
  description:
    'Write text content to a file inside the workspace, creating or overwriting. ' +
    'Parent directories are created as needed. Content max 5 MiB. ' +
    'Default permission mode is PROMPT — caller must approve before the runtime invokes this.',
  input_schema: {
    type: 'object',
    properties: {
      path: { type: 'string', description: 'Workspace-relative target path.' },
      content: { type: 'string', description: 'UTF-8 text content to write.' },
    },
    required: ['path', 'content'],
    additionalProperties: false,
  },
  source: 'base',
};

interface WriteFileInput {
  path: string;
  content: string;
}

function isWriteFileInput(x: unknown): x is WriteFileInput {
  if (typeof x !== 'object' || x === null) return false;
  const o = x as { path?: unknown; content?: unknown };
  return typeof o.path === 'string' && typeof o.content === 'string';
}

const MAX_MKDIR_DEPTH = 8;

export const writeFileExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isWriteFileInput(input)) {
    return { output: { error: 'write_file: input must be { path: string, content: string }' }, isError: true };
  }
  if (Buffer.byteLength(input.content, 'utf8') > MAX_READ_BYTES) {
    return {
      output: { error: `write_file: content exceeds ${MAX_READ_BYTES} bytes` },
      isError: true,
    };
  }

  const resolved = resolveSandboxedPath(ctx.workspace, input.path);
  if (!resolved.ok) {
    return { output: { error: `write_file denied: ${resolved.reason}` }, isError: true };
  }

  // Parent dir creation, bounded.
  const parent = path.dirname(resolved.absPath);
  try {
    // Refuse to create more than MAX_MKDIR_DEPTH new dirs.
    let toCreate = 0;
    let probe = parent;
    while (!fs.existsSync(probe)) {
      toCreate++;
      if (toCreate > MAX_MKDIR_DEPTH) {
        return {
          output: { error: `write_file: target requires creating more than ${MAX_MKDIR_DEPTH} dirs` },
          isError: true,
        };
      }
      const p2 = path.dirname(probe);
      if (p2 === probe) break;
      probe = p2;
    }
    await fs.promises.mkdir(parent, { recursive: true });
  } catch (err) {
    return { output: { error: `write_file mkdir failed: ${(err as Error).message}` }, isError: true };
  }

  let created = true;
  try {
    await fs.promises.access(resolved.absPath);
    created = false;
  } catch {
    /* file does not exist — it's a create */
  }

  if (ctx.signal.aborted) {
    return { output: { error: 'write_file aborted before write' }, isError: true };
  }

  try {
    await fs.promises.writeFile(resolved.absPath, input.content, 'utf8');
    const bytes = Buffer.byteLength(input.content, 'utf8');
    return { output: { path: input.path, bytes, created } };
  } catch (err) {
    return { output: { error: `write_file failed: ${(err as Error).message}` }, isError: true };
  }
};

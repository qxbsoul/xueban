/**
 * glob-files.ts — builtin `glob_files` tool.
 *
 * Matches files inside the workspace against a glob pattern. Default mode
 * ALLOW. We deliberately avoid taking a `fast-glob` dependency (the project
 * doesn't have it yet) and implement a small recursive walker on top of
 * Node 18+ `fs.readdir({ recursive: true })`. Patterns use the standard
 * `*`/`**`/`?` syntax — translated to a regex once and applied per relative
 * path.
 *
 * Sandbox notes:
 *   - The base directory (`cwd` arg, defaults to ".") is resolved via
 *     resolveSandboxedPath, so `cwd: ".."` is rejected up front.
 *   - The PATTERN itself does NOT permit `..` segments — we reject patterns
 *     containing `..` to prevent the walker emitting paths that escape on
 *     symlink follow. (We never follow symlinks during the walk anyway.)
 *   - Result paths are returned workspace-relative.
 *   - Hard cap at MAX_GLOB_MATCHES (1000).
 */

import fs from 'fs';
import path from 'path';
import type { ToolSpec } from '../../tool-spec';
import { resolveSandboxedPath, MAX_GLOB_MATCHES } from './sandbox-utils';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';

export const globFilesTool: ToolSpec = {
  name: 'glob_files',
  description:
    'Find files in the workspace matching a glob pattern (e.g. "**/*.ts", "src/*.json"). ' +
    'Returns up to 1000 workspace-relative paths sorted lexicographically. ' +
    'Patterns containing ".." are rejected.',
  input_schema: {
    type: 'object',
    properties: {
      pattern: {
        type: 'string',
        description: 'Glob pattern. Supports *, **, ?, character classes [abc].',
      },
      cwd: {
        type: 'string',
        description: 'Optional workspace-relative directory to search in. Defaults to workspace root.',
      },
    },
    required: ['pattern'],
    additionalProperties: false,
  },
  source: 'base',
};

interface GlobFilesInput {
  pattern: string;
  cwd?: string;
}

function isGlobInput(x: unknown): x is GlobFilesInput {
  return (
    typeof x === 'object' &&
    x !== null &&
    typeof (x as { pattern: unknown }).pattern === 'string'
  );
}

/** Translate a glob pattern to a RegExp. Anchored on both ends. */
function globToRegex(glob: string): RegExp {
  let re = '^';
  let i = 0;
  while (i < glob.length) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') {
        // ** — match across separators
        re += '.*';
        i += 2;
        // consume trailing slash if present (so `**/x` works)
        if (glob[i] === '/' || glob[i] === '\\') i++;
      } else {
        // * — match within one path segment
        re += '[^/\\\\]*';
        i++;
      }
    } else if (c === '?') {
      re += '[^/\\\\]';
      i++;
    } else if (c === '[') {
      // character class — pass through to end-bracket
      const end = glob.indexOf(']', i + 1);
      if (end === -1) {
        re += '\\[';
        i++;
      } else {
        re += glob.slice(i, end + 1);
        i = end + 1;
      }
    } else if ('.+^$(){}|\\'.includes(c)) {
      re += '\\' + c;
      i++;
    } else if (c === '/' || c === '\\') {
      re += '[/\\\\]';
      i++;
    } else {
      re += c;
      i++;
    }
  }
  re += '$';
  return new RegExp(re);
}

export const globFilesExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isGlobInput(input)) {
    return {
      output: { error: 'glob_files: input must be { pattern: string, cwd?: string }' },
      isError: true,
    };
  }

  if (input.pattern.includes('..')) {
    return { output: { error: 'glob_files: pattern must not contain ".."' }, isError: true };
  }

  const cwdRel = input.cwd ?? '.';
  const resolved = resolveSandboxedPath(ctx.workspace, cwdRel, { mustExist: true });
  if (!resolved.ok) {
    return { output: { error: `glob_files denied: ${resolved.reason}` }, isError: true };
  }

  const re = globToRegex(input.pattern);
  const matches: string[] = [];

  try {
    const entries = await fs.promises.readdir(resolved.absPath, {
      recursive: true,
      withFileTypes: true,
    });
    for (const ent of entries) {
      if (ctx.signal.aborted) {
        return { output: { error: 'glob_files aborted' }, isError: true };
      }
      if (!ent.isFile()) continue;
      // Node 20+ Dirent has `parentPath`; Node 18 uses `path`. Try both.
      const parent = (ent as unknown as { parentPath?: string; path?: string }).parentPath
        ?? (ent as unknown as { path?: string }).path
        ?? resolved.absPath;
      const abs = path.join(parent, ent.name);
      const relFromWs = path.relative(ctx.workspace, abs).replace(/\\/g, '/');
      // skip deny-listed components
      if (relFromWs.split('/').some((c) => c === '.git' || c === 'node_modules')) continue;
      if (re.test(relFromWs)) {
        matches.push(relFromWs);
        if (matches.length >= MAX_GLOB_MATCHES) break;
      }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `glob_files failed: ${msg}` }, isError: true };
  }

  matches.sort();
  return {
    output: {
      pattern: input.pattern,
      cwd: cwdRel,
      count: matches.length,
      truncated: matches.length >= MAX_GLOB_MATCHES,
      matches,
    },
  };
};

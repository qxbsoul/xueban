/**
 * sandbox-utils.ts — shared path-resolution + size-cap constants for builtins.
 *
 * Every read/write tool MUST funnel its input.path through `resolveSandboxedPath`
 * before touching disk. The function:
 *
 *   1. Rejects absolute paths outright (Windows drive letters AND POSIX `/`).
 *   2. Joins against the workspace root and resolves to an absolute path.
 *   3. Resolves the *real* path (follows symlinks) — but only if the target
 *      exists; for write/create flows where the target may not yet exist we
 *      walk up to the closest existing ancestor and realpath that, then
 *      reconstruct.
 *   4. Verifies the resolved real path is a descendant of the workspace real
 *      path. The check uses `path.relative` semantics — both endpoints must
 *      lie under the same realpath root.
 *   5. Rejects any path component matching a deny-list (`.git`, `node_modules`).
 *      We deny based on the *resolved* path so attempts like `./safe/../.git`
 *      still trigger.
 *
 * MAX_READ_BYTES is 5 MiB — matches the read tier's spec. Tools that stream
 * (fetch_url) use the same constant for response-size capping.
 *
 * MAX_DIR_ENTRIES (200) caps list_dir. MAX_GLOB_MATCHES (1000) caps glob_files.
 * MAX_GREP_BYTES (10 MiB) caps total bytes scanned by grep before we bail.
 */

import fs from 'fs';
import path from 'path';

export const MAX_READ_BYTES = 5 * 1024 * 1024; // 5 MiB
export const MAX_DIR_ENTRIES = 200;
export const MAX_GLOB_MATCHES = 1000;
export const MAX_GREP_BYTES = 10 * 1024 * 1024; // 10 MiB total scan budget
export const MAX_FETCH_BYTES = 5 * 1024 * 1024;
export const SHELL_TIMEOUT_MS = 30_000;

/** Components we always refuse to touch, even when nominally inside workspace. */
const DENY_COMPONENTS = new Set(['.git', 'node_modules']);

export type SandboxResult =
  | { ok: true; absPath: string; realPath: string }
  | { ok: false; reason: string };

/**
 * Resolve `relPath` relative to `workspace`, enforcing the full sandbox check.
 *
 * `mustExist=false` (default) lets callers resolve write targets that don't
 * yet exist — we anchor on the closest existing ancestor for the realpath
 * check, which still defeats symlink escapes (since any symlink in the path
 * must already exist).
 */
export function resolveSandboxedPath(
  workspace: string,
  relPath: string,
  opts: { mustExist?: boolean } = {},
): SandboxResult {
  if (typeof relPath !== 'string' || relPath.length === 0) {
    return { ok: false, reason: 'path must be a non-empty string' };
  }
  // Reject absolute paths — both POSIX `/` and Windows drive letters.
  if (path.isAbsolute(relPath)) {
    return { ok: false, reason: `absolute paths are not allowed: ${relPath}` };
  }
  // Reject NUL bytes (classic poison-null-byte trick).
  if (relPath.includes('\0')) {
    return { ok: false, reason: 'path contains NUL byte' };
  }

  const wsAbs = path.resolve(workspace);
  let wsReal: string;
  try {
    wsReal = fs.realpathSync(wsAbs);
  } catch {
    // workspace must exist
    return { ok: false, reason: `workspace does not exist: ${workspace}` };
  }

  const joined = path.resolve(wsAbs, relPath);

  // Check raw resolved path is under workspace (defeats `..` traversal).
  if (!isDescendant(wsAbs, joined)) {
    return { ok: false, reason: `path escapes workspace: ${relPath}` };
  }

  // Realpath check — follow symlinks where possible.
  const realPath = realpathBestEffort(joined);

  if (opts.mustExist && !fs.existsSync(realPath)) {
    return { ok: false, reason: `path does not exist: ${relPath}` };
  }

  if (!isDescendant(wsReal, realPath)) {
    return { ok: false, reason: `path resolves outside workspace (symlink escape?): ${relPath}` };
  }

  // Deny-list any component in the resolved path that matches.
  const relFromWs = path.relative(wsReal, realPath);
  for (const comp of relFromWs.split(/[\\/]/)) {
    if (DENY_COMPONENTS.has(comp)) {
      return { ok: false, reason: `denied path component: ${comp}` };
    }
  }

  return { ok: true, absPath: joined, realPath };
}

/** Walk up parents until we find one that exists, then realpath that. */
function realpathBestEffort(p: string): string {
  let cur = p;
  const tail: string[] = [];
  // safety bound — paths can't be longer than 256 components in practice
  for (let i = 0; i < 256; i++) {
    try {
      const real = fs.realpathSync(cur);
      return tail.length ? path.join(real, ...tail.reverse()) : real;
    } catch {
      const parent = path.dirname(cur);
      if (parent === cur) {
        // hit root without finding anything
        return p;
      }
      tail.push(path.basename(cur));
      cur = parent;
    }
  }
  return p;
}

/**
 * True iff `child` is `parent` or a descendant. Handles the
 * `/foo` vs `/foobar` confusion by requiring a path separator boundary.
 */
function isDescendant(parent: string, child: string): boolean {
  const rel = path.relative(parent, child);
  if (rel === '') return true;
  if (rel.startsWith('..')) return false;
  if (path.isAbsolute(rel)) return false;
  return true;
}

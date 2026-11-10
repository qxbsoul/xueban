/**
 * github.ts — builtin `github_request` (read) + `github_write` (mutate) tools.
 *
 * Thin passthrough to the GitHub REST API (https://api.github.com). Works
 * unauthenticated (rate-limited); set `GITHUB_TOKEN` for normal quotas and
 * write access. Split into two tools so the permission story stays clean:
 *
 *   github_request  GET-only   → default mode ALLOW (read-only, safe)
 *   github_write    POST/PUT/PATCH/DELETE → default mode PROMPT (side effect)
 *
 * The response body is passed through as `data` — issues / PRs / repos all
 * share this one pair of tools, keeping the registry small.
 */

import type { ToolSpec } from '../../tool-spec';
import type { BuiltinToolExecutor, BuiltinToolContext } from './types';
import { providerEnv, providerFetchJson } from './provider-http';

const GITHUB_API_VERSION = '2022-11-28';

// Path hygiene: must be a rooted absolute path on api.github.com. Rejects
// protocol-relative ('//host') and control characters; the host itself is
// fixed by the https://api.github.com prefix so no SSRF surface exists.
const SAFE_PATH_RE = /^\/(?!\/)[A-Za-z0-9._~!$&'()*+,;=:@%/\-?]*$/;

function resolveUrl(rawPath: string): string | null {
  if (typeof rawPath !== 'string' || !SAFE_PATH_RE.test(rawPath)) return null;
  try {
    const url = new URL(`https://api.github.com${rawPath}`);
    if (url.hostname !== 'api.github.com') return null;
    return url.toString();
  } catch {
    return null;
  }
}

function githubHeaders(env: Readonly<Record<string, string>>, accept?: string): Record<string, string> {
  const headers: Record<string, string> = {
    accept: accept ?? 'application/vnd.github+json',
    'x-github-api-version': GITHUB_API_VERSION,
  };
  if (env.GITHUB_TOKEN) headers.authorization = `Bearer ${env.GITHUB_TOKEN}`;
  return headers;
}

// ─── github_request (read-only) ─────────────────────────────────────────────

export const githubRequestTool: ToolSpec = {
  name: 'github_request',
  description:
    'Read from the GitHub REST API (GET only): repos, issues, pull requests, ' +
    'code search, releases… Path is relative to https://api.github.com, e.g. ' +
    '"/repos/owner/repo/issues". Optional GITHUB_TOKEN raises rate limits.',
  input_schema: {
    type: 'object',
    properties: {
      path: {
        type: 'string',
        description: "API path starting with '/', e.g. /repos/owner/repo/issues",
      },
      accept: {
        type: 'string',
        description: 'Optional Accept header override (e.g. raw media types).',
      },
    },
    required: ['path'],
    additionalProperties: false,
  },
  source: 'base',
};

interface GithubRequestInput {
  path: string;
  accept?: string;
}

function isGithubRequestInput(x: unknown): x is GithubRequestInput {
  return typeof x === 'object' && x !== null && typeof (x as { path?: unknown }).path === 'string';
}

export const githubRequestExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isGithubRequestInput(input)) {
    return { output: { error: "github_request: input must be { path: string, accept? }" }, isError: true };
  }
  const url = resolveUrl(input.path);
  if (!url) {
    return {
      output: { error: `github_request: unsafe or invalid API path: ${input.path}` },
      isError: true,
    };
  }

  try {
    const res = await providerFetchJson(
      url,
      { method: 'GET', headers: githubHeaders(providerEnv(ctx), input.accept) },
      ctx,
    );
    return {
      output: {
        provider: 'github',
        path: input.path,
        status: res.status,
        data: res.data,
        ...(res.status === 403 ? { note: 'GitHub API rate limit or access restriction — set GITHUB_TOKEN if unset.' } : {}),
      },
      ...(res.status >= 400 ? { isError: true } : {}),
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `github_request failed: ${msg}` }, isError: true };
  }
};

// ─── github_write (mutating) ────────────────────────────────────────────────

export const githubWriteTool: ToolSpec = {
  name: 'github_write',
  description:
    'Write to the GitHub REST API (POST/PUT/PATCH/DELETE): create or update ' +
    'issues, comments, pull requests, files… Requires GITHUB_TOKEN. Path is ' +
    'relative to https://api.github.com. Side-effecting: prompts for approval ' +
    'unless explicitly allowed.',
  input_schema: {
    type: 'object',
    properties: {
      method: { type: 'string', enum: ['POST', 'PUT', 'PATCH', 'DELETE'] },
      path: {
        type: 'string',
        description: "API path starting with '/', e.g. /repos/owner/repo/issues",
      },
      body: { description: 'Optional JSON request body.' },
      accept: {
        type: 'string',
        description: 'Optional Accept header override (e.g. raw media types).',
      },
    },
    required: ['method', 'path'],
    additionalProperties: false,
  },
  source: 'base',
};

interface GithubWriteInput {
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  path: string;
  body?: unknown;
  accept?: string;
}

function isGithubWriteInput(x: unknown): x is GithubWriteInput {
  if (typeof x !== 'object' || x === null) return false;
  const o = x as Record<string, unknown>;
  return (
    (o.method === 'POST' || o.method === 'PUT' || o.method === 'PATCH' || o.method === 'DELETE') &&
    typeof o.path === 'string'
  );
}

export const githubWriteExecutor: BuiltinToolExecutor = async (
  input: unknown,
  ctx: BuiltinToolContext,
) => {
  if (!isGithubWriteInput(input)) {
    return {
      output: { error: "github_write: input must be { method: 'POST'|'PUT'|'PATCH'|'DELETE', path: string, body?, accept? }" },
      isError: true,
    };
  }
  const url = resolveUrl(input.path);
  if (!url) {
    return {
      output: { error: `github_write: unsafe or invalid API path: ${input.path}` },
      isError: true,
    };
  }

  const env = providerEnv(ctx);
  if (!env.GITHUB_TOKEN) {
    return {
      output: {
        error: 'github_write: GITHUB_TOKEN is required for write operations (set it in the environment).',
      },
      isError: true,
    };
  }

  const headers = githubHeaders(env, input.accept);
  if (input.body !== undefined) headers['content-type'] = 'application/json';

  try {
    const res = await providerFetchJson(
      url,
      {
        method: input.method,
        headers,
        body: input.body !== undefined ? JSON.stringify(input.body) : undefined,
      },
      ctx,
    );
    return {
      output: {
        provider: 'github',
        method: input.method,
        path: input.path,
        status: res.status,
        data: res.data,
        ...(res.status === 403 ? { note: 'GitHub API rate limit or access restriction — check GITHUB_TOKEN scopes.' } : {}),
      },
      ...(res.status >= 400 ? { isError: true } : {}),
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { output: { error: `github_write failed: ${msg}` }, isError: true };
  }
};

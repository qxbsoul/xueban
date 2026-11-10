/**
 * env.ts — must be the FIRST import in src/index.ts.
 *
 * Loads the repo-root `.env` (project root is one level above `server/`) into
 * process.env before any other module reads environment variables. Node's
 * built-in `process.loadEnvFile()` (v20.12+) parses standard KEY=VALUE lines;
 * existing process.env values are never overwritten, so real shell env always
 * wins over the file.
 *
 * Why a dedicated module: ES module imports are hoisted, so inline
 * `process.loadEnvFile()` at the top of index.ts would still run AFTER
 * imports like routes/llm.ts have been evaluated. A side-effect-only import
 * of this module as the first import guarantees env is ready before the
 * router modules (and their module-level env reads) load.
 *
 * Silent no-op when the file is missing (local dev without .env) — callers
 * fall back to BYOK-only behavior, same as before this existed.
 */

import path from 'path';
import fs from 'fs';

// server/ is `<root>/server`; repo root is exactly one level up regardless of
// where the process was started from (server/ vs repo root).
const ROOT_ENV_PATH = path.resolve(__dirname, '..', '..', '.env');

try {
  if (fs.existsSync(ROOT_ENV_PATH)) {
    process.loadEnvFile(ROOT_ENV_PATH);
    // eslint-disable-next-line no-console
    console.log(`[env] loaded ${ROOT_ENV_PATH}`);
  }
} catch (err) {
  // Malformed .env must not take the whole server down — warn and continue
  // with whatever is already in process.env.
  // eslint-disable-next-line no-console
  console.warn(
    `[env] failed to load ${ROOT_ENV_PATH}: ${(err as Error).message}`,
  );
}

// Referenced so tsc doesn't prune the import in some bundler configurations.
void fs;
void path;

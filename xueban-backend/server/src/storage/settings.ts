/**
 * settings.ts — KV settings persistence (Story 15.17)
 *
 * Storage: $CWD/.xueban/settings.json
 *
 * MVP design note (15.17 vs 15.16):
 *   Story 15.16 (SQLite project / conversation model) is still P2 and not
 *   landed. This module ships a JSON-file backend so 15.17 can land
 *   independently. The exported API surface (`getSetting / setSetting /
 *   deleteSetting / listSettings`) is **stable** — when 15.16 introduces
 *   better-sqlite3, only the bodies below need to swap from `load()/persist()`
 *   to a `settings` table; route layer + tests stay untouched.
 *
 * Validation rules (mirror spec L86-91):
 *   - key must be non-empty, length ≤ 128
 *   - JSON-encoded value ≤ 64 KB
 *   - `sf_anthropic_key` (and any prefix variants) are hard-rejected — BYOK
 *     keys must stay client-only (defensive bottom-half of the BYOK boundary).
 *
 * Errors are thrown as `Error` with codes the route layer translates into
 * 400 / 413 responses (`KEY_FORBIDDEN`, `INVALID_KEY`, `VALUE_TOO_LARGE`).
 *
 * Style mirrors `storage/agents.ts` (load/persist + atomic disk write).
 */

import fs from 'fs';
import path from 'path';

// JSON-encoded payload representation. The on-disk value is always a JSON
// string so heterogeneous types (numbers, booleans, objects) round-trip
// losslessly through the same TEXT column when 15.16 lands.
interface SettingsFile {
  [key: string]: string;
}

const KEY_MAX_LEN = 128;
const VALUE_MAX_BYTES = 64 * 1024;

/**
 * Hard-blocked keys. Anything that LOOKS like a BYOK key is refused — better
 * to false-positive than to leak a `sk-ant-` token into a (future) shared
 * settings store. 2026-05-11 review P1-8: 也拒绝原型污染键（OpenDesign 模式）。
 */
const PROTO_KEYS = new Set(['__proto__', 'constructor', 'prototype']);
const VALID_KEY_RE = /^[a-zA-Z0-9._-]+$/;

function isForbiddenKey(key: string): boolean {
  if (!key) return false;
  if (PROTO_KEYS.has(key)) return true;
  return key === 'sf_anthropic_key' || key.startsWith('sf_anthropic_key');
}

function storagePaths(): { dir: string; file: string } {
  const dir = path.join(process.cwd(), '.xueban');
  return { dir, file: path.join(dir, 'settings.json') };
}

function load(): SettingsFile {
  const { file } = storagePaths();
  if (!fs.existsSync(file)) return {};
  let raw: string;
  try {
    raw = fs.readFileSync(file, 'utf-8');
  } catch {
    return {};
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      // Drop any non-string entries — disk corruption / pre-15.17 file.
      const out: SettingsFile = {};
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof v === 'string') out[k] = v;
      }
      return out;
    }
    return {};
  } catch (err) {
    // 2026-05-11 review P1-9: corrupt JSON 不再静默吞掉 → 旧用户配置全丢。
    // 备份到 .corrupt.<ts> 后再让 persist 重建空文件，留给用户人工 recover。
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    const backup = `${file}.corrupt.${ts}`;
    try {
      fs.renameSync(file, backup);
      console.warn(
        `[settings] corrupt settings.json — backed up to ${path.basename(backup)}: ${(err as Error).message}`,
      );
    } catch (renameErr) {
      console.warn(`[settings] corrupt settings.json + backup failed: ${(renameErr as Error).message}`);
    }
    return {};
  }
}

function persist(data: SettingsFile): void {
  const { dir, file } = storagePaths();
  fs.mkdirSync(dir, { recursive: true });
  // 2026-05-11 review P1-7: atomic write — 写 .tmp 再 rename，防多 tab 并发
  // PUT 互相覆盖（与 Story 15.8 saveRun 同模式）。OpenDesign apps/daemon/src/projects.ts
  // 用 await fsp.rename；这里用同步 renameSync 保持 storage API 同步签名。
  // rename 在 POSIX 是原子，Windows NTFS 也是原子（替换目标已存在文件）。
  const tmp = `${file}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), 'utf-8');
  fs.renameSync(tmp, file);
}

/**
 * Return all settings as a `{key: parsed-value}` map. Any entry whose
 * on-disk JSON fails to parse is silently skipped (defensive).
 */
export function listSettings(): Record<string, unknown> {
  const data = load();
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(data)) {
    try {
      out[k] = JSON.parse(v);
    } catch {
      /* skip malformed */
    }
  }
  return out;
}

/**
 * Read one key. Returns `undefined` when absent or when the stored JSON
 * cannot be parsed — callers should treat both as "not configured".
 */
export function getSetting(key: string): unknown {
  const data = load();
  const raw = data[key];
  if (raw === undefined) return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

/**
 * Upsert a setting. Throws with one of the documented codes on validation
 * failure; the route layer maps these to HTTP statuses.
 */
export function setSetting(key: string, value: unknown): void {
  if (isForbiddenKey(key)) throw new Error('KEY_FORBIDDEN');
  // 2026-05-11 review P1-8: 强字符集校验 — 拒绝 path-shaped / 控制字符 / unicode
  // 等可能搞坏下游消费者的字符（含未来 SQLite column name reuse 场景）。
  if (!key || key.length > KEY_MAX_LEN || !VALID_KEY_RE.test(key)) {
    throw new Error('INVALID_KEY');
  }
  const json = JSON.stringify(value);
  if (typeof json !== 'string') throw new Error('INVALID_VALUE');
  // `JSON.stringify` returns `undefined` for `undefined`, which we already
  // guard against by checking the resulting type above.
  if (Buffer.byteLength(json, 'utf-8') > VALUE_MAX_BYTES) {
    throw new Error('VALUE_TOO_LARGE');
  }
  const data = load();
  data[key] = json;
  persist(data);
}

/**
 * Delete a key. Idempotent — silently no-ops when the key is absent (matches
 * AC2 "DELETE … 不存在也返回 204").
 */
export function deleteSetting(key: string): boolean {
  const data = load();
  if (!(key in data)) return false;
  delete data[key];
  persist(data);
  return true;
}

// Test-only helper to wipe persisted state between unit tests.
export function _resetForTests(): void {
  const { file } = storagePaths();
  if (fs.existsSync(file)) fs.unlinkSync(file);
}

// Test-only helper exposing the forbidden-key predicate for assertions.
export function _isForbiddenKeyForTests(key: string): boolean {
  return isForbiddenKey(key);
}

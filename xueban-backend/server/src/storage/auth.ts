/**
 * storage/auth.ts — SIWE + guest session persistence layer.
 *
 * Tables: auth_nonces, auth_profiles, auth_sessions
 * (created in migrations/001-init.sql)
 *
 * All timestamps are ISO-8601 TEXT (UTC) to stay consistent with the rest of
 * the DB schema.
 */

import { getDb } from './sqlite';

// ── Types ─────────────────────────────────────────────────────────────────────

export interface NonceRow {
  id: number;
  address: string;
  nonce: string;
  used: number;
  expires_at: string;
  created_at: string;
}

export interface ProfileRow {
  address: string;
  did: string;
  display_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionRow {
  id: number;
  token_hash: string;
  address: string;
  auth_type: 'wallet' | 'guest';
  expires_at: string;
  created_at: string;
}

// ── Nonce helpers ─────────────────────────────────────────────────────────────

/**
 * createNonce — insert a new nonce for the given address.
 * expiresAt: ISO-8601 string (e.g. new Date(Date.now() + 5*60*1000).toISOString())
 */
export function createNonce(address: string, nonce: string, expiresAt: string): void {
  getDb()
    .prepare(
      `INSERT INTO auth_nonces (address, nonce, expires_at)
       VALUES (?, ?, ?)`,
    )
    .run(address.toLowerCase(), nonce, expiresAt);
}

/**
 * getNonce — return an unused, non-expired nonce row, or null.
 */
export function getNonce(nonce: string): NonceRow | null {
  const row = getDb()
    .prepare(
      `SELECT * FROM auth_nonces
       WHERE nonce = ?
         AND used = 0
         AND expires_at > datetime('now')`,
    )
    .get(nonce) as NonceRow | undefined;
  return row ?? null;
}

/**
 * useNonce — mark a nonce as consumed. Call after successful signature verify.
 * Returns true if exactly one row was updated.
 */
export function useNonce(nonce: string): boolean {
  const result = getDb()
    .prepare(`UPDATE auth_nonces SET used = 1 WHERE nonce = ? AND used = 0`)
    .run(nonce);
  return result.changes > 0;
}

// ── Session helpers ───────────────────────────────────────────────────────────

/**
 * createSession — persist a new session. token_hash is sha256(rawToken) hex.
 */
export function createSession(
  tokenHash: string,
  address: string,
  authType: 'wallet' | 'guest',
  expiresAt: string,
): void {
  getDb()
    .prepare(
      `INSERT INTO auth_sessions (token_hash, address, auth_type, expires_at)
       VALUES (?, ?, ?, ?)`,
    )
    .run(tokenHash, address.toLowerCase(), authType, expiresAt);
}

/**
 * getSessionByTokenHash — return a non-expired session, or null.
 */
export function getSessionByTokenHash(tokenHash: string): SessionRow | null {
  const row = getDb()
    .prepare(
      `SELECT * FROM auth_sessions
       WHERE token_hash = ?
         AND expires_at > datetime('now')`,
    )
    .get(tokenHash) as SessionRow | undefined;
  return row ?? null;
}

// ── Profile helpers ───────────────────────────────────────────────────────────

/**
 * upsertProfile — insert or update an auth_profile row.
 * displayName is optional; if null the existing value (or null) is preserved.
 */
export function upsertProfile(
  address: string,
  did: string,
  displayName?: string | null,
): void {
  const now = new Date().toISOString();
  const addr = address.toLowerCase();
  getDb()
    .prepare(
      `INSERT INTO auth_profiles (address, did, display_name, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(address) DO UPDATE SET
         did          = excluded.did,
         display_name = COALESCE(excluded.display_name, auth_profiles.display_name),
         updated_at   = excluded.updated_at`,
    )
    .run(addr, did, displayName ?? null, now, now);
}

/**
 * getProfile — return profile row or null.
 */
export function getProfile(address: string): ProfileRow | null {
  const row = getDb()
    .prepare(`SELECT * FROM auth_profiles WHERE address = ?`)
    .get(address.toLowerCase()) as ProfileRow | undefined;
  return row ?? null;
}

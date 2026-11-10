/**
 * routes/auth.ts — SIWE (Sign-In with Ethereum) + guest auth for Xueban.
 *
 * 0G Chain parameters:
 *   RPC  : https://evmrpc-testnet.0g.ai
 *   Chain ID : 16600
 *
 * ethers version: v6  (ethers.verifyMessage, ethers.JsonRpcProvider)
 *
 * Endpoints:
 *   POST /api/auth/guest          — issue a guest session token
 *   GET  /api/auth/nonce          — generate a fresh SIWE nonce
 *   POST /api/auth/verify         — verify SIWE signature → session token
 *   GET  /api/auth/me             — validate Bearer token, return profile
 */

import { Router, Request, Response } from 'express';
import crypto from 'crypto';
import { ethers } from 'ethers';
import {
  createNonce,
  getNonce,
  useNonce,
  createSession,
  getSessionByTokenHash,
  upsertProfile,
  getProfile,
} from '../storage/auth';

const router = Router();

// ── Helpers ───────────────────────────────────────────────────────────────────

/** sha256(input) → hex string */
function sha256(input: string): string {
  return crypto.createHash('sha256').update(input).digest('hex');
}

/** Generate a random hex string of `bytes` bytes. */
function randomHex(bytes: number): string {
  return crypto.randomBytes(bytes).toString('hex');
}

/** Extract Bearer token from Authorization header, or null. */
function extractBearer(req: Request): string | null {
  const auth = req.headers.authorization ?? '';
  if (!auth.startsWith('Bearer ')) return null;
  return auth.slice(7).trim() || null;
}

/** Session TTL for wallet logins: 7 days */
const WALLET_SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000;
/** Session TTL for guest logins: 24 hours */
const GUEST_SESSION_TTL_MS = 24 * 60 * 60 * 1000;
/** Nonce TTL: 5 minutes */
const NONCE_TTL_MS = 5 * 60 * 1000;

// ── POST /api/auth/guest ──────────────────────────────────────────────────────

router.post('/guest', (_req: Request, res: Response) => {
  try {
    const rawToken = randomHex(32);
    const tokenHash = sha256(rawToken);
    const expiresAt = new Date(Date.now() + GUEST_SESSION_TTL_MS).toISOString();

    createSession(tokenHash, 'guest', 'guest', expiresAt);

    res.json({
      token: rawToken,
      user: {
        address: 'guest',
        display_name: 'Guest',
        auth_type: 'guest',
      },
    });
  } catch (err) {
    console.error('[auth] guest error:', err);
    res.status(500).json({ error: 'Failed to create guest session' });
  }
});

// ── GET /api/auth/nonce?address=0x... ────────────────────────────────────────

router.get('/nonce', (req: Request, res: Response) => {
  const { address } = req.query as { address?: string };
  if (!address || !/^0x[0-9a-fA-F]{40}$/.test(address)) {
    return res.status(400).json({ error: 'Valid address query param required (0x + 40 hex chars)' });
  }

  try {
    const nonce = randomHex(16);
    const expiresAt = new Date(Date.now() + NONCE_TTL_MS).toISOString();
    createNonce(address, nonce, expiresAt);
    res.json({ nonce });
  } catch (err) {
    console.error('[auth] nonce error:', err);
    res.status(500).json({ error: 'Failed to generate nonce' });
  }
});

// ── POST /api/auth/verify ─────────────────────────────────────────────────────

interface VerifyBody {
  address: string;
  message: string;
  signature: string;
}

router.post('/verify', async (req: Request, res: Response) => {
  const { address, message, signature } = req.body as VerifyBody;

  if (!address || !message || !signature) {
    return res.status(400).json({ error: 'address, message, signature required' });
  }

  // 1. Recover signer address using ethers v6
  let recovered: string;
  try {
    recovered = ethers.verifyMessage(message, signature);
  } catch (err) {
    return res.status(400).json({ error: 'Invalid signature format' });
  }

  // 2. Compare recovered address with claimed address (case-insensitive)
  if (recovered.toLowerCase() !== address.toLowerCase()) {
    return res.status(401).json({ error: 'Signature address mismatch' });
  }

  // 3. Extract nonce from the SIWE message and validate it
  //    SIWE messages embed: "Nonce: <hex>"
  const nonceMatch = message.match(/^Nonce:\s*(\S+)$/m);
  if (!nonceMatch) {
    return res.status(400).json({ error: 'Nonce not found in message' });
  }
  const nonce = nonceMatch[1];

  const nonceRow = getNonce(nonce);
  if (!nonceRow) {
    return res.status(401).json({ error: 'Nonce is invalid, expired, or already used' });
  }

  // 3b. Verify the nonce was issued for the same address being authenticated
  //     (prevents one address from reusing a nonce generated for a different address)
  if (nonceRow.address !== address.toLowerCase()) {
    return res.status(401).json({ error: 'Nonce address mismatch' });
  }

  // 4. Mark nonce as used
  useNonce(nonce);

  // 5. Upsert profile
  const did = `did:0g:${address.toLowerCase()}`;
  upsertProfile(address, did);

  // 6. Create session
  const rawToken = randomHex(32);
  const tokenHash = sha256(rawToken);
  const expiresAt = new Date(Date.now() + WALLET_SESSION_TTL_MS).toISOString();
  createSession(tokenHash, address, 'wallet', expiresAt);

  // 7. Return token + profile
  const profile = getProfile(address);
  res.json({
    token: rawToken,
    user: {
      address: address.toLowerCase(),
      did,
      display_name: profile?.display_name ?? null,
      auth_type: 'wallet',
    },
  });
});

// ── GET /api/auth/me ──────────────────────────────────────────────────────────

router.get('/me', (req: Request, res: Response) => {
  const rawToken = extractBearer(req);
  if (!rawToken) {
    return res.status(401).json({ error: 'Authorization: Bearer <token> required' });
  }

  const tokenHash = sha256(rawToken);
  const session = getSessionByTokenHash(tokenHash);
  if (!session) {
    return res.status(401).json({ error: 'Invalid or expired session token' });
  }

  // Guest sessions have no profile row
  if (session.auth_type === 'guest') {
    return res.json({
      address: 'guest',
      display_name: 'Guest',
      auth_type: 'guest',
    });
  }

  const profile = getProfile(session.address);
  if (!profile) {
    // Session exists but profile was deleted — treat as expired
    return res.status(401).json({ error: 'Profile not found' });
  }

  res.json({
    address: profile.address,
    did: profile.did,
    display_name: profile.display_name,
    auth_type: 'wallet',
  });
});

export default router;

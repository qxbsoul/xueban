/**
 * settings-preferences.test.ts — /api/settings/preferences route contract
 *
 * Covers the ZCode-style appearance sync endpoint (theme / language / zoom):
 *   - GET returns only explicitly saved fields (absent ≠ defaulted)
 *   - PUT partial-merges validated fields over the stored blob
 *   - invalid enum / range / shape → 400 with field-level error
 *   - legacy /appearance alias reads+writes the same blob (theme-only view)
 *   - preferences survive the KV round-trip (same settings.json file)
 *
 * Storage lands in a temp cwd (storage/settings.ts resolves process.cwd() at
 * call time) so the dev checkout's real .xueban/settings.json is never
 * touched.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import settingsRouter from '../routes/settings';

const app = express();
app.use(express.json({ limit: '1mb' }));
app.use('/api/settings', settingsRouter);

let tmpDir = '';
let prevCwd = '';

beforeAll(() => {
  prevCwd = process.cwd();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-prefs-test-'));
  process.chdir(tmpDir);
});

afterAll(() => {
  process.chdir(prevCwd);
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe('GET /api/settings/preferences', () => {
  it('returns empty preferences when nothing was saved', async () => {
    const res = await request(app).get('/api/settings/preferences');
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ preferences: {} });
  });

  it('returns only explicitly saved fields after a PUT', async () => {
    await request(app).put('/api/settings/preferences').send({ language: 'zh' });
    const res = await request(app).get('/api/settings/preferences');
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ preferences: { language: 'zh' } });
  });
});

describe('PUT /api/settings/preferences', () => {
  it('accepts all three fields at once', async () => {
    const res = await request(app).put('/api/settings/preferences').send({
      theme: 'paper',
      language: 'en',
      zoom: 1.2,
    });
    expect(res.status).toBe(200);
    expect(res.body).toEqual({
      preferences: { theme: 'paper', language: 'en', zoom: 1.2 },
    });
  });

  it('partial-merges: unset fields keep their stored value', async () => {
    await request(app).put('/api/settings/preferences').send({
      theme: 'dark',
      language: 'zh',
      zoom: 0.9,
    });
    const res = await request(app).put('/api/settings/preferences').send({ theme: 'light' });
    expect(res.status).toBe(200);
    expect(res.body.preferences).toEqual({ theme: 'light', language: 'zh', zoom: 0.9 });
  });

  it('rejects an invalid theme', async () => {
    const res = await request(app).put('/api/settings/preferences').send({ theme: 'neon' });
    expect(res.status).toBe(400);
    expect(res.body.error.field).toBe('theme');
  });

  it('rejects an invalid language', async () => {
    const res = await request(app).put('/api/settings/preferences').send({ language: 'fr' });
    expect(res.status).toBe(400);
    expect(res.body.error.field).toBe('language');
  });

  it('rejects zoom out of range', async () => {
    const tooBig = await request(app).put('/api/settings/preferences').send({ zoom: 2 });
    expect(tooBig.status).toBe(400);
    expect(tooBig.body.error.field).toBe('zoom');

    const tooSmall = await request(app).put('/api/settings/preferences').send({ zoom: 0.5 });
    expect(tooSmall.status).toBe(400);
  });

  it('rejects an empty / non-object body', async () => {
    const empty = await request(app).put('/api/settings/preferences').send({});
    expect(empty.status).toBe(400);
    expect(empty.body.error.field).toBe('body');
  });

  it('rounds zoom to 2 decimals to avoid float drift', async () => {
    const res = await request(app).put('/api/settings/preferences').send({ zoom: 1.199999 });
    expect(res.status).toBe(200);
    expect(res.body.preferences.zoom).toBe(1.2);
  });
});

describe('legacy /appearance alias', () => {
  it('GET echoes the stored theme (dark fallback only when unset)', async () => {
    const prefs = await request(app).get('/api/settings/preferences');
    const res = await request(app).get('/api/settings/appearance');
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ theme: prefs.body.preferences.theme ?? 'dark' });
  });

  it('PUT { theme } writes the shared preferences blob', async () => {
    await request(app).put('/api/settings/appearance').send({ theme: 'light' });
    const prefs = await request(app).get('/api/settings/preferences');
    expect(prefs.body.preferences.theme).toBe('light');
    const alias = await request(app).get('/api/settings/appearance');
    expect(alias.body).toEqual({ theme: 'light' });
  });

  it('PUT rejects an invalid theme', async () => {
    const res = await request(app).put('/api/settings/appearance').send({ theme: 'solarized' });
    expect(res.status).toBe(400);
  });

  it('does not clobber language/zoom stored via /preferences', async () => {
    await request(app).put('/api/settings/preferences').send({ language: 'en', zoom: 1.1 });
    await request(app).put('/api/settings/appearance').send({ theme: 'paper' });
    const prefs = await request(app).get('/api/settings/preferences');
    expect(prefs.body.preferences).toEqual({ theme: 'paper', language: 'en', zoom: 1.1 });
  });
});

describe('preferences vs generic KV route', () => {
  it('blob is visible through the KV listing (GET /api/settings)', async () => {
    await request(app).put('/api/settings/preferences').send({ theme: 'dark', language: 'zh', zoom: 1 });
    const res = await request(app).get('/api/settings');
    expect(res.status).toBe(200);
    expect(res.body.settings.preferences).toEqual({ theme: 'dark', language: 'zh', zoom: 1 });
  });

  it('literal /preferences route shadows the generic /:key matcher (GET shape differs)', async () => {
    const res = await request(app).get('/api/settings/preferences');
    // Literal route responds { preferences } — not the KV { key, value } shape.
    expect(res.status).toBe(200);
    expect(res.body).toHaveProperty('preferences');
    expect(res.body).not.toHaveProperty('key');
  });
});

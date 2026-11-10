import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import xuebanKnowledgeRouter from '../routes/xueban-knowledge';
import { _resetForTests } from '../storage/sqlite';
import { companyOwnsKnowledgePack, linkKnowledgePack } from '../storage/xueban-knowledge';

const app = express();
app.use(express.json());
app.use('/api/xueban-knowledge', xuebanKnowledgeRouter);

const packId = 'a'.repeat(32);
const originalCwd = process.cwd();
let dir = '';
let deletePackStatus = 200;
let remoteCalls: Array<{ url: string; init?: RequestInit }> = [];

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'sf-xueban-knowledge-'));
  process.chdir(dir);
  _resetForTests();
  linkKnowledgePack('co1', packId);
  deletePackStatus = 200;
  remoteCalls = [];
  vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/api/xueban/auth/me')) {
      const authorization = new Headers(init?.headers).get('authorization');
      const id = authorization === 'Bearer co2' ? 'co2' : 'co1';
      return Response.json({ company: { id, name: id } });
    }
    remoteCalls.push({ url, init });
    if (init?.method === 'PATCH') {
      const updates = JSON.parse(String(init.body)) as { name?: string; description?: string };
      return Response.json({ data: { pack_id: packId, ...updates } });
    }
    if (init?.method === 'DELETE' && url.endsWith(`/packs/${packId}`)) {
      return Response.json(
        deletePackStatus === 200
          ? { data: { deleted: true, pack_id: packId } }
          : { error: { code: 'BACKEND_ERROR', message: 'delete failed' } },
        { status: deletePackStatus },
      );
    }
    if (init?.method === 'DELETE' && url.includes('/sources/')) {
      return Response.json({ data: { deleted: true, source_id: 'source-1' } });
    }
    return Response.json({ error: { code: 'NOT_FOUND', message: 'not found' } }, { status: 404 });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  _resetForTests();
  process.chdir(originalCwd);
  rmSync(dir, { recursive: true, force: true });
});

describe('公司知识包管理', () => {
  it('PATCH 仅代理名称和描述，并保留远端响应结构', async () => {
    const res = await request(app)
      .patch(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co1')
      .send({ name: '  新名称  ', description: '新描述', metadata: { company_id: 'co2' } });

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ data: { pack_id: packId, name: '新名称', description: '新描述' } });
    expect(remoteCalls).toHaveLength(1);
    expect(remoteCalls[0].init?.method).toBe('PATCH');
    expect(JSON.parse(String(remoteCalls[0].init?.body))).toEqual({ name: '新名称', description: '新描述' });
  });

  it('PATCH 拒绝空名称和空更新，且不调用远端', async () => {
    const emptyName = await request(app)
      .patch(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co1')
      .send({ name: '   ' });
    expect(emptyName.status).toBe(422);
    expect(emptyName.body.error.code).toBe('KNOWLEDGE_NAME_REQUIRED');

    const emptyPatch = await request(app)
      .patch(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co1')
      .send({});
    expect(emptyPatch.status).toBe(422);
    expect(emptyPatch.body.error.code).toBe('KNOWLEDGE_UPDATE_REQUIRED');
    expect(remoteCalls).toHaveLength(0);
  });

  it('PATCH、DELETE pack 和 DELETE source 均禁止跨公司访问', async () => {
    const patch = await request(app)
      .patch(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co2')
      .send({ name: '越权修改' });
    const deletePack = await request(app)
      .delete(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co2');
    const deleteSource = await request(app)
      .delete(`/api/xueban-knowledge/packs/${packId}/sources/source-1`)
      .set('Authorization', 'Bearer co2');

    for (const res of [patch, deletePack, deleteSource]) {
      expect(res.status).toBe(404);
      expect(res.body.error.code).toBe('KNOWLEDGE_PACK_NOT_FOUND');
    }
    expect(remoteCalls).toHaveLength(0);
    expect(companyOwnsKnowledgePack('co1', packId)).toBe(true);
  });

  it('DELETE pack 远端成功后才删除当前公司的映射', async () => {
    const res = await request(app)
      .delete(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co1');

    expect(res.status).toBe(200);
    expect(res.body.data).toEqual({ deleted: true, pack_id: packId });
    expect(remoteCalls[0].init?.method).toBe('DELETE');
    expect(companyOwnsKnowledgePack('co1', packId)).toBe(false);
  });

  it('DELETE pack 远端失败时保留映射', async () => {
    deletePackStatus = 503;
    const res = await request(app)
      .delete(`/api/xueban-knowledge/packs/${packId}`)
      .set('Authorization', 'Bearer co1');

    expect(res.status).toBe(503);
    expect(res.body.error.code).toBe('BACKEND_ERROR');
    expect(companyOwnsKnowledgePack('co1', packId)).toBe(true);
  });

  it('DELETE source 代理既有 Python 能力且不删除知识包映射', async () => {
    const res = await request(app)
      .delete(`/api/xueban-knowledge/packs/${packId}/sources/source-1`)
      .set('Authorization', 'Bearer co1');

    expect(res.status).toBe(200);
    expect(res.body.data).toEqual({ deleted: true, source_id: 'source-1' });
    expect(remoteCalls[0].url.endsWith(`/packs/${packId}/sources/source-1`)).toBe(true);
    expect(remoteCalls[0].init?.method).toBe('DELETE');
    expect(companyOwnsKnowledgePack('co1', packId)).toBe(true);
  });
});

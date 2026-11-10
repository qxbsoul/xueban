/**
 * knowledge-vaults.test.ts — 本地仓库(vault)知识库契约测试,2026-08-19。
 *
 * 覆盖三层:
 *   - lib/vault-fs walkVault:忽略规则(dot 目录/.git/.obsidian/node_modules/
 *     垃圾文件)、扩展白名单与类型映射、'/' 分隔相对路径、排序、双上限截断。
 *   - lib/vault-fs readVaultTextFile:正常读、绝对路径拒、.. 拒、非白名单
 *     扩展拒、不存在 404、超限截断。
 *   - routes/knowledge-vaults(supertest):注册幂等(path UNIQUE)、列表存活
 *     探测、移除只删注册行(REG-1:磁盘文件原样)、files/file 端到端。
 *
 * 存储落在临时 cwd 的 .xueban/app.sqlite(对齐 settings-preferences
 * 测试模式),不碰开发 checkout 的真实库。
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import express from 'express';
import request from 'supertest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { walkVault, readVaultTextFile, VaultFsError, type WalkVaultResult } from '../lib/vault-fs';
import { _resetForTests } from '../storage/sqlite';
import knowledgeVaultsRouter from '../routes/knowledge-vaults';

// ── fixture:一个仿真 Obsidian vault ─────────────────────────────────────────

let tmpRoot = '';
let tmpVault = '';
let tmpCwd = '';
let prevCwd = '';

function w(p: string, content: string): void {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, content, 'utf8');
}

const app = express();
app.use(express.json({ limit: '1mb' }));
app.use('/api/knowledge-vaults', knowledgeVaultsRouter);

beforeAll(() => {
  prevCwd = process.cwd();

  // 容器目录:vault 与「仓库外靶子文件」都在里面,清理只删这一个容器。
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-vault-root-'));
  tmpVault = path.join(tmpRoot, 'vault');
  fs.mkdirSync(tmpVault, { recursive: true });
  w(path.join(tmpVault, 'notes', 'a.md'), '# A\nhello vault');
  w(path.join(tmpVault, 'notes', 'sub', 'b.markdown'), 'note b');
  w(path.join(tmpVault, 'readme.txt'), 'plain text');
  w(path.join(tmpVault, 'data', 'conf.yaml'), 'k: v');
  w(path.join(tmpVault, 'data', 'dump.csv'), 'x,y\n1,2');
  w(path.join(tmpVault, 'assets', 'pic.png'), '\u0089PNG-fake');
  w(path.join(tmpVault, 'docs', 'paper.pdf'), '%PDF-fake');
  // 应被忽略的:dot 目录 / node_modules / dot 文件 / 系统垃圾 / 非白名单扩展
  w(path.join(tmpVault, '.git', 'config.md'), 'git internals');
  w(path.join(tmpVault, '.obsidian', 'app.json'), '{}');
  w(path.join(tmpVault, '.shadow', 'index.db.md'), 'stale index');
  w(path.join(tmpVault, 'node_modules', 'pkg', 'readme.md'), 'dep readme');
  w(path.join(tmpVault, 'Thumbs.db'), 'junk');
  w(path.join(tmpVault, 'desktop.ini'), 'junk');
  w(path.join(tmpVault, '.DS_Store'), 'junk');
  w(path.join(tmpVault, 'bin', 'tool.exe'), 'binary');
  // 逃逸靶子:仓库外的文件,readVaultTextFile 不应能碰到
  w(path.join(tmpRoot, 'outside-secret.md'), 'should not be readable');

  // 路由/存储测试用独立 cwd(真实 .xueban/app.sqlite 不被污染)
  tmpCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-vault-cwd-'));
  process.chdir(tmpCwd);
  _resetForTests();
});

afterAll(() => {
  process.chdir(prevCwd);
  _resetForTests();
  fs.rmSync(tmpRoot, { recursive: true, force: true });
  fs.rmSync(tmpCwd, { recursive: true, force: true });
});

// ── walkVault ────────────────────────────────────────────────────────────────

describe('walkVault', () => {
  // 在 beforeAll 之后才算(外层 fixture 先建好目录树)
  let walked: WalkVaultResult;
  beforeAll(() => {
    walked = walkVault(tmpVault);
  });

  it('只收白名单扩展,相对路径用 / 分隔且排序', () => {
    expect(walked.files.map((f) => f.path)).toEqual([
      'assets/pic.png',
      'data/conf.yaml',
      'data/dump.csv',
      'docs/paper.pdf',
      'notes/a.md',
      'notes/sub/b.markdown',
      'readme.txt',
    ]);
  });

  it('忽略 dot 目录(.git/.obsidian/.shadow)、node_modules 与垃圾文件', () => {
    const paths = walked.files.map((f) => f.path).join('\n');
    expect(paths).not.toContain('.git');
    expect(paths).not.toContain('.obsidian');
    expect(paths).not.toContain('.shadow');
    expect(paths).not.toContain('node_modules');
    expect(paths).not.toContain('Thumbs.db');
    expect(paths).not.toContain('.DS_Store');
    expect(paths).not.toContain('desktop.ini');
    expect(paths).not.toContain('tool.exe');
  });

  it('类型映射 doc/pdf/image + 带 size/mtime 元数据', () => {
    const md = walked.files.find((f) => f.path === 'notes/a.md')!;
    expect(md.type).toBe('doc');
    expect(md.name).toBe('a.md');
    expect(md.size).toBeGreaterThan(0);
    expect(md.mtime).toBeGreaterThan(0);
    expect(walked.files.find((f) => f.path === 'docs/paper.pdf')!.type).toBe('pdf');
    expect(walked.files.find((f) => f.path === 'assets/pic.png')!.type).toBe('image');
  });

  it('maxFiles 上限:超限截断并标 truncated', () => {
    const capped = walkVault(tmpVault, { maxFiles: 2 });
    expect(capped.files.length).toBe(2);
    expect(capped.truncated).toBe(true);
  });
});

// ── readVaultTextFile ────────────────────────────────────────────────────────

describe('readVaultTextFile', () => {
  it('读仓库内文本文件(utf8)', () => {
    const out = readVaultTextFile(tmpVault, 'notes/a.md');
    expect(out.content).toContain('hello vault');
    expect(out.truncated).toBe(false);
  });

  it('反斜杠相对路径也能读(Windows 用户手输)', () => {
    const out = readVaultTextFile(tmpVault, 'notes\\a.md');
    expect(out.content).toContain('hello vault');
  });

  it('绝对路径 → FORBIDDEN', () => {
    expect(() => readVaultTextFile(tmpVault, path.join(tmpRoot, 'outside-secret.md'))).toThrow(
      VaultFsError,
    );
  });

  it('.. 穿越 → FORBIDDEN', () => {
    let code = '';
    try {
      readVaultTextFile(tmpVault, '../outside-secret.md');
    } catch (e) {
      code = (e as VaultFsError).code;
    }
    expect(code).toBe('FORBIDDEN');
  });

  it('非白名单扩展(图片)→ UNSUPPORTED_TYPE', () => {
    let code = '';
    try {
      readVaultTextFile(tmpVault, 'assets/pic.png');
    } catch (e) {
      code = (e as VaultFsError).code;
    }
    expect(code).toBe('UNSUPPORTED_TYPE');
  });

  it('不存在 → NOT_FOUND', () => {
    let code = '';
    try {
      readVaultTextFile(tmpVault, 'notes/missing.md');
    } catch (e) {
      code = (e as VaultFsError).code;
    }
    expect(code).toBe('NOT_FOUND');
  });

  it('超过 maxBytes → 截断 truncated=true', () => {
    const big = path.join(tmpVault, 'big.log');
    fs.writeFileSync(big, 'x'.repeat(3000), 'utf8');
    const out = readVaultTextFile(tmpVault, 'big.log', { maxBytes: 1000 });
    expect(out.truncated).toBe(true);
    expect(out.content.length).toBeLessThanOrEqual(1000);
    expect(out.size).toBe(3000);
  });
});

// ── 路由契约(supertest)──────────────────────────────────────────────────────

describe('POST/GET/DELETE /api/knowledge-vaults', () => {
  it('注册 → 列表(health=active) → 同路径重复注册幂等(改名不改 id)', async () => {
    const created = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: `"${tmpVault}"` }); // 带引号粘贴路径也要能注册
    expect(created.status).toBe(200);
    expect(created.body.data.health).toBe('active');
    const id = created.body.data.vault_id as string;
    // name 缺省 = basename
    expect(created.body.data.name).toBe(path.basename(tmpVault));

    const again = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: tmpVault, name: '我的仓库' });
    expect(again.status).toBe(200);
    expect(again.body.data.vault_id).toBe(id);
    expect(again.body.data.name).toBe('我的仓库');

    const list = await request(app).get('/api/knowledge-vaults');
    expect(list.status).toBe(200);
    expect(list.body.data.vaults).toHaveLength(1);
  });

  it('相对路径 → 400 NOT_ABSOLUTE;不存在 → 404', async () => {
    const rel = await request(app).post('/api/knowledge-vaults').send({ path: 'relative/dir' });
    expect(rel.status).toBe(400);
    expect(rel.body.error.code).toBe('NOT_ABSOLUTE');

    const gone = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: path.join(tmpCwd, 'no-such-dir') });
    expect(gone.status).toBe(404);
  });

  it('create:true — 目标不存在时新建一级目录并注册', async () => {
    const newDir = path.join(tmpRoot, 'brand-new-vault');
    const res = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: newDir, name: '新仓库', create: true });
    expect(res.status).toBe(200);
    expect(res.body.data.health).toBe('active');
    expect(fs.statSync(newDir).isDirectory()).toBe(true);

    // 已存在 + create:true → 幂等(改回原名,不报错)
    const again = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: newDir, create: true });
    expect(again.status).toBe(200);

    await request(app).delete(`/api/knowledge-vaults/${res.body.data.vault_id}`);
    fs.rmSync(newDir, { recursive: true, force: true });
  });

  it('create:true — 父目录不存在 404 / 非法目录名 400 / 无 create 时不存在 404', async () => {
    const noParent = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: path.join(tmpRoot, 'ghost', 'v'), create: true });
    expect(noParent.status).toBe(404);
    expect(noParent.body.error.code).toBe('PARENT_NOT_FOUND');

    const badName = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: path.join(tmpRoot, '.hidden'), create: true });
    expect(badName.status).toBe(400);
    expect(badName.body.error.code).toBe('BAD_NAME');

    const noCreate = await request(app)
      .post('/api/knowledge-vaults')
      .send({ path: path.join(tmpRoot, 'not-created') });
    expect(noCreate.status).toBe(404);
    expect(fs.existsSync(path.join(tmpRoot, 'not-created'))).toBe(false);
  });

  it('files:端到端扫描(忽略 node_modules 等)+ touch last_opened_at', async () => {
    const list = await request(app).get('/api/knowledge-vaults');
    const id = list.body.data.vaults[0].vault_id as string;

    const files = await request(app).get(`/api/knowledge-vaults/${id}/files`);
    expect(files.status).toBe(200);
    expect(files.body.data.truncated).toBe(false);
    const paths = (files.body.data.files as Array<{ path: string }>).map((f) => f.path);
    expect(paths).toContain('notes/a.md');
    expect(paths.some((p) => p.includes('node_modules'))).toBe(false);

    const after = await request(app).get('/api/knowledge-vaults');
    expect(after.body.data.vaults[0].last_opened_at).toBeTruthy();
  });

  it('file:读文件 ok + .. 穿越 403 + 未知仓库 404', async () => {
    const list = await request(app).get('/api/knowledge-vaults');
    const id = list.body.data.vaults[0].vault_id as string;

    const read = await request(app)
      .get(`/api/knowledge-vaults/${id}/file`)
      .query({ path: 'notes/a.md' });
    expect(read.status).toBe(200);
    expect(read.body.data.content).toContain('hello vault');

    const escape = await request(app)
      .get(`/api/knowledge-vaults/${id}/file`)
      .query({ path: '../outside-secret.md' });
    expect(escape.status).toBe(403);

    const ghost = await request(app).get('/api/knowledge-vaults/no-such-id/file').query({ path: 'a.md' });
    expect(ghost.status).toBe(404);
  });

  it('移除只删注册行(REG-1:磁盘文件原样)', async () => {
    const list = await request(app).get('/api/knowledge-vaults');
    const id = list.body.data.vaults[0].vault_id as string;

    const del = await request(app).delete(`/api/knowledge-vaults/${id}`);
    expect(del.status).toBe(200);
    expect(del.body.data.deleted).toBe(true);
    expect(del.body.data.disk_untouched).toBe(true);

    // 用户文件必须还在、内容不变
    expect(fs.readFileSync(path.join(tmpVault, 'notes', 'a.md'), 'utf8')).toContain('hello vault');

    const after = await request(app).get('/api/knowledge-vaults');
    expect(after.body.data.vaults).toHaveLength(0);
  });
});

// ── 链接图谱(Obsidian 式,挂载即有)────────────────────────────────────────

describe('GET /api/knowledge-vaults/:id/graph', () => {
  it('wiki 双链 + 相对 md 链接建边,未解析双链成 ghost,外链忽略', async () => {
    // fixture 补一个带链接的文件
    w(path.join(tmpVault, 'notes', 'linked.md'),
      '参见 [[a]] 与 [[sub/b|别名]];详情 [b 笔记](sub/b.md);外部 [x](https://example.com);未解析 [[Ghost Note]]');

    const created = await request(app).post('/api/knowledge-vaults').send({ path: tmpVault });
    const id = created.body.data.vault_id as string;
    const res = await request(app).get(`/api/knowledge-vaults/${id}/graph`);
    expect(res.status).toBe(200);
    expect(res.body.meta.rag_backend).toBe('vault-links');

    const nodes = res.body.data.nodes as Array<{ id: string; raw_type: string; name?: string }>;
    const links = res.body.data.links as Array<{ source: string; target: string; relation: string }>;
    const ids = nodes.map((n) => n.id);

    // 文档节点存在
    expect(ids).toContain('notes/a.md');
    expect(ids).toContain('notes/sub/b.markdown');
    expect(ids).toContain('notes/linked.md');
    // 双链([[a]] 与 [[sub/b]])→ wiki 边
    expect(links).toContainEqual({ source: 'notes/linked.md', target: 'notes/a.md', relation: 'wiki' });
    expect(links).toContainEqual({ source: 'notes/linked.md', target: 'notes/sub/b.markdown', relation: 'wiki' });
    // 相对 md 链接 → ref 边
    expect(links).toContainEqual({ source: 'notes/linked.md', target: 'notes/sub/b.markdown', relation: 'ref' });
    // https 外链不产生任何以 http 开头的目标;未解析双链 → ghost 节点
    expect(links.some((l) => l.target.startsWith('http'))).toBe(false);
    const ghost = nodes.find((n) => n.raw_type === 'ghost');
    expect(ghost?.name).toBe('Ghost Note');
    // meta 计数一致
    expect(res.body.meta.node_count).toBe(nodes.length);
    expect(res.body.meta.link_count).toBe(links.length);
    expect(res.body.meta.scanned_files).toBeGreaterThan(0);

    await request(app).delete(`/api/knowledge-vaults/${id}`);
  });

  it('无链接的空仓库 → 空图(node_count = 文档数, link_count = 0)', async () => {
    const empty = path.join(tmpRoot, 'empty-vault');
    fs.mkdirSync(empty, { recursive: true });
    w(path.join(empty, 'solo.md'), '# 孤岛笔记,没有任何链接');
    const created = await request(app).post('/api/knowledge-vaults').send({ path: empty });
    const id = created.body.data.vault_id as string;
    const res = await request(app).get(`/api/knowledge-vaults/${id}/graph`);
    expect(res.status).toBe(200);
    expect(res.body.data.nodes).toHaveLength(1);
    expect(res.body.data.links).toHaveLength(0);
    await request(app).delete(`/api/knowledge-vaults/${id}`);
  });
});

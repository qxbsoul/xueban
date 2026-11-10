/**
 * clawhub-e2e.smoke.ts — ClawHub 端到端冒烟（QA 严过关，2026-06）
 *
 * 沿用 conversations-move-project.test.ts 的进程内挂载模式（不依赖常驻服务）：
 *   1. GET /api/cli/detect — ARC 注册项 + is_claw/installable 镜像字段
 *   2. POST /api/clawhub/install/hermes — 422 copy-only（白名单外边界）
 *   3. POST /api/clawhub/install/no-such-cli — 404
 *   4. POST /api/clawhub/install/:id + GET stream（node 注入白名单跑真实 spawn）
 *      验证 install-start → install-log* → install-done SSE 事件序
 *   5. POST /api/clawhub/install/:id/cancel — 204 + INSTALL_CANCELED 帧
 */
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import express from 'express';
import request from 'supertest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let app: express.Express;
let clawhubRouter: express.Router;
let cliRouter: express.Router;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let clawInstaller: typeof import('../lib/claw-installer');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let cliDetector: typeof import('../cli-detector');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let cliRegistry: typeof import('../cli-registry');
let restoreNodePm: () => void;

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-clawhub-e2e-'));
  process.chdir(tmp);

  clawInstaller = await import('../lib/claw-installer');
  cliDetector = await import('../cli-detector');
  cliRegistry = await import('../cli-registry');
  clawhubRouter = (await import('../routes/clawhub')).default;
  cliRouter = (await import('../routes/cli')).default;

  // 注入 node 为白名单 pm（真实 spawn 用，与单测同法）
  restoreNodePm = clawInstaller.__testInjectPm('node');

  const a = express();
  a.use(express.json());
  a.use('/api/clawhub', clawhubRouter);
  a.use('/api/cli', cliRouter);
  app = a;
});

afterAll(async () => {
  clawInstaller.__resetInstallerForTest();
  restoreNodePm();
  cliDetector.__resetDetectCacheForTest();
  process.chdir(origCwd);
  // Windows: spawn 的 which/where 子进程可能仍短暂持有 cwd 句柄 →
  // EBUSY。多次重试（force 容忍不存在；失败仅告警不影响结果）。
  for (let i = 0; i < 5; i++) {
    try {
      rmSync(tmp, { recursive: true, force: true });
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 300));
    }
  }
});

/** 临时把 ARC 的 install_spec 换成 node 命令（真实 spawn，秒级完成）。 */
function withArcSpec(argv: string[], fn: () => Promise<void>): Promise<void> {
  const arc = cliRegistry.KNOWN_CLIS.find((c) => c.id === 'autoresearchclaw');
  if (!arc) throw new Error('autoresearchclaw missing');
  const orig = arc.install_spec;
  const origCmd = arc.install_cmd;
  arc.install_spec = { pm: 'node', args: argv.slice(1) };
  return fn().finally(() => {
    arc.install_spec = orig;
    arc.install_cmd = origCmd;
  });
}

describe('E2E — GET /api/cli/detect（FR-01/07）', () => {
  it(
    'detect 列表含 autoresearchclaw + 全部镜像字段 + installable 派生',
    async () => {
      const res = await request(app).get('/api/cli/detect').expect(200);
      const items = res.body.items as Array<Record<string, unknown>>;
      expect(items.length).toBeGreaterThanOrEqual(22); // 既有 21 + ARC

      const arc = items.find((i) => i.id === 'autoresearchclaw');
      expect(arc).toBeDefined();
      expect(arc!.is_claw).toBe(true);
      expect(String(arc!.description)).toContain('研究');
      expect(arc!.category).toBe('research');
      // 2026-10-30 校准：ARC 是 git-clone 型（非 PyPI），copy-only 引导安装；
      // binary=researchclaw，卸载走 uninstall_cmd 引导（uninstallable=false）。
      expect(arc!.installable).toBe(false);
      expect(arc!.install_spec).toBeUndefined();
      expect(arc!.uninstallable).toBe(false);
      expect(String(arc!.uninstall_cmd)).toContain('AutoResearchClaw');
      expect(arc!.stream_format).toBe('plain-line');

      // is_claw 三件套在册（FR-07 AC1）
      const clawIds = items.filter((i) => i.is_claw).map((i) => i.id);
      expect(clawIds).toEqual(expect.arrayContaining(['openclaw', 'hermes', 'autoresearchclaw']));

      // 每个条目都有 installable 派生字段（DetectedCli 契约）
      expect(items.every((i) => typeof i.installable === 'boolean')).toBe(true);
      // hermes（中文文案）→ copy-only；cursor（中文）→ copy-only
      expect(items.find((i) => i.id === 'hermes')!.installable).toBe(false);
      expect(items.find((i) => i.id === 'cursor')!.installable).toBe(false);
    },
    120_000,
  );

  it(
    'detect/refresh 重扫可用（FR-09 AC2：30s 内全量重扫）',
    async () => {
      const t0 = Date.now();
      const res = await request(app).post('/api/cli/detect/refresh').expect(200);
      const ms = Date.now() - t0;
      expect(res.body.items.length).toBeGreaterThanOrEqual(22);
      expect(ms).toBeLessThan(30_000);
    },
    120_000,
  );
});

describe('E2E — POST /api/clawhub/install/:cliId 边界（FR-02）', () => {
  it('copy-only CLI（hermes）→ 422 INSTALL_NOT_EXECUTABLE', async () => {
    const res = await request(app).post('/api/clawhub/install/hermes').expect(422);
    expect(res.body.error).toBe('INSTALL_NOT_EXECUTABLE');
    expect(String(res.body.message)).toContain('copy-only');
  });

  it('未知 cliId → 404 CLI_NOT_FOUND', async () => {
    const res = await request(app).post('/api/clawhub/install/no-such-cli').expect(404);
    expect(res.body.error).toBe('CLI_NOT_FOUND');
  });

  it(
    '真实安装流：201 → SSE install-start → install-log* → install-done',
    async () => {
      await withArcSpec(
        ['node', '-e', 'console.log("smoke-line-1"); console.log("smoke-line-2");'],
        async () => {
          const res = await request(app)
            .post('/api/clawhub/install/autoresearchclaw')
            .expect(201);
          expect(res.body.install_id).toMatch(/^inst-/);
          expect(res.body.cli_id).toBe('autoresearchclaw');
          expect(res.body.command).toContain('node');
          expect(res.body.stream_url).toBe(
            `/api/clawhub/install/${res.body.install_id}/stream`,
          );

          // 等安装完成（真实 spawn node，秒级）
          const session = clawInstaller.getInstall(res.body.install_id)!;
          await session.finished;
          expect(['done', 'error']).toContain(session.status);

          // SSE 重放：drain 已累积事件
          const sse = await request(app)
            .get(`/api/clawhub/install/${res.body.install_id}/stream`)
            .expect(200);
          expect(sse.headers['content-type']).toContain('text/event-stream');
          const body = sse.text;
          expect(body).toContain('event: install-start');
          expect(body).toContain('smoke-line-1');
          expect(body).toContain('smoke-line-2');
          // 终态帧 = install-done（exit 0）
          expect(body).toContain('event: install-done');
          expect(body).toContain('"exit_code":0');
        },
      );
    },
    60_000,
  );

  it(
    '取消流：cancel → 204 → INSTALL_CANCELED 帧可重放',
    async () => {
      await withArcSpec(['node', '-e', 'setTimeout(() => {}, 60000)'], async () => {
        const res = await request(app).post('/api/clawhub/install/autoresearchclaw').expect(201);
        const installId = res.body.install_id;
        // 确认 running
        await new Promise((r) => setTimeout(r, 400));
        const sess = clawInstaller.getInstall(installId)!;
        expect(sess.status).toBe('running');

        await request(app).post(`/api/clawhub/install/${installId}/cancel`).expect(204);
        await sess.finished;

        const sse = await request(app)
          .get(`/api/clawhub/install/${installId}/stream`)
          .expect(200);
        expect(sse.text).toContain('INSTALL_CANCELED');
      });
    },
    60_000,
  );

  it('未知安装会话 stream → 404', async () => {
    const res = await request(app)
      .get('/api/clawhub/install/inst-none/stream')
      .expect(404);
    expect(res.body.error).toBe('INSTALL_SESSION_NOT_FOUND');
  });
});

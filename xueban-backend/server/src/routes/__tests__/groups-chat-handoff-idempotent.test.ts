/**
 * groups-chat-handoff-idempotent.test.ts — 群回合交接包幂等与锚编号回归（2026-11-07）。
 *
 * 薄壳 sinkRoundHandoff 直测（真实 SQLite 临时 cwd，不动 mock——动态 import
 * 拦不住模块内部 require，走真库最稳）：
 *   - 同回合二次触发只建一包（shift_id 幂等锚）
 *   - 同 shift_id 不同 from/to（发言序变化）也只建一包——不因三元组不同重复建包
 *   - A,B,A 发言序经 sink 不产自环丢包（from/to 去重语义，纯函数侧另有用例）
 *   - memory.autoLearn=false → 不落包
 *   - nextChatTurnSeq：0-hop 回合（无边团队）不撞旧轮锚号；负时间戳锚不占号
 *   - maxShiftTurnAnchor：只认「前缀+纯数字尾」，他组/异形锚不占号
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

let tmp: string;
let origCwd: string;
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let sqlite: typeof import('../../storage/sqlite');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let handoffs: typeof import('../../storage/memory-handoff');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let settings: typeof import('../../storage/settings');
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
let chat: typeof import('../groups-chat');

beforeAll(async () => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-round-handoff-'));
  process.chdir(tmp);
  sqlite = await import('../../storage/sqlite');
  sqlite._resetForTests();
  handoffs = await import('../../storage/memory-handoff');
  settings = await import('../../storage/settings');
  chat = await import('../groups-chat');
  sqlite.getDb();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

/** 某个群的全部交接包（按 shift_id 前缀筛，能数出重复建包）。 */
function byGroup(gid: string): Array<{ shift_id: string; from_agent: string; to_agent: string }> {
  return handoffs
    .listHandoffs({ limit: 200 })
    .filter((h) => h.shift_id.startsWith(`chat:${gid}:`))
    .map((h) => ({ shift_id: h.shift_id, from_agent: h.from_agent, to_agent: h.to_agent }));
}

describe('sinkRoundHandoff — 回合交接包幂等', () => {
  it('同回合二次触发只建一包', async () => {
    const input = {
      groupId: 'gA',
      turnSeq: 1,
      goal: '写周报',
      speakers: [
        { id: 'ia1', name: 'Atlas', excerpt: '我起草了' },
        { id: 'ia2', name: 'Nova', excerpt: '我复核了' },
      ],
    };
    await chat.sinkRoundHandoff(input);
    await chat.sinkRoundHandoff(input);
    const rows = byGroup('gA');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ shift_id: 'chat:gA:1', from_agent: 'ia2', to_agent: 'ia1' });
  });

  it('同 shift_id 不同发言序（from/to 变化）也只建一包', async () => {
    await chat.sinkRoundHandoff({
      groupId: 'gB',
      turnSeq: 7,
      goal: 'g',
      speakers: [
        { id: 'ib1', name: 'A', excerpt: 'x' },
        { id: 'ib2', name: 'B', excerpt: 'y' },
      ],
    });
    // 同回合再触发但发言序反转 → from/to 与首包不同；shift_id 锚应拦下第二包。
    await chat.sinkRoundHandoff({
      groupId: 'gB',
      turnSeq: 7,
      goal: 'g',
      speakers: [
        { id: 'ib2', name: 'B', excerpt: 'y' },
        { id: 'ib1', name: 'A', excerpt: 'x' },
      ],
    });
    expect(byGroup('gB')).toHaveLength(1);
  });

  it('A,B,A 发言序不产自环：sink 落包 from=末位 to=顺延下一位', async () => {
    await chat.sinkRoundHandoff({
      groupId: 'gC',
      turnSeq: 2,
      goal: 'g',
      speakers: [
        { id: 'ic1', name: 'A', excerpt: 'x' },
        { id: 'ic2', name: 'B', excerpt: 'y' },
        { id: 'ic1', name: 'A', excerpt: 'x2' },
      ],
    });
    const rows = byGroup('gC');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ from_agent: 'ic1', to_agent: 'ic2' });
  });

  it('memory.autoLearn=false → 不落包', async () => {
    settings.setSetting('memory.autoLearn', false);
    try {
      await chat.sinkRoundHandoff({
        groupId: 'gD',
        turnSeq: 1,
        goal: 'g',
        speakers: [
          { id: 'id1', name: 'A', excerpt: 'x' },
          { id: 'id2', name: 'B', excerpt: 'y' },
        ],
      });
      expect(byGroup('gD')).toHaveLength(0);
    } finally {
      settings.setSetting('memory.autoLearn', true);
    }
  });
});

describe('nextChatTurnSeq / maxShiftTurnAnchor — 交接锚编号', () => {
  it('0-hop 回合不撞旧轮锚号：sink 过的 turn 号不再复用', () => {
    // 无 hops 时 nextTurnSeq 恒为 1——旧实现会让每个无边回合都拿 turn 1。
    expect(chat.nextChatTurnSeq('gT')).toBe(1);
    handoffs.createHandoff({ shift_id: 'chat:gT:1', from_agent: 'a', to_agent: 'b' });
    expect(chat.nextChatTurnSeq('gT')).toBe(2);
    handoffs.createHandoff({ shift_id: 'chat:gT:2', from_agent: 'a', to_agent: 'b' });
    expect(chat.nextChatTurnSeq('gT')).toBe(3);
  });

  it('负时间戳锚（串行路径）与他组锚不占号', () => {
    handoffs.createHandoff({ shift_id: 'chat:gT:-1762000000000', from_agent: 'a', to_agent: 'b' });
    handoffs.createHandoff({ shift_id: 'chat:gT:9x', from_agent: 'a', to_agent: 'b' });
    handoffs.createHandoff({ shift_id: 'chat:gOther:42', from_agent: 'a', to_agent: 'b' });
    expect(handoffs.maxShiftTurnAnchor('chat:gT:')).toBe(2);
    expect(chat.nextChatTurnSeq('gT')).toBe(3);
    // 他组自己的锚只管自己。
    expect(chat.nextChatTurnSeq('gOther')).toBe(43);
  });
});

/**
 * groups-store.test.ts — 后端合一·阶段1a（2026-06-12）。
 *
 * 覆盖 storage/groups-store.ts：建群/读/列表三过滤(workspace/kind/archived)/PATCH/
 * 追加消息+读消息升序/DM find-or-create 幂等/reaction 增删/pin/user_settings per-user。
 * 关键断言移植自 Python tests/test_groups_api.py（name trim、archived 默认隐藏、
 * kind 过滤、policy_matrix 原样存等）。
 *
 * 隔离：每个用例 chdir 到临时目录 + sqlite._resetForTests()，避免污染真实
 * .xueban（参考 storage/conversations.test.ts 的 inIsolated 模式）。
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

import * as sqlite from '../sqlite';
import * as store from '../groups-store';

let origCwd: string;
let tmpDir: string;

beforeEach(() => {
  origCwd = process.cwd();
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-groups-store-'));
  process.chdir(tmpDir);
  sqlite._resetForTests();
  sqlite.getDb(); // boot schema in the fresh cwd
});

afterEach(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
});

describe('importGroupRecord (阶段2 迁移)', () => {
  it('保留原 group_id + 历史消息导入', () => {
    store.importGroupRecord({
      group_id: 'fixedid000000000000000000000000a',
      name: '迁移群',
      kind: 'group',
      agent_ids: ['a1'],
      created_at: '2026-01-01T00:00:00Z',
      messages: [
        { message_id: 'm1', sender_name: 'user', sender_kind: 'user', content: '你好', timestamp: '2026-01-01T00:01:00Z' },
        { message_id: 'm2', sender_name: 'sel', sender_kind: 'agent', content: '收到', timestamp: '2026-01-01T00:02:00Z', pinned: true },
      ],
    });
    const g = store.getGroup('fixedid000000000000000000000000a');
    expect(g).not.toBeNull();
    expect(g!.group_id).toBe('fixedid000000000000000000000000a');
    expect(g!.name).toBe('迁移群');
    expect(g!.agent_ids).toEqual(['a1']);
    expect(g!.messages).toHaveLength(2);
    expect(g!.messages[0].content).toBe('你好'); // 升序
    expect(g!.messages[1].pinned).toBe(true);
  });

  it('幂等：重跑同一 record 不翻倍消息', () => {
    const rec = {
      group_id: 'fixedid000000000000000000000000b',
      name: 'X',
      messages: [{ message_id: 'm1', content: 'a', timestamp: '2026-01-01T00:00:00Z' }],
    };
    store.importGroupRecord(rec);
    store.importGroupRecord(rec); // 重跑
    const g = store.getGroup('fixedid000000000000000000000000b');
    expect(g!.messages).toHaveLength(1); // 不翻倍
  });
});

describe('createGroup', () => {
  it('persists a group with defaulted list/dict fields', () => {
    const rec = store.createGroup({ name: 'Test Group' });
    expect(rec.group_id).toMatch(/^[0-9a-f]{32}$/);
    expect(rec.name).toBe('Test Group');
    expect(rec.template_id).toBe('');
    expect(rec.group_template_id).toBe('');
    expect(rec.agent_ids).toEqual([]);
    expect(rec.member_emails).toEqual([]);
    expect(rec.policy_matrix).toEqual({});
    expect(rec.kind).toBe('group');
    expect(rec.archived).toBe(false);
    expect(rec.user_settings).toEqual({});
    expect(rec.messages).toEqual([]);
    expect(typeof rec.created_at).toBe('string');
    // updated_at absent at create time (Python parity).
    expect(rec.updated_at).toBeNull();
  });

  it('trims whitespace in name (Python body.name.strip parity)', () => {
    const rec = store.createGroup({ name: '  Trimmed  ' });
    expect(rec.name).toBe('Trimmed');
  });

  it('stores template_id verbatim (template requirement dropped)', () => {
    const rec = store.createGroup({
      name: 'g',
      template_id: 'no-such-template-xyz',
    });
    expect(rec.template_id).toBe('no-such-template-xyz');
  });

  it('returns unique group_ids', () => {
    const a = store.createGroup({ name: 'A' });
    const b = store.createGroup({ name: 'B' });
    expect(a.group_id).not.toBe(b.group_id);
  });

  it('round-trips agent_ids / member_emails / policy_matrix as JSON', () => {
    const rec = store.createGroup({
      name: 'g',
      agent_ids: ['agent-a', 'agent-b'],
      member_emails: ['x@y.z'],
      policy_matrix: { 'agent-a|writeFile': 'allow' },
      workspace_id: 'ws-1',
      team_id: 'team-7',
      workspace_dir: '/tmp/scratch',
      conversation_id: 'conv-1',
    });
    const got = store.getGroup(rec.group_id)!;
    expect(got.agent_ids).toEqual(['agent-a', 'agent-b']);
    expect(got.member_emails).toEqual(['x@y.z']);
    expect(got.policy_matrix).toEqual({ 'agent-a|writeFile': 'allow' });
    expect(got.workspace_id).toBe('ws-1');
    expect(got.team_id).toBe('team-7');
    expect(got.workspace_dir).toBe('/tmp/scratch');
    expect(got.conversation_id).toBe('conv-1');
  });
});

describe('getGroup', () => {
  it('returns null for unknown id', () => {
    expect(store.getGroup('does-not-exist')).toBeNull();
  });
});

describe('listGroups filters', () => {
  it('excludes DM by default (kinds defaults to group)', () => {
    store.createGroup({ name: 'grp', kind: 'group' });
    store.createGroup({ name: 'dm', kind: 'dm', agent_ids: ['a'] });
    const list = store.listGroups();
    expect(list.length).toBe(1);
    expect(list[0].name).toBe('grp');
  });

  it('kinds:[dm] returns only DMs; kinds all returns both', () => {
    store.createGroup({ name: 'grp', kind: 'group' });
    store.createGroup({ name: 'dm', kind: 'dm', agent_ids: ['a'] });
    expect(store.listGroups({ kinds: ['dm'] }).length).toBe(1);
    expect(store.listGroups({ kinds: ['group', 'dm'] }).length).toBe(2);
  });

  it('hides archived by default; include_archived=true reveals them', () => {
    const g = store.createGroup({ name: 'doomed' });
    store.patchGroup(g.group_id, { archived: true });
    expect(store.listGroups().length).toBe(0);
    expect(store.listGroups({ include_archived: true }).length).toBe(1);
  });

  it('strict workspace scoping: filter matches only equal workspace_id', () => {
    store.createGroup({ name: 'in-ws', workspace_id: 'ws-1' });
    store.createGroup({ name: 'other-ws', workspace_id: 'ws-2' });
    store.createGroup({ name: 'no-ws', workspace_id: null });
    // explicit ws filter → only ws-1
    const scoped = store.listGroups({ workspace_id: 'ws-1' });
    expect(scoped.map((r) => r.name)).toEqual(['in-ws']);
    // null filter → only records whose workspace_id is null
    const nulls = store.listGroups({ workspace_id: null });
    expect(nulls.map((r) => r.name)).toEqual(['no-ws']);
    // no filter (undefined) → all three
    expect(store.listGroups({}).length).toBe(3);
  });
});

describe('patchGroup', () => {
  it('updates only provided fields and bumps updated_at', async () => {
    const g = store.createGroup({ name: 'orig' });
    expect(g.updated_at).toBeNull();
    const patched = store.patchGroup(g.group_id, {
      name: 'renamed',
      announcement: 'hello',
    })!;
    expect(patched.name).toBe('renamed');
    expect(patched.announcement).toBe('hello');
    expect(patched.updated_at).not.toBeNull();
  });

  it('archived patch toggles list visibility', () => {
    const g = store.createGroup({ name: 'g' });
    store.patchGroup(g.group_id, { archived: true });
    expect(store.getGroup(g.group_id)!.archived).toBe(true);
  });

  it('workspace_dir / conversation_id null clears the field', () => {
    const g = store.createGroup({
      name: 'g',
      workspace_dir: '/tmp/x',
      conversation_id: 'c1',
    });
    const patched = store.patchGroup(g.group_id, {
      workspace_dir: null,
      conversation_id: null,
    })!;
    expect(patched.workspace_dir).toBeNull();
    expect(patched.conversation_id).toBeNull();
  });

  it('returns null for unknown group', () => {
    expect(store.patchGroup('nope', { name: 'x' })).toBeNull();
  });
});

describe('messages', () => {
  it('appends messages with generated id/timestamp and reads them ascending', async () => {
    const g = store.createGroup({ name: 'g' });
    const m1 = store.appendGroupMessage(g.group_id, {
      content: 'first',
      sender_kind: 'user',
    });
    await new Promise((r) => setTimeout(r, 5));
    const m2 = store.appendGroupMessage(g.group_id, {
      content: 'second',
      sender_name: 'Agent A',
      sender_kind: 'agent',
    });
    expect(m1.message_id).toMatch(/^[0-9a-f]{32}$/);
    expect(m1.reactions).toEqual({});
    expect(m1.pinned).toBe(false);
    expect(m2.sender_name).toBe('Agent A');
    expect(m2.sender_kind).toBe('agent');

    const list = store.listGroupMessages(g.group_id);
    expect(list.map((m) => m.content)).toEqual(['first', 'second']);
    // updated_at synced on the group
    expect(store.getGroup(g.group_id)!.updated_at).not.toBeNull();
    // messages also surfaced via getGroup
    expect(store.getGroup(g.group_id)!.messages.length).toBe(2);
  });

  it('stores reply_to anchor', () => {
    const g = store.createGroup({ name: 'g' });
    const src = store.appendGroupMessage(g.group_id, { content: 'src' });
    const reply = store.appendGroupMessage(g.group_id, {
      content: 'reply',
      reply_to: src.message_id,
    });
    expect(reply.reply_to).toBe(src.message_id);
  });
});

describe('resolveDmGroup find-or-create', () => {
  it('creates a DM on first resolve and is idempotent', () => {
    const first = store.resolveDmGroup('agent-x', 'ws-1');
    expect(first.kind).toBe('dm');
    expect(first.agent_ids).toEqual(['agent-x']);
    expect(first.workspace_id).toBe('ws-1');
    const second = store.resolveDmGroup('agent-x', 'ws-1');
    expect(second.group_id).toBe(first.group_id);
    // only one DM exists
    expect(store.listGroups({ kinds: ['dm'] }).length).toBe(1);
  });

  it('distinguishes DMs across workspaces and agents', () => {
    const a = store.resolveDmGroup('agent-x', 'ws-1');
    const b = store.resolveDmGroup('agent-x', 'ws-2');
    const c = store.resolveDmGroup('agent-y', 'ws-1');
    expect(new Set([a.group_id, b.group_id, c.group_id]).size).toBe(3);
  });
});

describe('reactions', () => {
  it('adds then removes (toggle) and prunes empty buckets', () => {
    const g = store.createGroup({ name: 'g' });
    const m = store.appendGroupMessage(g.group_id, { content: 'x' });
    const added = store.setReaction(g.group_id, m.message_id, '👍', 'u1', true)!;
    expect(added['👍']).toEqual(['u1']);
    // second user
    const both = store.setReaction(g.group_id, m.message_id, '👍', 'u2', true)!;
    expect(both['👍'].sort()).toEqual(['u1', 'u2']);
    // dedupe: re-add u1 is a no-op
    const deduped = store.setReaction(g.group_id, m.message_id, '👍', 'u1', true)!;
    expect(deduped['👍'].sort()).toEqual(['u1', 'u2']);
    // remove u1
    const afterRemove = store.setReaction(g.group_id, m.message_id, '👍', 'u1', false)!;
    expect(afterRemove['👍']).toEqual(['u2']);
    // remove last → bucket pruned
    const empty = store.setReaction(g.group_id, m.message_id, '👍', 'u2', false)!;
    expect(empty['👍']).toBeUndefined();
    // persisted
    const reread = store.listGroupMessages(g.group_id)[0];
    expect(reread.reactions).toEqual({});
  });

  it('returns null for unknown message', () => {
    const g = store.createGroup({ name: 'g' });
    expect(store.setReaction(g.group_id, 'nope', '👍', 'u', true)).toBeNull();
  });
});

describe('pin', () => {
  it('toggles when pinned omitted; sets explicitly otherwise', () => {
    const g = store.createGroup({ name: 'g' });
    const m = store.appendGroupMessage(g.group_id, { content: 'x' });
    expect(store.setPinned(g.group_id, m.message_id)).toBe(true);
    expect(store.setPinned(g.group_id, m.message_id)).toBe(false);
    expect(store.setPinned(g.group_id, m.message_id, true)).toBe(true);
    expect(store.listGroupMessages(g.group_id)[0].pinned).toBe(true);
  });

  it('returns null for unknown message', () => {
    const g = store.createGroup({ name: 'g' });
    expect(store.setPinned(g.group_id, 'nope')).toBeNull();
  });
});

describe('user settings (per-user merge)', () => {
  it('returns clean defaults when user never saved', () => {
    const g = store.createGroup({ name: 'g' });
    const s = store.getUserSettings(g.group_id, 'anonymous')!;
    expect(s).toEqual({
      muted: false,
      pinned: false,
      folded: false,
      show_nickname: true,
      my_nickname: null,
    });
  });

  it('put then get round-trips and does not clobber other users', () => {
    const g = store.createGroup({ name: 'g' });
    store.putUserSettings(g.group_id, 'u1', {
      muted: true,
      pinned: true,
      folded: false,
      show_nickname: true,
      my_nickname: 'Alice',
    });
    store.putUserSettings(g.group_id, 'u2', {
      muted: false,
      pinned: false,
      folded: true,
      show_nickname: false,
      my_nickname: null,
    });
    expect(store.getUserSettings(g.group_id, 'u1')!.muted).toBe(true);
    expect(store.getUserSettings(g.group_id, 'u1')!.my_nickname).toBe('Alice');
    expect(store.getUserSettings(g.group_id, 'u2')!.folded).toBe(true);
    // u1 unaffected by u2 write
    expect(store.getUserSettings(g.group_id, 'u1')!.pinned).toBe(true);
    // untouched user still gets defaults
    expect(store.getUserSettings(g.group_id, 'u3')!.muted).toBe(false);
  });

  it('returns null for unknown group', () => {
    expect(store.getUserSettings('nope', 'u')).toBeNull();
    expect(
      store.putUserSettings('nope', 'u', {
        muted: false,
        pinned: false,
        folded: false,
        show_nickname: true,
        my_nickname: null,
      }),
    ).toBeNull();
  });
});

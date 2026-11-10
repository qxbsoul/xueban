/**
 * conversations.test.ts — Story 15.16 — Conversation + Message CRUD.
 *
 * Run:  npx tsx src/storage/conversations.test.ts   (from server/)
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('        detail:', detail);
  }
}

async function inIsolated(
  fn: (mods: {
    conv: typeof import('./conversations');
    proj: typeof import('./projects');
    sqlite: typeof import('./sqlite');
  }) => Promise<void> | void,
): Promise<void> {
  const orig = process.cwd();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-conv-test-'));
  process.chdir(tmp);
  try {
    const sqlite = await import('./sqlite');
    sqlite._resetForTests();
    const proj = await import('./projects');
    const conv = await import('./conversations');
    await fn({ conv, proj, sqlite });
    sqlite._resetForTests();
  } finally {
    process.chdir(orig);
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
}

async function main() {
  console.log('\n[1] create conversation under project');
  await inIsolated(async ({ conv, proj }) => {
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id, 'Main thread');
    check('conversation_id present', /^[0-9a-f-]{36}$/i.test(c.conversation_id));
    check('project_id linked', c.project_id === p.project_id);
    check('title stored', c.title === 'Main thread');

    const list = conv.listConversations(p.project_id);
    check('listConversations() length 1', list.length === 1);
    check('listed convo matches', list[0].conversation_id === c.conversation_id);
  });

  console.log('\n[2] appendMessage + listMessages ascending');
  await inIsolated(async ({ conv, proj }) => {
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    const m1 = conv.appendMessage(c.conversation_id, { role: 'user', content: 'hi' });
    await new Promise((r) => setTimeout(r, 5));
    const m2 = conv.appendMessage(c.conversation_id, {
      role: 'assistant',
      content: 'hello',
      run_id: 'run-xyz',
    });
    await new Promise((r) => setTimeout(r, 5));
    const m3 = conv.appendMessage(c.conversation_id, {
      role: 'system',
      content: 'context',
    });

    const all = conv.listMessages(c.conversation_id);
    check('3 messages persisted', all.length === 3);
    check(
      'order ASC by created_at',
      all[0].message_id === m1.message_id &&
        all[1].message_id === m2.message_id &&
        all[2].message_id === m3.message_id,
      all.map((m) => m.role),
    );
    check('run_id stored', all[1].run_id === 'run-xyz');
    check('user role stored', all[0].role === 'user');
  });

  console.log('\n[3] appendMessage updates conversation.updated_at');
  await inIsolated(async ({ conv, proj }) => {
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    const before = c.updated_at;
    await new Promise((r) => setTimeout(r, 10));
    conv.appendMessage(c.conversation_id, { role: 'user', content: 'm' });
    const after = conv.getConversation(c.conversation_id);
    check(
      'conversation.updated_at advanced',
      (after?.updated_at ?? '') > before,
      { before, after: after?.updated_at },
    );
  });

  console.log('\n[4] getRecentMessages limit + ASC return order');
  await inIsolated(async ({ conv, proj }) => {
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    for (let i = 0; i < 5; i++) {
      conv.appendMessage(c.conversation_id, {
        role: 'user',
        content: `msg-${i}`,
      });
      await new Promise((r) => setTimeout(r, 2));
    }
    const recent = conv.getRecentMessages(c.conversation_id, 3);
    check('limit honored', recent.length === 3);
    check('returns last 3 in ASC order', recent.map((m) => m.content).join(',') === 'msg-2,msg-3,msg-4', recent.map((m) => m.content));
  });

  console.log('\n[5] CASCADE: delete project removes messages');
  await inIsolated(async ({ conv, proj, sqlite }) => {
    const db = sqlite.getDb();
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    conv.appendMessage(c.conversation_id, { role: 'user', content: 'doomed' });

    proj.deleteProject(p.project_id);
    const remaining = (
      db
        .prepare(`SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?`)
        .get(c.conversation_id) as { n: number }
    ).n;
    check('messages cascaded out', remaining === 0, remaining);
  });

  // O3 — timeline_message_id back-trace bridge. assistant messages written by
  // the run-session stream carry the timeline projector's assistant_text id so
  // the front-end can map a timeline message → its conversation row.
  console.log('\n[6] O3 — appendMessage persists optional timeline_message_id');
  await inIsolated(async ({ conv, proj }) => {
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    const tmid = 'msg_sess-abc_0007';
    const m = conv.appendMessage(c.conversation_id, {
      role: 'assistant',
      content: 'answer',
      run_id: 'run-xyz',
      timeline_message_id: tmid,
    });
    check('returned record carries timeline_message_id', m.timeline_message_id === tmid, m.timeline_message_id);
    const all = conv.listMessages(c.conversation_id);
    check('listMessages reads back timeline_message_id', all[0].timeline_message_id === tmid, all[0].timeline_message_id);
    const recent = conv.getRecentMessages(c.conversation_id, 5);
    check('getRecentMessages reads back timeline_message_id', recent[0].timeline_message_id === tmid, recent[0].timeline_message_id);
  });

  console.log('\n[7] O3 — timeline_message_id is optional (back-compat: null when omitted)');
  await inIsolated(async ({ conv, proj }) => {
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    const m = conv.appendMessage(c.conversation_id, { role: 'user', content: 'hi' });
    check('omitted → null on returned record', m.timeline_message_id === null, m.timeline_message_id);
    const all = conv.listMessages(c.conversation_id);
    check('omitted → null on read', all[0].timeline_message_id === null, all[0].timeline_message_id);
  });

  console.log('\n[8] O3 — legacy rows (no column-value) read without error');
  await inIsolated(async ({ conv, proj, sqlite }) => {
    const db = sqlite.getDb();
    const p = proj.createProject({ name: 'p' });
    const c = conv.createConversation(p.project_id);
    // Simulate a pre-O3 row: INSERT only the original columns, leaving the new
    // timeline_message_id column at its default (NULL).
    db.prepare(
      `INSERT INTO messages (message_id, conversation_id, role, content, run_id, created_at)
       VALUES (?, ?, 'assistant', 'legacy', NULL, ?)`,
    ).run('legacy-mid', c.conversation_id, new Date().toISOString());
    const all = conv.listMessages(c.conversation_id);
    check('legacy row present', all.length === 1, all.length);
    check('legacy row timeline_message_id is null', all[0].timeline_message_id === null, all[0].timeline_message_id);
  });

  console.log('\n────────────────────────────────────────');
  console.log(`  ${pass} passed,  ${fail} failed`);
  console.log('────────────────────────────────────────\n');
  if (fail > 0) process.exit(1);
}

main().catch((e) => {
  console.error('test crashed:', e);
  process.exit(1);
});

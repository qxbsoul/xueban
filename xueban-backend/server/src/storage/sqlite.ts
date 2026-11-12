/**
 * sqlite.ts — Story 15.16 — single sqlite connection for the server.
 *
 * Why singleton:
 *   better-sqlite3 is sync; opening multiple connections to the same WAL db
 *   is supported but pointless for our single-process server. One handle,
 *   reused across routes.
 *
 * Boot sequence:
 *   1. mkdir .xueban/ (cwd-relative — matches existing JSON storage).
 *   2. Open / create app.sqlite.
 *   3. journal_mode = WAL (better crash safety + reader/writer parallelism).
 *   4. foreign_keys = ON (off by default in sqlite, must opt in).
 *   5. busy_timeout = 5s (avoid SQLITE_BUSY on concurrent route handlers).
 *   6. Run migrations/001-init.sql (idempotent — IF NOT EXISTS everywhere).
 *
 * Test isolation: _resetForTests() closes the handle so tests can chdir to
 * an isolated tmp dir and call getDb() again — the next getDb() call reads
 * the fresh cwd.
 */

import Database from 'better-sqlite3';
import fs from 'fs';
import path from 'path';

let _db: Database.Database | null = null;
let _initialized = false;

function paths() {
  const cwd = process.cwd();
  const dir = path.join(cwd, '.xueban');
  const file = path.join(dir, 'app.sqlite');
  // migrations live next to the compiled / source tree at server/migrations/.
  // __dirname when run via tsx points into server/src/storage, so go up two.
  const migration = path.join(__dirname, '..', '..', 'migrations', '001-init.sql');
  return { dir, file, migration };
}

/**
 * Add a column to a table if it isn't already present. Idempotent: reads
 * PRAGMA table_info and only issues ALTER TABLE when the column is missing.
 * `colDef` is the SQLite type/affinity (e.g. 'TEXT') — keep it nullable with
 * no NOT NULL/UNIQUE so the ALTER can never fail on an existing populated table.
 */
function addColumnIfMissing(
  db: Database.Database,
  table: string,
  column: string,
  colDef: string,
): void {
  const cols = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
  if (cols.some((c) => c.name === column)) return;
  db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${colDef}`);
}

export function getDb(): Database.Database {
  if (_db) return _db;

  const { dir, file, migration } = paths();
  const dbExisted = fs.existsSync(file);
  fs.mkdirSync(dir, { recursive: true });

  const db = new Database(file);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');

  // Always run the DDL — every statement is IF NOT EXISTS so re-running on
  // an existing db is a no-op. This also makes brand-new test cwds work.
  const ddl = fs.readFileSync(migration, 'utf-8');
  db.exec(ddl);

  // ── Idempotent additive column migrations ────────────────────────────────
  // SQLite has no `ADD COLUMN IF NOT EXISTS`, and CREATE TABLE IF NOT EXISTS
  // skips column changes on a db that already has the table. So for columns
  // added after the initial schema we ADD them only when absent. Each entry is
  // best-effort additive (nullable, no default constraint that could fail) so
  // an existing db is upgraded in place without a destructive rebuild.
  addColumnIfMissing(db, 'messages', 'timeline_message_id', 'TEXT');
  // Polynoia-parity chat-side message editing — stamp when a row was edited.
  addColumnIfMissing(db, 'messages', 'edited_at', 'TEXT');
  // B1 统一会话内核（2026-06-11）— 与 Python 群聊体系的显式互链：
  // conversation.group_id ↔ group.conversation_id（groups.py 同日加）。
  addColumnIfMissing(db, 'conversations', 'group_id', 'TEXT');
  // 后端合一·阶段0（2026-06-12）— 持久 run 索引（storage/run-index.ts）。
  // session↔conversation 映射不再依赖内存 sessionStore 的 TTL 生命周期。
  db.exec(`CREATE TABLE IF NOT EXISTS run_index (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_run_index_conversation ON run_index(conversation_id);`);

  // 后端合一·阶段1a（2026-06-12）— Node 侧群存储（storage/groups-store.ts），
  // 与 Python xueban/api/groups.py 的 JSON-file 群记录逐字段等价。1a 只产数据层，
  // 路由/开关由 1b 接入。蛇形列名对齐 Python record，list/dict 字段以 TEXT(JSON) 存。
  //   groups：群/DM 会话头（kind='group'|'dm'）。archived INTEGER(0/1)；
  //           agent_ids/member_emails/policy_matrix/user_settings 存 JSON 文本。
  //   group_messages：每条消息一行（拆出 messages 数组）；reactions 存 JSON 文本，
  //                   pinned INTEGER(0/1)；idx(group_id, timestamp) 支撑按时间升序读。
  db.exec(`CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    template_id TEXT NOT NULL DEFAULT '',
    group_template_id TEXT NOT NULL DEFAULT '',
    agent_ids TEXT NOT NULL DEFAULT '[]',
    member_emails TEXT NOT NULL DEFAULT '[]',
    policy_matrix TEXT NOT NULL DEFAULT '{}',
    workspace_id TEXT,
    workspace_dir TEXT,
    team_id TEXT,
    kind TEXT NOT NULL DEFAULT 'group',
    conversation_id TEXT,
    announcement TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    user_settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT
  );
  CREATE INDEX IF NOT EXISTS idx_groups_workspace ON groups(workspace_id);
  CREATE INDEX IF NOT EXISTS idx_groups_kind ON groups(kind);

  CREATE TABLE IF NOT EXISTS group_messages (
    message_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT 'user',
    sender_kind TEXT NOT NULL DEFAULT 'user',
    content TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    reply_to TEXT,
    reactions TEXT NOT NULL DEFAULT '{}',
    pinned INTEGER NOT NULL DEFAULT 0
  );
  CREATE INDEX IF NOT EXISTS idx_group_messages_group ON group_messages(group_id, timestamp);`);

  // 本地仓库注册表（2026-08-19，移植自 Shadow vault_registry）— 把任意本地文件夹
  // 注册为知识库仓库（Obsidian vault 模式）。path UNIQUE：同路径重复注册幂等
  // upsert；status 是注册级标记（active/archived），磁盘存活探测不落库。
  // 铁律（Shadow REG-1）：remove 只删注册行，永不触碰磁盘用户文件。
  db.exec(`CREATE TABLE IF NOT EXISTS knowledge_vaults (
    vault_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_opened_at TEXT,
    status TEXT NOT NULL DEFAULT 'active'
  );`);

  // 工作台公司 ↔ 多模态知识包映射。Python 负责文件与索引，Node 只保存归属，
  // 列表、上传、检索和对话召回都必须先校验公司，避免不同公司互相看到资料。
  db.exec(`CREATE TABLE IF NOT EXISTS xueban_knowledge_packs (
    company_id TEXT NOT NULL,
    pack_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (company_id, pack_id),
    UNIQUE (pack_id)
  );
  CREATE INDEX IF NOT EXISTS idx_xueban_knowledge_company ON xueban_knowledge_packs(company_id);`);

  // 经验记忆（2026-11-05，移植自 XUEBAN-OS PR#3 memory.py 设计）— 跨任务长期记忆：
  // run 结束时从已有事实自动提炼（失败→教训、中断→偏好），零 LLM 调用。
  // scope_key/task_type 分域召回（空=通用，不匹配不召回）；同域上限 8 条软归档；
  // 重复内容只加权（score+0.1 上限 1.0, hits+1）不新增。与手动 River Memory
  //（storage/memory-entries.ts JSON）并存双区、互不覆盖。
  db.exec(`CREATE TABLE IF NOT EXISTS memory_lessons (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'lesson',
    scope_key TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0.6,
    hits INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_memory_lessons_scope
    ON memory_lessons(scope_key, task_type, archived);`);

  // Agent 间任务交接（2026-11-05，设计移植自 CopilotKit/OpenBot 的 hop/work_items
  // 模型）— 每次 DAG 边遍历（A→B 交接）落一行，status 全生命周期可审计：
  // pending → running → done|failed|timeout|empty|denied|aborted。
  // 部分索引只覆盖未结算行（settled_at IS NULL），对齐 OpenBot「只索引未完成
  // work_items」——正常跑完的库绝大部分行已结算，该索引又小又准。
  db.exec(`CREATE TABLE IF NOT EXISTS agent_hops (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    turn_seq INTEGER NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    output_chars INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    settled_at INTEGER
  );
  CREATE INDEX IF NOT EXISTS idx_agent_hops_group_turn ON agent_hops(group_id, turn_seq);
  CREATE INDEX IF NOT EXISTS idx_agent_hops_open ON agent_hops(status) WHERE settled_at IS NULL;`);

  // Token 用量流水（2026-11-05，ledger 思想平移自 XUEBAN-OS PR#3 company.py）—
  // llm.ts 每次调用返回 usage 时落一条（input/output tokens + 估算成本），
  // 数据看板（routes/dashboard.ts）按天/按模型聚合现算。写入 best-effort，
  // 失败静默，绝不影响 LLM 主链路。
  db.exec(`CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_est_usd REAL NOT NULL DEFAULT 0
  );
  CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage(ts);
  CREATE INDEX IF NOT EXISTS idx_token_usage_session ON token_usage(session_id);`);
  addColumnIfMissing(db, 'token_usage', 'company_id', "TEXT NOT NULL DEFAULT ''");
  addColumnIfMissing(db, 'token_usage', 'cached_input_tokens', 'INTEGER NOT NULL DEFAULT 0');
  addColumnIfMissing(db, 'token_usage', 'cost_est_cny', 'REAL NOT NULL DEFAULT 0');
  addColumnIfMissing(db, 'token_usage', 'pricing_status', "TEXT NOT NULL DEFAULT 'unavailable'");
  db.exec('CREATE INDEX IF NOT EXISTS idx_token_usage_company_ts ON token_usage(company_id, ts)');

  // 记忆交接 + 长期画像写回（2026-11-05，功能线③ XUEBAN-OS PR#3 设计的另一半）—
  // 纯增量表，不动 memory_lessons / agent_hops 的既有语义。交接副本单独存
  // memory_handoff_items，不写回经验记忆表（避免挤占每域 8 条上限）。
  // 画像用「当前值表 + append-only 事件流水」双表：覆盖有规则，历史可回溯。
  db.exec(`CREATE TABLE IF NOT EXISTS memory_handoffs (
    id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    scope_key TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    accepted_at INTEGER,
    UNIQUE(shift_id, from_agent, to_agent)
  );
  CREATE INDEX IF NOT EXISTS idx_memory_handoffs_to
    ON memory_handoffs(to_agent, status);

  CREATE TABLE IF NOT EXISTS memory_handoff_items (
    id TEXT PRIMARY KEY,
    handoff_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'lesson',
    scope_key TEXT NOT NULL DEFAULT '',
    task_type TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0.6,
    created_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_memory_handoff_items_ref ON memory_handoff_items(handoff_id);

  CREATE TABLE IF NOT EXISTS memory_profile_values (
    subject_type TEXT NOT NULL DEFAULT 'customer',
    subject_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.7,
    source TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(subject_type, subject_id, key)
  );

  CREATE TABLE IF NOT EXISTS memory_profile_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL,
    old_confidence REAL,
    confidence REAL NOT NULL,
    action TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'system',
    created_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_memory_profile_events
    ON memory_profile_events(subject_id, key, id);`);

  // 交接快照可按学生主体过滤画像（2026-11-12）：老库补 subject_id 列。
  addColumnIfMissing(db, 'memory_handoffs', 'subject_id', "TEXT NOT NULL DEFAULT ''");

  // ── 学生长期记忆（2026-11-12）──────────────────────────────────────────────
  // 一条账号 = 一个学生，所有表按 owner_id 隔离；原始学习事件永不因记忆归档删除。
  db.exec(`
  CREATE TABLE IF NOT EXISTS student_learning_events (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'chat_turn',
    subject TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    evidence_quote TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_student_events_owner ON student_learning_events(owner_id, created_at);
  CREATE INDEX IF NOT EXISTS idx_student_events_conv ON student_learning_events(conversation_id);

  CREATE TABLE IF NOT EXISTS student_memories (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    memory_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.7,
    explicitness TEXT NOT NULL DEFAULT 'explicit',
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    source_conversation_id TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_confirmed_at INTEGER NOT NULL,
    expires_at INTEGER,
    UNIQUE(owner_id, memory_key)
  );
  CREATE INDEX IF NOT EXISTS idx_student_memories_owner ON student_memories(owner_id, status, subject);

  CREATE TABLE IF NOT EXISTS student_memory_evidence (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
  );
  CREATE UNIQUE INDEX IF NOT EXISTS idx_student_evidence_unique ON student_memory_evidence(memory_id, message_id, quote);
  CREATE INDEX IF NOT EXISTS idx_student_evidence_memory ON student_memory_evidence(memory_id);

  CREATE TABLE IF NOT EXISTS student_memory_versions (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    changed_by TEXT NOT NULL DEFAULT 'system',
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_student_versions_memory ON student_memory_versions(memory_id, version);

  CREATE TABLE IF NOT EXISTS student_memory_candidates (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    memory_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.7,
    explicitness TEXT NOT NULL DEFAULT 'explicit',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    suggested_action TEXT NOT NULL DEFAULT 'create',
    target_memory_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    reviewed_at INTEGER
  );
  CREATE INDEX IF NOT EXISTS idx_student_candidates_owner ON student_memory_candidates(owner_id, status, created_at);

  CREATE TABLE IF NOT EXISTS student_memory_jobs (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(owner_id, conversation_id, assistant_message_id)
  );
  CREATE INDEX IF NOT EXISTS idx_student_jobs_status ON student_memory_jobs(status, created_at);
  `);


  // 记忆按登录账号隔离（2026-11-12）：owner_id = xueban_users.id。
  // 空字符串 = 老数据/无账号上下文，只对同样没有账号上下文的调用可见。
  addColumnIfMissing(db, 'memory_lessons', 'owner_id', "TEXT NOT NULL DEFAULT ''");
  db.exec('CREATE INDEX IF NOT EXISTS idx_memory_lessons_owner ON memory_lessons(owner_id, scope_key, task_type, archived)');
  addColumnIfMissing(db, 'memory_handoffs', 'owner_id', "TEXT NOT NULL DEFAULT ''");
  db.exec('CREATE INDEX IF NOT EXISTS idx_memory_handoffs_owner ON memory_handoffs(owner_id, to_agent, status)');
  addColumnIfMissing(db, 'memory_profile_values', 'owner_id', "TEXT NOT NULL DEFAULT ''");
  addColumnIfMissing(db, 'memory_profile_events', 'owner_id', "TEXT NOT NULL DEFAULT ''");
  db.exec('CREATE INDEX IF NOT EXISTS idx_memory_profile_values_owner ON memory_profile_values(owner_id, subject_type, subject_id)');

  try {
    const pkCols = db
      .prepare(`SELECT name FROM pragma_table_info('memory_profile_values') WHERE pk > 0`)
      .all() as Array<{ name: string }>;
    if (!pkCols.some((col) => col.name === 'owner_id')) {
      db.transaction(() => {
        db.exec(`CREATE TABLE memory_profile_values_new (
          owner_id TEXT NOT NULL DEFAULT '',
          subject_type TEXT NOT NULL DEFAULT 'customer',
          subject_id TEXT NOT NULL,
          key TEXT NOT NULL,
          value TEXT NOT NULL,
          confidence REAL NOT NULL DEFAULT 0.7,
          source TEXT NOT NULL DEFAULT '',
          evidence TEXT NOT NULL DEFAULT '',
          updated_at INTEGER NOT NULL,
          PRIMARY KEY(owner_id, subject_type, subject_id, key)
        )`);
        const cols = (db.prepare(`PRAGMA table_info('memory_profile_values')`).all() as Array<{ name: string }>).map((c) => c.name);
        const ownerExpr = cols.includes('owner_id') ? 'owner_id' : "''";
        db.exec(`INSERT OR REPLACE INTO memory_profile_values_new
          (owner_id, subject_type, subject_id, key, value, confidence, source, evidence, updated_at)
          SELECT ${ownerExpr}, subject_type, subject_id, key, value, confidence, source, evidence, updated_at
            FROM memory_profile_values`);
        db.exec('DROP TABLE memory_profile_values');
        db.exec('ALTER TABLE memory_profile_values_new RENAME TO memory_profile_values');
      })();
    }
  } catch (error) {
    // 这是账号隔离的唯一键约束：迁移失败继续启动会让不同账号继续撞键，
    // 必须显式失败，不能当非致命异常吞掉。
    throw new Error(`memory_profile_values 账号主键迁移失败，拒绝启动：${(error as Error).message}`);
  }

  try {
    const uniqueIdx = db
      .prepare(`SELECT name FROM pragma_index_list('memory_handoffs') WHERE "unique" = 1`)
      .all() as Array<{ name: string }>;
    const hasOwnerUnique = uniqueIdx.some((idx) => {
      const cols = db.prepare(`SELECT name FROM pragma_index_info(?)`).all(idx.name) as Array<{ name: string }>;
      return cols.some((col) => col.name === 'owner_id');
    });
    if (!hasOwnerUnique) {
      db.transaction(() => {
        db.exec(`CREATE TABLE memory_handoffs_new (
          id TEXT PRIMARY KEY,
          owner_id TEXT NOT NULL DEFAULT '',
          subject_id TEXT NOT NULL DEFAULT '',
          shift_id TEXT NOT NULL,
          from_agent TEXT NOT NULL,
          to_agent TEXT NOT NULL,
          scope_key TEXT NOT NULL DEFAULT '',
          task_type TEXT NOT NULL DEFAULT '',
          summary TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending',
          context_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL,
          accepted_at INTEGER,
          UNIQUE(owner_id, shift_id, from_agent, to_agent)
        )`);
        const cols = (db.prepare(`PRAGMA table_info('memory_handoffs')`).all() as Array<{ name: string }>).map((c) => c.name);
        const ownerExpr = cols.includes('owner_id') ? 'owner_id' : "''";
        db.exec(`INSERT OR REPLACE INTO memory_handoffs_new
          (id, owner_id, subject_id, shift_id, from_agent, to_agent, scope_key, task_type, summary, status, context_json, created_at, accepted_at)
          SELECT id, ${ownerExpr}, subject_id, shift_id, from_agent, to_agent, scope_key, task_type, summary, status, context_json, created_at, accepted_at
            FROM memory_handoffs`);
        db.exec('DROP TABLE memory_handoffs');
        db.exec('ALTER TABLE memory_handoffs_new RENAME TO memory_handoffs');
        db.exec('CREATE INDEX IF NOT EXISTS idx_memory_handoffs_to ON memory_handoffs(to_agent, status)');
        db.exec('CREATE INDEX IF NOT EXISTS idx_memory_handoffs_owner ON memory_handoffs(owner_id, to_agent, status)');
      })();
    }
  } catch (error) {
    throw new Error(`memory_handoffs 账号唯一键迁移失败，拒绝启动：${(error as Error).message}`);
  }


  // 多产物登记（2026-11-07）— run_artifacts：一 run 多产物的独立登记表。
  // 动机：run-sessions 此前「last blueprint wins」（runs 表三字段只留最后一件
  // 产物），多产物 run 的其余产物在产物面板/项目产物里全部不可见。
  // 约定：runs.artifact_type/filename/url 三字段保留 = 主产物（向后兼容），
  // 本表按捕获序（seq）登记每一件 blueprint 产物。
  // 不挂 FOREIGN KEY：saveRun 用 INSERT OR REPLACE（PK 冲突先删后插），
  // 挂 FK 会级联清掉子行——改为 deleteRun/saveRun 清理时手动删孤儿行。
  db.exec(`CREATE TABLE IF NOT EXISTS run_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    artifact_type TEXT,
    artifact_filename TEXT,
    artifact_url TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, seq)
  );
  CREATE INDEX IF NOT EXISTS idx_run_artifacts_run ON run_artifacts(run_id, seq);`);

  _db = db;
  _initialized = true;
  console.log(`[sqlite] db ${dbExisted ? 'loaded' : 'initialized'} at ${file}`);
  return db;
}

/** Idempotent boot wrapper — call once from index.ts at startup. */
export function initSqlite(): Database.Database {
  return getDb();
}

/** Test helper: close handle so the next getDb() picks up a new cwd. */
export function _resetForTests(): void {
  if (_db) {
    try {
      _db.close();
    } catch {
      /* ignore */
    }
    _db = null;
    _initialized = false;
  }
}

export function isInitialized(): boolean {
  return _initialized;
}

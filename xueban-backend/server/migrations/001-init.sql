-- Story 15.16 — initial sqlite schema for 智慧学伴 server.
-- 5 tables: projects, conversations, messages, agents, runs.
-- Idempotent: every CREATE uses IF NOT EXISTS so re-running on an existing db
-- file is a no-op.
--
-- Notes:
--   * Timestamps stored as ISO-8601 TEXT (matches existing JSON shape so
--     migration is lossless).
--   * agents/runs.project_id is nullable + ON DELETE SET NULL — quick-hire
--     agents and pre-Story-15.16 runs have no project association.
--   * conversations + messages CASCADE — deleting a project nukes its chat
--     history but leaves agents/runs as orphans (intentional per spec).
--   * journal_mode=WAL + busy_timeout configured at runtime in sqlite.ts.

CREATE TABLE IF NOT EXISTS projects (
  project_id      TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  workspace_path  TEXT NOT NULL,
  skill_id        TEXT,
  design_system_id TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  title           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_project
  ON conversations(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  message_id      TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
  content         TEXT NOT NULL,
  run_id          TEXT,
  -- O3 — optional back-trace bridge: the timeline projector's assistant_text
  -- message id (msg_<session_id>_NNNN) that produced this row, so the
  -- front-end can map a timeline message → its persisted conversation row.
  -- NULL for user/system rows and for any row written before O3. Fresh DBs get
  -- the column here; existing DBs are upgraded by the guarded ALTER in
  -- sqlite.ts (SQLite has no ADD COLUMN IF NOT EXISTS).
  timeline_message_id TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS agents (
  agent_id      TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  soul          TEXT NOT NULL,
  workspace_id  TEXT NOT NULL DEFAULT 'default',
  blueprint     TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'idle',
  source        TEXT NOT NULL DEFAULT 'quick_hire',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_workspace ON agents(workspace_id);

CREATE TABLE IF NOT EXISTS runs (
  run_id              TEXT PRIMARY KEY,
  session_id          TEXT NOT NULL,
  project_id          TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
  conversation_id     TEXT REFERENCES conversations(conversation_id) ON DELETE SET NULL,
  goal                TEXT NOT NULL,
  skill_name          TEXT,
  skill_display_name  TEXT,
  artifact_type       TEXT,
  artifact_filename   TEXT,
  artifact_url        TEXT,
  project_dir         TEXT,
  status              TEXT NOT NULL DEFAULT 'completed',
  created_at          TEXT NOT NULL,
  completed_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_completed ON runs(completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, completed_at DESC);

-- ── Auth tables (SIWE + guest session) ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS auth_nonces (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  address     TEXT NOT NULL,
  nonce       TEXT NOT NULL UNIQUE,
  used        INTEGER NOT NULL DEFAULT 0,   -- 0=unused 1=used
  expires_at  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_auth_nonces_nonce ON auth_nonces(nonce);

CREATE TABLE IF NOT EXISTS auth_profiles (
  address      TEXT PRIMARY KEY,
  did          TEXT NOT NULL,
  display_name TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS auth_sessions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash  TEXT NOT NULL UNIQUE,   -- sha256(raw_token) hex
  address     TEXT NOT NULL,
  auth_type   TEXT NOT NULL DEFAULT 'wallet',  -- 'wallet' | 'guest'
  expires_at  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_token ON auth_sessions(token_hash);

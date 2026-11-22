import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import * as store from '../storage/learning';
import * as sqlite from '../storage/sqlite';

let tmp = '';
let origCwd = '';

beforeAll(() => {
  origCwd = process.cwd();
  tmp = mkdtempSync(path.join(tmpdir(), 'sf-legacy-db-'));
  process.chdir(tmp);
  const dir = path.join(tmp, '.xueban');
  mkdirSync(dir, { recursive: true });
  const db = new Database(path.join(dir, 'app.sqlite'));
  db.exec(`
    CREATE TABLE learning_courses (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
      subject TEXT DEFAULT '', grade TEXT DEFAULT '', source_type TEXT DEFAULT 'textbook',
      source_name TEXT DEFAULT '', status TEXT DEFAULT 'ready', stage TEXT DEFAULT 'done',
      progress INTEGER DEFAULT 100, error TEXT DEFAULT '', created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE TABLE learning_source_blocks (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, course_id TEXT NOT NULL,
      material_id TEXT DEFAULT '', block_type TEXT NOT NULL, ordinal INTEGER NOT NULL,
      text TEXT NOT NULL, markdown TEXT DEFAULT '', page INTEGER, line_start INTEGER DEFAULT 0,
      line_end INTEGER DEFAULT 0, bbox_json TEXT DEFAULT '{}', status TEXT DEFAULT 'parsed',
      created_at INTEGER NOT NULL
    );
    INSERT INTO learning_courses (id, owner_id, title, subject, grade, source_type, source_name, status, stage, progress, error, created_at, updated_at)
      VALUES ('legacy-course', 'legacy-owner', '旧课程', '数学', '七年级', 'textbook', '旧教材', 'ready', 'done', 100, '', 1, 1);
    INSERT INTO learning_source_blocks (id, owner_id, course_id, material_id, block_type, ordinal, text, markdown, page, line_start, line_end, bbox_json, status, created_at)
      VALUES ('legacy-block', 'legacy-owner', 'legacy-course', 'legacy-material', 'table', 1, '<table><tr><td>old</td></tr></table>', '<table><tr><td>old</td></tr></table>', 6, 1, 1, '{}', 'needs_review', 1);
  `);
  db.close();
  sqlite._resetForTests();
});

afterAll(() => {
  sqlite._resetForTests();
  process.chdir(origCwd);
  rmSync(tmp, { recursive: true, force: true });
});

describe('旧数据库 table_status 迁移', () => {
  it('自动加列、保留旧课程和旧表格，并默认待校验', () => {
    store.ensureLearningSchema();
    const course = store.getCourse('legacy-owner', 'legacy-course');
    expect(course?.title).toBe('旧课程');
    const block = store.listSourceBlocks('legacy-owner', 'legacy-course')[0];
    expect(block.id).toBe('legacy-block');
    expect(block.status).toBe('needs_review');
    expect(block.table_status).toBe('');
    const columns = sqlite.getDb().prepare("PRAGMA table_info(learning_source_blocks)").all() as Array<{ name: string }>;
    expect(columns.some((column) => column.name === 'table_status')).toBe(true);
  });
});
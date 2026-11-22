/**
 * 学习中心存储层（2026-11-15）。
 *
 * 说明：本阶段只建立课程、目录、教案、练习、进度与作答的数据结构，
 * 不实现真正的教材解析 / AI 出题 / AI 判题；生成器只产出明确标注的框架占位内容。
 */
import { randomUUID } from 'node:crypto';
import { getDb } from './sqlite';

export const COURSE_SOURCE_TYPES = ['textbook', 'curriculum', 'description'] as const;
export type CourseSourceType = (typeof COURSE_SOURCE_TYPES)[number];

export type CourseStatus = 'draft' | 'generating' | 'ready' | 'failed';
export type LessonStatus = 'not_started' | 'reading' | 'completed';
export type ExercisePhase = 'in_class' | 'post_class';

export interface LearningCourse {
  id: string;
  owner_id: string;
  title: string;
  subject: string;
  grade: string;
  source_type: CourseSourceType;
  source_name: string;
  status: CourseStatus;
  parse_quality: 'complete' | 'partial' | 'needs_review';
  quality_json: string;
  stage: string;
  progress: number;
  error: string;
  created_at: number;
  updated_at: number;
}

export interface LearningMaterial {
  id: string;
  owner_id: string;
  course_id: string;
  filename: string;
  mime: string;
  size: number;
  extraction_status: string;
  text_content: string;
  sha256: string;
  format: string;
  page_count: number | null;
  parse_audit_json: string;
  has_original: number;
  created_at: number;
}

export interface LearningUnit {
  id: string;
  owner_id: string;
  course_id: string;
  parent_id: string;
  level: 'chapter' | 'section';
  ordinal: number;
  title: string;
  summary: string;
  created_at: number;
}

export interface LearningLesson {
  id: string;
  owner_id: string;
  course_id: string;
  unit_id: string;
  ordinal: number;
  title: string;
  objective: string;
  content: string;
  key_points_json: string;
  source_refs_json: string;
  status: 'draft' | 'ready';
  version: number;
  created_at: number;
  updated_at: number;
}

export interface LearningExercise {
  id: string;
  owner_id: string;
  course_id: string;
  unit_id: string;
  lesson_id: string;
  phase: ExercisePhase;
  ordinal: number;
  stem: string;
  answer: string;
  explanation: string;
  difficulty: string;
  source_type: 'textbook' | 'generated';
  validation_status: 'placeholder' | 'unverified' | 'checked';
  source_ref_json: string;
  created_at: number;
}

export interface LessonProgress {
  owner_id: string;
  course_id: string;
  lesson_id: string;
  status: LessonStatus;
  completed_at: number | null;
  updated_at: number;
}

export interface ExerciseAttempt {
  id: string;
  owner_id: string;
  exercise_id: string;
  course_id: string;
  lesson_id: string;
  answer: string;
  status: string;
  feedback: string;
  created_at: number;
}

export interface GenerationJob {
  id: string;
  owner_id: string;
  course_id: string;
  status: 'queued' | 'running' | 'done' | 'failed';
  stage: string;
  error: string;
  attempts: number;
  batch_count: number;
  details_json: string;
  created_at: number;
  updated_at: number;
}

export interface GenerationEvent {
  id: string;
  job_id: string;
  owner_id: string;
  course_id: string;
  kind: string;
  message: string;
  payload_json: string;
  created_at: number;
}

export interface LearningSourceBlock {
  id: string;
  owner_id: string;
  course_id: string;
  material_id: string;
  block_type: 'heading' | 'paragraph' | 'formula' | 'table' | 'image' | 'list';
  ordinal: number;
  text: string;
  markdown: string;
  page: number | null;
  line_start: number;
  line_end: number;
  bbox_json: string;
  status: 'parsed' | 'needs_review';
  heading_level?: 'chapter' | 'section' | 'knowledge' | null;
  original_text?: string;
  page_status?: string;
  extraction_method?: string;
  created_at: number;
}

export interface LearningKnowledgePoint {
  id: string;
  owner_id: string;
  course_id: string;
  unit_id: string;
  lesson_id: string;
  ordinal: number;
  title: string;
  explanation: string;
  concepts_json: string;
  formulas_json: string;
  methods_json: string;
  prerequisites_json: string;
  source_block_ids_json: string;
  origin: 'textbook' | 'agent_inferred';
  status: 'parsed' | 'needs_review';
  confidence: number;
  created_at: number;
}

export interface LearningDiagnosis {
  id: string;
  owner_id: string;
  course_id: string;
  status: 'not_started' | 'queued' | 'ready' | 'failed';
  summary: string;
  mastery_json: string;
  error: string;
  created_at: number;
  updated_at: number;
}


let ensured = false;

export function ensureLearningSchema(): void {
  if (ensured) return;
  const db = getDb();
  db.exec(`
    CREATE TABLE IF NOT EXISTS learning_courses (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      title TEXT NOT NULL,
      subject TEXT NOT NULL DEFAULT '',
      grade TEXT NOT NULL DEFAULT '',
      source_type TEXT NOT NULL,
      source_name TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'draft',
      parse_quality TEXT NOT NULL DEFAULT 'complete',
      quality_json TEXT NOT NULL DEFAULT '{}',
      stage TEXT NOT NULL DEFAULT '',
      progress INTEGER NOT NULL DEFAULT 0,
      error TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_courses_owner ON learning_courses(owner_id, updated_at);

    CREATE TABLE IF NOT EXISTS learning_materials (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      filename TEXT NOT NULL,
      mime TEXT NOT NULL DEFAULT '',
      size INTEGER NOT NULL DEFAULT 0,
      extraction_status TEXT NOT NULL DEFAULT 'indexed',
      text_content TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_materials_course ON learning_materials(owner_id, course_id);

    CREATE TABLE IF NOT EXISTS learning_course_units (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      parent_id TEXT NOT NULL DEFAULT '',
      level TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      title TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_units_course ON learning_course_units(owner_id, course_id, ordinal);

    CREATE TABLE IF NOT EXISTS learning_lessons (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      unit_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      title TEXT NOT NULL,
      objective TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL DEFAULT '',
      key_points_json TEXT NOT NULL DEFAULT '[]',
      source_refs_json TEXT NOT NULL DEFAULT '[]',
      status TEXT NOT NULL DEFAULT 'draft',
      version INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_lessons_course ON learning_lessons(owner_id, course_id, ordinal);

    CREATE TABLE IF NOT EXISTS learning_exercises (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      unit_id TEXT NOT NULL DEFAULT '',
      lesson_id TEXT NOT NULL,
      phase TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      stem TEXT NOT NULL,
      answer TEXT NOT NULL DEFAULT '',
      explanation TEXT NOT NULL DEFAULT '',
      difficulty TEXT NOT NULL DEFAULT 'basic',
      source_type TEXT NOT NULL DEFAULT 'generated',
      validation_status TEXT NOT NULL DEFAULT 'placeholder',
      source_ref_json TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_exercises_lesson ON learning_exercises(owner_id, lesson_id, phase, ordinal);

    CREATE TABLE IF NOT EXISTS learning_lesson_progress (
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      lesson_id TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'not_started',
      completed_at INTEGER,
      updated_at INTEGER NOT NULL,
      PRIMARY KEY(owner_id, lesson_id)
    );

    CREATE TABLE IF NOT EXISTS learning_exercise_attempts (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      exercise_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      lesson_id TEXT NOT NULL,
      answer TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'submitted',
      feedback TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_attempts_owner ON learning_exercise_attempts(owner_id, created_at);

    CREATE TABLE IF NOT EXISTS learning_generation_jobs (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      stage TEXT NOT NULL DEFAULT 'queued',
      error TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_jobs_course ON learning_generation_jobs(owner_id, course_id);
    CREATE TABLE IF NOT EXISTS learning_generation_events (
      id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      message TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_events_job ON learning_generation_events(job_id, created_at);

    CREATE TABLE IF NOT EXISTS learning_diagnoses (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'not_started',
      summary TEXT NOT NULL DEFAULT '',
      mastery_json TEXT NOT NULL DEFAULT '{}',
      error TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_diagnoses_course ON learning_diagnoses(owner_id, course_id, created_at);

    CREATE TABLE IF NOT EXISTS learning_source_blocks (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      material_id TEXT NOT NULL DEFAULT '',
      block_type TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      text TEXT NOT NULL,
      markdown TEXT NOT NULL DEFAULT '',
      page INTEGER,
      line_start INTEGER NOT NULL DEFAULT 0,
      line_end INTEGER NOT NULL DEFAULT 0,
      bbox_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'parsed',
      heading_level TEXT NOT NULL DEFAULT '',
      original_text TEXT NOT NULL DEFAULT '',
      page_status TEXT NOT NULL DEFAULT '',
      extraction_method TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_blocks_course ON learning_source_blocks(owner_id, course_id, ordinal);

    CREATE TABLE IF NOT EXISTS learning_knowledge_points (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      course_id TEXT NOT NULL,
      unit_id TEXT NOT NULL,
      lesson_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      title TEXT NOT NULL,
      explanation TEXT NOT NULL DEFAULT '',
      concepts_json TEXT NOT NULL DEFAULT '[]',
      formulas_json TEXT NOT NULL DEFAULT '[]',
      methods_json TEXT NOT NULL DEFAULT '[]',
      source_block_ids_json TEXT NOT NULL DEFAULT '[]',
      status TEXT NOT NULL DEFAULT 'needs_review',
      confidence REAL NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_learning_points_lesson ON learning_knowledge_points(owner_id, lesson_id, ordinal);
  `);
  const addColumn = (table: string, column: string, definition: string) => {
    const columns = getDb().prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
    if (!columns.some((item) => item.name === column)) {
      getDb().exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
    }
  };
  addColumn('learning_source_blocks', 'heading_level', "TEXT NOT NULL DEFAULT ''");
  addColumn('learning_source_blocks', 'original_text', "TEXT NOT NULL DEFAULT ''");
  addColumn('learning_source_blocks', 'page_status', "TEXT NOT NULL DEFAULT ''");
  addColumn('learning_source_blocks', 'extraction_method', "TEXT NOT NULL DEFAULT ''");
  addColumn('learning_materials', 'original_blob', 'BLOB');
  addColumn('learning_materials', 'sha256', "TEXT NOT NULL DEFAULT ''");
  addColumn('learning_materials', 'format', "TEXT NOT NULL DEFAULT 'text'");
  addColumn('learning_materials', 'page_count', 'INTEGER');
  addColumn('learning_materials', 'parse_audit_json', "TEXT NOT NULL DEFAULT '{}'");
  addColumn('learning_course_units', 'source_ref_json', "TEXT NOT NULL DEFAULT '[]'");
  addColumn('learning_course_units', 'status', "TEXT NOT NULL DEFAULT 'needs_review'");
  addColumn('learning_lessons', 'concepts_json', "TEXT NOT NULL DEFAULT '[]'");
  addColumn('learning_lessons', 'formulas_json', "TEXT NOT NULL DEFAULT '[]'");
  addColumn('learning_lessons', 'methods_json', "TEXT NOT NULL DEFAULT '[]'");
  addColumn('learning_knowledge_points', 'prerequisites_json', "TEXT NOT NULL DEFAULT '[]'");
  addColumn('learning_knowledge_points', 'origin', "TEXT NOT NULL DEFAULT 'textbook'");
  addColumn('learning_generation_jobs', 'attempts', 'INTEGER NOT NULL DEFAULT 0');
  addColumn('learning_generation_jobs', 'batch_count', 'INTEGER NOT NULL DEFAULT 0');
  addColumn('learning_generation_jobs', 'details_json', "TEXT NOT NULL DEFAULT '{}'");
  addColumn('learning_courses', 'parse_quality', "TEXT NOT NULL DEFAULT 'complete'");
  addColumn('learning_courses', 'quality_json', "TEXT NOT NULL DEFAULT '{}'");
  ensured = true;
}

function now(): number {
  return Date.now();
}

export function createCourse(input: {
  owner_id: string;
  title: string;
  subject?: string;
  grade?: string;
  source_type: CourseSourceType;
  source_name?: string;
}): LearningCourse {
  ensureLearningSchema();
  const id = randomUUID();
  const time = now();
  getDb().prepare(
    `INSERT INTO learning_courses
      (id, owner_id, title, subject, grade, source_type, source_name, status, stage, progress, error, created_at, updated_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.title, input.subject || '', input.grade || '',
    input.source_type, input.source_name || '', 'draft', 'created', 0, '', time, time,
  );
  return getCourse(input.owner_id, id)!;
}

export function getCourse(ownerId: string, courseId: string): LearningCourse | undefined {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_courses WHERE owner_id = ? AND id = ?`)
    .get(ownerId, courseId) as LearningCourse | undefined;
}

export function listCourses(ownerId: string): LearningCourse[] {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_courses WHERE owner_id = ? ORDER BY updated_at DESC`)
    .all(ownerId) as LearningCourse[];
}

export function updateCourseStatus(
  ownerId: string,
  courseId: string,
  patch: {
    status?: CourseStatus;
    stage?: string;
    progress?: number;
    error?: string;
    parse_quality?: LearningCourse['parse_quality'];
    quality?: unknown;
  },
): void {
  ensureLearningSchema();
  const course = getCourse(ownerId, courseId);
  if (!course) return;
  getDb().prepare(
    `UPDATE learning_courses
        SET status = ?, parse_quality = ?, quality_json = ?, stage = ?, progress = ?, error = ?, updated_at = ?
      WHERE id = ? AND owner_id = ?`,
  ).run(
    patch.status ?? course.status,
    patch.parse_quality ?? course.parse_quality,
    patch.quality === undefined ? course.quality_json : JSON.stringify(patch.quality),
    patch.stage ?? course.stage,
    patch.progress ?? course.progress,
    patch.error ?? course.error,
    now(), courseId, ownerId,
  );
}

export function updateCourseMeta(ownerId: string, courseId: string, patch: {
  title?: string;
  subject?: string;
  grade?: string;
}): void {
  ensureLearningSchema();
  const course = getCourse(ownerId, courseId);
  if (!course) return;
  getDb().prepare(
    `UPDATE learning_courses SET title = ?, subject = ?, grade = ?, updated_at = ? WHERE id = ? AND owner_id = ?`,
  ).run(
    patch.title?.slice(0, 100) || course.title,
    patch.subject?.slice(0, 20) || course.subject,
    patch.grade?.slice(0, 20) || course.grade,
    now(), courseId, ownerId,
  );
}

export function updateKnowledgePointPrerequisites(ownerId: string, pointId: string, prerequisites: string[]): void {
  ensureLearningSchema();
  getDb().prepare(
    `UPDATE learning_knowledge_points SET prerequisites_json = ? WHERE id = ? AND owner_id = ?`,
  ).run(JSON.stringify(prerequisites), pointId, ownerId);
}

export function deleteCourse(ownerId: string, courseId: string): boolean {
  ensureLearningSchema();
  const db = getDb();
  const exists = getCourse(ownerId, courseId);
  if (!exists) return false;
  const tx = db.transaction(() => {
    db.prepare(`DELETE FROM learning_exercises WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_knowledge_points WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_lessons WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_course_units WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_source_blocks WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_materials WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_lesson_progress WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_generation_jobs WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_generation_events WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_courses WHERE owner_id = ? AND id = ?`).run(ownerId, courseId);
  });
  tx();
  return true;
}

export function clearCourseStructure(ownerId: string, courseId: string): void {
  ensureLearningSchema();
  const db = getDb();
  // 调用方负责事务边界；不要在事务内部再开事务。
  db.prepare(`DELETE FROM learning_exercises WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
  db.prepare(`DELETE FROM learning_knowledge_points WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
  db.prepare(`DELETE FROM learning_lessons WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
  db.prepare(`DELETE FROM learning_course_units WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
  db.prepare(`DELETE FROM learning_lesson_progress WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
}

export function runInTransaction<T>(callback: () => T): T {
  ensureLearningSchema();
  return getDb().transaction(callback)();
}

export function deleteSourceBlocksExcept(ownerId: string, courseId: string, keepIds: string[]): void {
  ensureLearningSchema();
  const db = getDb();
  if (keepIds.length === 0) {
    db.prepare(`DELETE FROM learning_source_blocks WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    return;
  }
  const placeholders = keepIds.map(() => '?').join(',');
  db.prepare(
    `DELETE FROM learning_source_blocks WHERE owner_id = ? AND course_id = ? AND id NOT IN (${placeholders})`,
  ).run(ownerId, courseId, ...keepIds);
}

export function deleteSourceBlocksByIds(ownerId: string, ids: string[]): void {
  ensureLearningSchema();
  if (!ids.length) return;
  const db = getDb();
  const placeholders = ids.map(() => '?').join(',');
  db.prepare(`DELETE FROM learning_source_blocks WHERE owner_id = ? AND id IN (${placeholders})`).run(ownerId, ...ids);
}

export function createSourceBlock(input: {
  owner_id: string;
  course_id: string;
  material_id: string;
  block_type: LearningSourceBlock['block_type'];
  ordinal: number;
  text: string;
  markdown?: string;
  page?: number | null;
  line_start: number;
  line_end: number;
  bbox?: unknown;
  status?: 'parsed' | 'needs_review';
  heading_level?: 'chapter' | 'section' | 'knowledge' | null;
  original_text?: string;
  page_status?: string;
  extraction_method?: string;
}): LearningSourceBlock {
  ensureLearningSchema();
  const id = randomUUID();
  getDb().prepare(
    `INSERT INTO learning_source_blocks
      (id, owner_id, course_id, material_id, block_type, ordinal, text, markdown, page, line_start, line_end, bbox_json, status, heading_level, original_text, page_status, extraction_method, created_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.course_id, input.material_id, input.block_type, input.ordinal,
    input.text, input.markdown ?? input.text, input.page ?? null, input.line_start, input.line_end,
    JSON.stringify(input.bbox || {}), input.status || 'parsed', input.heading_level || '',
    input.original_text || input.text, input.page_status || '', input.extraction_method || '', now(),
  );
  return getDb().prepare(`SELECT * FROM learning_source_blocks WHERE id = ?`).get(id) as LearningSourceBlock;
}

export function listSourceBlocks(ownerId: string, courseId: string): LearningSourceBlock[] {
  ensureLearningSchema();
  return getDb().prepare(
    `SELECT * FROM learning_source_blocks WHERE owner_id = ? AND course_id = ? ORDER BY ordinal`,
  ).all(ownerId, courseId) as LearningSourceBlock[];
}

export function createKnowledgePoint(input: {
  owner_id: string;
  course_id: string;
  unit_id: string;
  lesson_id: string;
  ordinal: number;
  title: string;
  explanation: string;
  concepts?: string[];
  formulas?: string[];
  methods?: string[];
  prerequisites?: string[];
  source_block_ids?: string[];
  origin?: 'textbook' | 'agent_inferred';
  status?: 'parsed' | 'needs_review';
  confidence?: number;
}): LearningKnowledgePoint {
  ensureLearningSchema();
  const id = randomUUID();
  getDb().prepare(
    `INSERT INTO learning_knowledge_points
      (id, owner_id, course_id, unit_id, lesson_id, ordinal, title, explanation, concepts_json, formulas_json,
       methods_json, prerequisites_json, source_block_ids_json, origin, status, confidence, created_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.course_id, input.unit_id, input.lesson_id, input.ordinal,
    input.title, input.explanation, JSON.stringify(input.concepts || []), JSON.stringify(input.formulas || []),
    JSON.stringify(input.methods || []), JSON.stringify(input.prerequisites || []),
    JSON.stringify(input.source_block_ids || []), input.origin || 'textbook',
    input.status || 'needs_review', input.confidence ?? 0, now(),
  );
  return getDb().prepare(`SELECT * FROM learning_knowledge_points WHERE id = ?`).get(id) as LearningKnowledgePoint;
}

export function listKnowledgePoints(ownerId: string, courseId: string, lessonId?: string): LearningKnowledgePoint[] {
  ensureLearningSchema();
  if (lessonId) {
    return getDb().prepare(
      `SELECT * FROM learning_knowledge_points WHERE owner_id = ? AND course_id = ? AND lesson_id = ? ORDER BY ordinal`,
    ).all(ownerId, courseId, lessonId) as LearningKnowledgePoint[];
  }
  return getDb().prepare(
    `SELECT * FROM learning_knowledge_points WHERE owner_id = ? AND course_id = ? ORDER BY lesson_id, ordinal`,
  ).all(ownerId, courseId) as LearningKnowledgePoint[];
}

export function clearCourseContent(ownerId: string, courseId: string): void {
  ensureLearningSchema();
  const db = getDb();
  const tx = db.transaction(() => {
    db.prepare(`DELETE FROM learning_exercises WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_knowledge_points WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_lessons WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_course_units WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
    db.prepare(`DELETE FROM learning_source_blocks WHERE owner_id = ? AND course_id = ?`).run(ownerId, courseId);
  });
  tx();
}

export function createMaterial(input: {
  owner_id: string;
  course_id: string;
  filename: string;
  mime?: string;
  size?: number;
  extraction_status?: string;
  text_content: string;
  original?: Buffer;
  sha256?: string;
  format?: string;
  page_count?: number | null;
  parse_audit_json?: string;
}): LearningMaterial {
  ensureLearningSchema();
  const id = randomUUID();
  getDb().prepare(
    `INSERT INTO learning_materials
      (id, owner_id, course_id, filename, mime, size, extraction_status, text_content, original_blob, sha256, format, page_count, parse_audit_json, created_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.course_id, input.filename, input.mime || '',
    input.size || 0, input.extraction_status || 'indexed', input.text_content,
    input.original || null, input.sha256 || '', input.format || 'text', input.page_count ?? null,
    input.parse_audit_json || '{}', now(),
  );
  return getDb().prepare(`SELECT * FROM learning_materials WHERE id = ?`).get(id) as LearningMaterial;
}

export function listMaterials(ownerId: string, courseId: string): LearningMaterial[] {
  ensureLearningSchema();
  // 不返回原始二进制，仅返回是否保留了原始文件。
  return getDb().prepare(
    `SELECT id, owner_id, course_id, filename, mime, size, extraction_status, text_content, sha256, format, page_count,
            parse_audit_json, CASE WHEN original_blob IS NULL THEN 0 ELSE 1 END AS has_original, created_at
       FROM learning_materials WHERE owner_id = ? AND course_id = ? ORDER BY created_at`,
  ).all(ownerId, courseId) as LearningMaterial[];
}

export function getMaterialSource(ownerId: string, courseId: string): {
  filename: string;
  mime: string;
  blob: Buffer | null;
  text: string;
  audit: string;
} | undefined {
  ensureLearningSchema();
  const row = getDb().prepare(
    `SELECT filename, mime, original_blob, text_content, parse_audit_json FROM learning_materials
      WHERE owner_id = ? AND course_id = ? ORDER BY created_at DESC LIMIT 1`,
  ).get(ownerId, courseId) as { filename: string; mime: string; original_blob: Buffer | null; text_content: string; parse_audit_json: string } | undefined;
  if (!row) return undefined;
  return { filename: row.filename, mime: row.mime, blob: row.original_blob || null, text: row.text_content, audit: row.parse_audit_json || '{}' };
}

export function createUnit(input: {
  owner_id: string;
  course_id: string;
  parent_id?: string;
  level: 'chapter' | 'section';
  ordinal: number;
  title: string;
  summary?: string;
  source_ref?: unknown;
  status?: 'parsed' | 'needs_review';
}): LearningUnit {
  ensureLearningSchema();
  const id = randomUUID();
  getDb().prepare(
    `INSERT INTO learning_course_units
      (id, owner_id, course_id, parent_id, level, ordinal, title, summary, source_ref_json, status, created_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.course_id, input.parent_id || '', input.level,
    input.ordinal, input.title, input.summary || '', JSON.stringify(input.source_ref || []),
    input.status || 'needs_review', now(),
  );
  return getDb().prepare(`SELECT * FROM learning_course_units WHERE id = ?`).get(id) as LearningUnit;
}

export function listUnits(ownerId: string, courseId: string): LearningUnit[] {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_course_units WHERE owner_id = ? AND course_id = ? ORDER BY ordinal`)
    .all(ownerId, courseId) as LearningUnit[];
}

export function createLesson(input: {
  owner_id: string;
  course_id: string;
  unit_id: string;
  ordinal: number;
  title: string;
  objective: string;
  content: string;
  key_points: string[];
  source_refs: unknown[];
  concepts?: string[];
  formulas?: string[];
  methods?: string[];
  status?: 'draft' | 'ready';
}): LearningLesson {
  ensureLearningSchema();
  const id = randomUUID();
  const time = now();
  getDb().prepare(
    `INSERT INTO learning_lessons
      (id, owner_id, course_id, unit_id, ordinal, title, objective, content, key_points_json, source_refs_json,
       concepts_json, formulas_json, methods_json, status, version, created_at, updated_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.course_id, input.unit_id, input.ordinal, input.title,
    input.objective, input.content, JSON.stringify(input.key_points), JSON.stringify(input.source_refs),
    JSON.stringify(input.concepts || []), JSON.stringify(input.formulas || []), JSON.stringify(input.methods || []),
    input.status || 'draft', 1, time, time,
  );
  return getLesson(input.owner_id, id)!;
}

export function getLesson(ownerId: string, lessonId: string): LearningLesson | undefined {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_lessons WHERE owner_id = ? AND id = ?`)
    .get(ownerId, lessonId) as LearningLesson | undefined;
}

export function listLessons(ownerId: string, courseId: string): LearningLesson[] {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_lessons WHERE owner_id = ? AND course_id = ? ORDER BY ordinal`)
    .all(ownerId, courseId) as LearningLesson[];
}

export function createExercise(input: {
  owner_id: string;
  course_id: string;
  unit_id: string;
  lesson_id: string;
  phase: ExercisePhase;
  ordinal: number;
  stem: string;
  answer?: string;
  explanation?: string;
  difficulty?: string;
  source_type?: 'textbook' | 'generated';
  validation_status?: 'placeholder' | 'unverified' | 'checked';
  source_ref?: unknown;
}): LearningExercise {
  ensureLearningSchema();
  const id = randomUUID();
  getDb().prepare(
    `INSERT INTO learning_exercises
      (id, owner_id, course_id, unit_id, lesson_id, phase, ordinal, stem, answer, explanation,
       difficulty, source_type, validation_status, source_ref_json, created_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.course_id, input.unit_id, input.lesson_id, input.phase, input.ordinal,
    input.stem, input.answer || '', input.explanation || '', input.difficulty || 'basic',
    input.source_type || 'generated', input.validation_status || 'placeholder',
    JSON.stringify(input.source_ref || {}), now(),
  );
  return getDb().prepare(`SELECT * FROM learning_exercises WHERE id = ?`).get(id) as LearningExercise;
}

export function listExercises(ownerId: string, filters: {
  course_id?: string;
  lesson_id?: string;
  phase?: ExercisePhase;
}): LearningExercise[] {
  ensureLearningSchema();
  let sql = `SELECT * FROM learning_exercises WHERE owner_id = ?`;
  const args: unknown[] = [ownerId];
  if (filters.course_id) { sql += ` AND course_id = ?`; args.push(filters.course_id); }
  if (filters.lesson_id) { sql += ` AND lesson_id = ?`; args.push(filters.lesson_id); }
  if (filters.phase) { sql += ` AND phase = ?`; args.push(filters.phase); }
  sql += ` ORDER BY lesson_id, phase, ordinal`;
  return getDb().prepare(sql).all(...args) as LearningExercise[];
}

export function getExercise(ownerId: string, exerciseId: string): LearningExercise | undefined {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_exercises WHERE owner_id = ? AND id = ?`)
    .get(ownerId, exerciseId) as LearningExercise | undefined;
}

export function getProgress(ownerId: string, lessonId: string): LessonProgress | undefined {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_lesson_progress WHERE owner_id = ? AND lesson_id = ?`)
    .get(ownerId, lessonId) as LessonProgress | undefined;
}

export function listProgress(ownerId: string, courseId: string): LessonProgress[] {
  ensureLearningSchema();
  return getDb().prepare(`SELECT * FROM learning_lesson_progress WHERE owner_id = ? AND course_id = ?`)
    .all(ownerId, courseId) as LessonProgress[];
}

export function setLessonProgress(ownerId: string, courseId: string, lessonId: string, status: LessonStatus): LessonProgress {
  ensureLearningSchema();
  const time = now();
  const completed = status === 'completed' ? time : null;
  getDb().prepare(
    `INSERT INTO learning_lesson_progress (owner_id, course_id, lesson_id, status, completed_at, updated_at)
     VALUES (?,?,?,?,?,?)
     ON CONFLICT(owner_id, lesson_id) DO UPDATE SET status = excluded.status,
       completed_at = excluded.completed_at, updated_at = excluded.updated_at`,
  ).run(ownerId, courseId, lessonId, status, completed, time);
  return getProgress(ownerId, lessonId)!;
}

export function isLessonCompleted(ownerId: string, lessonId: string): boolean {
  return getProgress(ownerId, lessonId)?.status === 'completed';
}

export function createAttempt(input: {
  owner_id: string;
  exercise_id: string;
  course_id: string;
  lesson_id: string;
  answer: string;
  status?: string;
  feedback?: string;
}): ExerciseAttempt {
  ensureLearningSchema();
  const id = randomUUID();
  getDb().prepare(
    `INSERT INTO learning_exercise_attempts (id, owner_id, exercise_id, course_id, lesson_id, answer, status, feedback, created_at)
     VALUES (?,?,?,?,?,?,?,?,?)`,
  ).run(
    id, input.owner_id, input.exercise_id, input.course_id, input.lesson_id,
    input.answer, input.status || 'submitted', input.feedback || '', now(),
  );
  return getDb().prepare(`SELECT * FROM learning_exercise_attempts WHERE id = ?`).get(id) as ExerciseAttempt;
}

export function listAttempts(ownerId: string, filters: { course_id?: string; exercise_id?: string } = {}): ExerciseAttempt[] {
  ensureLearningSchema();
  let sql = `SELECT * FROM learning_exercise_attempts WHERE owner_id = ?`;
  const args: unknown[] = [ownerId];
  if (filters.course_id) { sql += ` AND course_id = ?`; args.push(filters.course_id); }
  if (filters.exercise_id) { sql += ` AND exercise_id = ?`; args.push(filters.exercise_id); }
  sql += ` ORDER BY created_at DESC LIMIT 200`;
  return getDb().prepare(sql).all(...args) as ExerciseAttempt[];
}

export function createGenerationJob(ownerId: string, courseId: string): GenerationJob {
  ensureLearningSchema();
  const id = randomUUID();
  const time = now();
  getDb().prepare(
    `INSERT INTO learning_generation_jobs (id, owner_id, course_id, status, stage, error, created_at, updated_at)
     VALUES (?,?,?,?,?,?,?,?)`,
  ).run(id, ownerId, courseId, 'queued', 'queued', '', time, time);
  return getDb().prepare(`SELECT * FROM learning_generation_jobs WHERE id = ?`).get(id) as GenerationJob;
}

export function getLatestJob(ownerId: string, courseId: string): GenerationJob | undefined {
  ensureLearningSchema();
  return getDb().prepare(
    `SELECT * FROM learning_generation_jobs WHERE owner_id = ? AND course_id = ? ORDER BY created_at DESC LIMIT 1`,
  ).get(ownerId, courseId) as GenerationJob | undefined;
}

export function updateJob(
  ownerId: string,
  jobId: string,
  patch: { status?: GenerationJob['status']; stage?: string; error?: string; attempts?: number; batch_count?: number; details?: unknown },
): void {
  ensureLearningSchema();
  const job = getDb().prepare(`SELECT * FROM learning_generation_jobs WHERE id = ? AND owner_id = ?`)
    .get(jobId, ownerId) as GenerationJob | undefined;
  if (!job) return;
  getDb().prepare(
    `UPDATE learning_generation_jobs
        SET status = ?, stage = ?, error = ?, attempts = ?, batch_count = ?, details_json = ?, updated_at = ?
      WHERE id = ? AND owner_id = ?`,
  ).run(
    patch.status ?? job.status, patch.stage ?? job.stage, patch.error ?? job.error,
    patch.attempts ?? job.attempts, patch.batch_count ?? job.batch_count,
    patch.details === undefined ? job.details_json : JSON.stringify(patch.details),
    now(), jobId, ownerId,
  );
}

export function appendGenerationEvent(
  ownerId: string,
  courseId: string,
  jobId: string,
  kind: string,
  message: string,
  payload?: unknown,
): void {
  ensureLearningSchema();
  getDb().prepare(
    `INSERT INTO learning_generation_events
      (id, job_id, owner_id, course_id, kind, message, payload_json, created_at)
     VALUES (?,?,?,?,?,?,?,?)`,
  ).run(randomUUID(), jobId, ownerId, courseId, kind.slice(0, 40), message.slice(0, 1000), JSON.stringify(payload || {}), now());
  // 事件本身也是心跳；避免长模型调用期间被误判为 stale。
  getDb().prepare(`UPDATE learning_generation_jobs SET updated_at = ? WHERE id = ? AND owner_id = ?`).run(now(), jobId, ownerId);
}

export function listGenerationEvents(ownerId: string, courseId: string, limit = 120): GenerationEvent[] {
  ensureLearningSchema();
  return getDb().prepare(
    `SELECT * FROM learning_generation_events WHERE owner_id = ? AND course_id = ? ORDER BY created_at DESC LIMIT ?`,
  ).all(ownerId, courseId, Math.max(1, Math.min(limit, 300))) as GenerationEvent[];
}

export function getLatestDiagnosis(ownerId: string, courseId: string): LearningDiagnosis | undefined {
  ensureLearningSchema();
  return getDb().prepare(
    `SELECT * FROM learning_diagnoses WHERE owner_id = ? AND course_id = ? ORDER BY created_at DESC LIMIT 1`,
  ).get(ownerId, courseId) as LearningDiagnosis | undefined;
}

export function getCourseDetail(ownerId: string, courseId: string) {
  ensureLearningSchema();
  const course = getCourse(ownerId, courseId);
  if (!course) return undefined;
  const units = listUnits(ownerId, courseId);
  const lessons = listLessons(ownerId, courseId);
  const progress = new Map(listProgress(ownerId, courseId).map((row) => [row.lesson_id, row]));
  const exercises = listExercises(ownerId, { course_id: courseId });
  const chapters = units.filter((unit) => unit.level === 'chapter').map((unit) => ({
    ...unit,
    sections: units.filter((section) => section.level === 'section' && section.parent_id === unit.id).map((section) => {
      const lesson = lessons.find((item) => item.unit_id === section.id);
      const counts = {
        in_class: exercises.filter((item) => item.lesson_id === lesson?.id && item.phase === 'in_class').length,
        post_class: exercises.filter((item) => item.lesson_id === lesson?.id && item.phase === 'post_class').length,
      };
      return {
        ...section,
        lesson: lesson || null,
        progress: progress.get(lesson?.id || '') || null,
        exercise_counts: counts,
        knowledge_points: lesson
          ? listKnowledgePoints(ownerId, courseId, lesson.id).map((point) => ({
              ...point,
              concepts: JSON.parse(point.concepts_json || '[]'),
              formulas: JSON.parse(point.formulas_json || '[]'),
              methods: JSON.parse(point.methods_json || '[]'),
              prerequisites: JSON.parse(point.prerequisites_json || '[]'),
              source_block_ids: JSON.parse(point.source_block_ids_json || '[]'),
            }))
          : [],
      };
    }),
  }));
  return {
    course,
    materials: listMaterials(ownerId, courseId),
    source_blocks: listSourceBlocks(ownerId, courseId),
    chapters,
    stats: {
      chapters: chapters.length,
      sections: chapters.reduce((sum, chapter) => sum + chapter.sections.length, 0),
      lessons: lessons.length,
      exercises: exercises.length,
      completed: [...progress.values()].filter((row) => row.status === 'completed').length,
    },
  };
}

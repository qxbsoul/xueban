import { apiDelete, apiFetch, apiGet, apiPost } from './api';
import { loadApiSettings } from './settings-storage';

export type LearningSourceType = 'textbook' | 'curriculum' | 'description';
export type LearningCourseStatus = 'draft' | 'generating' | 'ready' | 'needs_review' | 'failed';
export type ExercisePhase = 'in_class' | 'post_class';

export const SOURCE_TYPE_LABEL: Record<string, string> = {
  textbook: '教材',
  curriculum: '课标/教学大纲',
  description: '自主描述',
};

export const COURSE_STATUS_LABEL: Record<string, string> = {
  draft: '待整理',
  generating: '正在生成',
  ready: '可学习',
  needs_review: '质量提示',
  failed: '生成失败',
};

export const QUALITY_LABEL: Record<string, string> = {
  complete: '完整解析',
  partial: '部分解析',
  needs_review: '需核对',
};

export interface LearningJob {
  id: string;
  status: 'queued' | 'running' | 'done' | 'failed';
  stage: string;
  error: string;
  created_at: number;
  updated_at: number;
}

export interface LearningCourse {
  id: string;
  owner_id: string;
  title: string;
  subject: string;
  grade: string;
  source_type: LearningSourceType;
  source_name: string;
  status: LearningCourseStatus;
  parse_quality?: 'complete' | 'partial' | 'needs_review';
  quality_json?: string;
  stage: string;
  progress: number;
  error: string;
  created_at: number;
  updated_at: number;
  job?: LearningJob | null;
}

export interface LearningMaterial {
  id: string;
  filename: string;
  mime: string;
  size: number;
  extraction_status: string;
  sha256?: string;
  format?: string;
  page_count?: number | null;
  has_original?: number;
  created_at: number;
}

export interface LearningLessonSummary {
  id: string;
  title: string;
  objective: string;
  content?: string;
  teaching_units_json?: string;
  status: 'draft' | 'ready';
}

export interface LearningSourceBlock {
  id: string;
  material_id: string;
  block_type: 'heading' | 'paragraph' | 'formula' | 'table' | 'image' | 'list';
  ordinal: number;
  text: string;
  markdown: string;
  page: number | null;
  bbox_json?: string;
  table_status?: 'structure_validated' | 'content_verified' | 'needs_review' | 'failed';
  line_start: number;
  line_end: number;
  status: 'parsed' | 'needs_review';
  extraction_method?: string;
  raw_text?: string;
  confidence?: number;
  repair_history_json?: string;
}

export interface LearningKnowledgePoint {
  id: string;
  unit_id: string;
  lesson_id: string;
  ordinal: number;
  title: string;
  explanation: string;
  concepts: string[];
  formulas: string[];
  methods: string[];
  prerequisites: string[];
  source_block_ids: string[];
  origin: 'textbook' | 'agent_inferred';
  status: 'parsed' | 'needs_review';
  confidence: number;
}

export interface LearningSection {
  id: string;
  title: string;
  summary: string;
  status?: 'parsed' | 'needs_review';
  lesson: LearningLessonSummary | null;
  progress: { status: 'not_started' | 'reading' | 'completed'; completed_at: number | null } | null;
  exercise_counts: { in_class: number; post_class: number };
  knowledge_points: LearningKnowledgePoint[];
}

export interface LearningChapter {
  id: string;
  title: string;
  summary: string;
  sections: LearningSection[];
}

export interface LearningGenerationEvent {
  id: string;
  job_id: string;
  owner_id: string;
  course_id: string;
  kind: string;
  message: string;
  payload_json: string;
  created_at: number;
}

export interface LearningCourseDetail {
  course: LearningCourse;
  materials: LearningMaterial[];
  source_blocks: LearningSourceBlock[];
  chapters: LearningChapter[];
  stats: {
    chapters: number;
    sections: number;
    lessons: number;
    exercises: number;
    completed: number;
  };
  job: LearningJob | null;
  events?: LearningGenerationEvent[];
}

export interface LearningLessonDetail {
  lesson: {
    id: string;
    course_id: string;
    unit_id: string;
    title: string;
    objective: string;
    content: string;
    key_points_json: string;
    teaching_units_json?: string;
    source_refs_json: string;
    status: 'draft' | 'ready';
    version: number;
  };
  unit: { id: string; title: string; level: string } | null;
  course: LearningCourse;
  knowledge_points: LearningKnowledgePoint[];
  source_blocks: LearningSourceBlock[];
}

export interface LearningExercise {
  id: string;
  course_id: string;
  lesson_id: string;
  unit_id: string;
  phase: ExercisePhase;
  ordinal: number;
  stem: string;
  explanation: string;
  difficulty: string;
  source_type: 'textbook' | 'generated';
  validation_status: 'placeholder' | 'unverified' | 'checked';
  source_ref_json: string;
  created_at: number;
  locked: boolean;
  answer?: undefined;
}

export interface LearningAttempt {
  id: string;
  exercise_id: string;
  course_id: string;
  lesson_id: string;
  answer: string;
  status: string;
  feedback: string;
  created_at: number;
}

export interface LearningModelSelection {
  provider: string;
  model?: string;
  base_url?: string;
  customKey?: string;
  custom?: boolean;
}

export interface LearningDiagnosis {
  available: boolean;
  status: 'not_started' | 'queued' | 'ready' | 'failed';
  summary: string;
  mastery: Record<string, unknown> | null;
  message: string;
  latest: {
    id: string;
    status: string;
    summary: string;
    created_at: number;
    updated_at: number;
  } | null;
}

export const getLearningDiagnosis = (courseId: string) =>
  apiGet<LearningDiagnosis>(`/api/learning/courses/${encodeURIComponent(courseId)}/diagnosis`);

export const listLearningCourses = () => apiGet<LearningCourse[]>('/api/learning/courses');
export const getLearningCourse = (id: string) => apiGet<LearningCourseDetail>(`/api/learning/courses/${encodeURIComponent(id)}`);
export const deleteLearningCourse = (id: string) => apiDelete<{ ok: boolean }>(`/api/learning/courses/${encodeURIComponent(id)}`);
export async function retryLearningCourse(id: string, model?: LearningModelSelection): Promise<{ course: LearningCourse; job: LearningJob }> {
  const settings = loadApiSettings();
  const selected = model || { provider: settings.provider, model: settings.model, base_url: settings.baseUrl };
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (selected.customKey) headers['X-Custom-Api-Key'] = selected.customKey;
  const response = await apiFetch(`/api/learning/courses/${encodeURIComponent(id)}/retry`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      provider: selected.provider,
      model: selected.model,
      base_url: selected.base_url,
      custom: Boolean(selected.customKey),
    }),
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!response.ok) {
    const error = body as { error?: { message?: string } } | null;
    throw new Error(error?.error?.message || `重试失败（HTTP ${response.status}）`);
  }
  return body as { course: LearningCourse; job: LearningJob };
}

export async function getLearningOriginalPdf(courseId: string): Promise<Blob> {
  const response = await apiFetch(`/api/learning/courses/${encodeURIComponent(courseId)}/original`, {}, 60_000);
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { error?: { message?: string } } | null;
    throw new Error(body?.error?.message || '原始教材加载失败');
  }
  return response.blob();
}

export const getLearningLesson = (courseId: string, lessonId: string) =>
  apiGet<LearningLessonDetail>(`/api/learning/courses/${encodeURIComponent(courseId)}/lessons/${encodeURIComponent(lessonId)}`);

export const completeLearningLesson = (lessonId: string) =>
  apiPost<{ message: string; unlocked: { post_class: boolean } }>(`/api/learning/lessons/${encodeURIComponent(lessonId)}/complete`, {});

export const listLearningExercises = (params: {
  course_id?: string;
  lesson_id?: string;
  phase?: ExercisePhase;
}) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value) query.set(key, value);
  });
  return apiGet<LearningExercise[]>(`/api/learning/exercises?${query.toString()}`);
};

export async function submitLearningAttempt(exerciseId: string, answer: string): Promise<{
  attempt: LearningAttempt;
  grading: { available: boolean; status: string; message: string };
}> {
  const response = await apiFetch(`/api/learning/exercises/${encodeURIComponent(exerciseId)}/attempts`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer }),
  });
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!response.ok) {
    const error = body as { error?: { message?: string } } | null;
    throw new Error(error?.error?.message || `提交失败（HTTP ${response.status}）`);
  }
  return body as { attempt: LearningAttempt; grading: { available: boolean; status: string; message: string } };
}

export async function createLearningCourse(input: {
  title: string;
  subject: string;
  grade: string;
  source_type: LearningSourceType;
  source_text: string;
  file?: File | null;
  model?: LearningModelSelection;
}): Promise<{ course: LearningCourse; job: LearningJob }> {
  const form = new FormData();
  form.set('title', input.title);
  form.set('subject', input.subject);
  form.set('grade', input.grade);
  form.set('source_type', input.source_type);
  form.set('source_text', input.source_text);
  const settings = loadApiSettings();
  const selected = input.model || { provider: settings.provider, model: settings.model, base_url: settings.baseUrl };
  form.set('provider', selected.provider);
  form.set('model', selected.model || '');
  form.set('base_url', selected.base_url || '');
  form.set('custom', selected.customKey ? 'true' : 'false');
  if (input.file) form.set('file', input.file);
  const headers: Record<string, string> = {};
  if (selected.customKey) headers['X-Custom-Api-Key'] = selected.customKey;
  const response = await apiFetch('/api/learning/courses', { method: 'POST', body: form, headers }, 120_000);
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!response.ok) {
    const error = body as { error?: { message?: string } } | null;
    throw new Error(error?.error?.message || `创建课程失败（HTTP ${response.status}）`);
  }
  return body as { course: LearningCourse; job: LearningJob };
}

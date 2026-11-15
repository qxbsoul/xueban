import { apiDelete, apiGet, apiPost } from './api';

export type MemoryCategory =
  | 'profile'
  | 'learning_goal'
  | 'preference'
  | 'learning_style'
  | 'weakness'
  | 'misconception'
  | 'learning_context'
  | 'feedback';

export const MEMORY_CATEGORY_LABEL: Record<string, string> = {
  profile: '学习者背景',
  learning_goal: '学习目标',
  preference: '学习偏好',
  learning_style: '学习方式',
  weakness: '知识薄弱点',
  misconception: '常见理解误区',
  learning_context: '学习背景',
  feedback: '学习反馈',
};

export interface MemoryItem {
  id: string;
  owner_id: string;
  category: MemoryCategory;
  subject: string;
  topic: string;
  memory_key: string;
  title: string;
  content: string;
  confidence: number;
  explicitness: 'explicit' | 'inferred' | string;
  status: 'active' | 'archived' | 'superseded' | 'deleted' | 'blocked';
  version: number;
  evidence_count: number;
  source_conversation_id: string;
  source_message_id: string;
  created_at: number;
  updated_at: number;
  last_confirmed_at: number;
  expires_at: number | null;
}

export interface MemoryEvidence {
  id: string;
  message_id: string;
  quote: string;
  conversation_id: string;
  created_at: number;
}

export interface MemoryVersion {
  version: number;
  changed_by: string;
  reason: string;
  created_at: number;
  snapshot: MemoryItem | null;
}

export interface MemoryDetail {
  memory: MemoryItem;
  evidence: MemoryEvidence[];
  versions: MemoryVersion[];
}

export interface CandidateItem {
  id: string;
  owner_id: string;
  category: MemoryCategory;
  subject: string;
  topic: string;
  memory_key: string;
  title: string;
  content: string;
  confidence: number;
  explicitness: string;
  evidence: Array<{ message_id: string; quote: string; conversation_id?: string; created_at?: number }>;
  suggested_action: string;
  target_memory_id: string;
  status: 'pending' | 'conflict' | 'accepted' | 'rejected';
  reason: string;
  created_at: number;
  reviewed_at: number | null;
}

export interface MemoryJob {
  id: string;
  owner_id: string;
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  subject: string;
  provider: string;
  model: string;
  status: 'pending' | 'running' | 'waiting_credentials' | 'done' | 'failed' | 'superseded';
  turn_revision: number;
  attempts: number;
  last_error: string;
  created_at: number;
  updated_at: number;
}

export interface MemoryOutboxRow {
  id: string;
  owner_id: string;
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  subject: string;
  provider: string;
  status: 'awaiting_assistant' | 'done' | 'abandoned';
  run_state: string;
  abandoned_reason: string;
  abandoned_at: number | null;
  restored_at: number | null;
  restore_count: number;
  created_at: number;
  updated_at: number;
}

export interface MemoryListParams {
  subject?: string;
  category?: string;
  status?: string;
  q?: string;
  explicitness?: string;
  limit?: number;
  offset?: number;
  cursor?: string;
  sort?: 'updated' | 'created';
}

export interface MemoryStats {
  active: number;
  archived: number;
  blocked: number;
  by_category: Record<string, number>;
  latest_update: number;
}

export const listMemories = (params: MemoryListParams = {}) => {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value));
  });
  return apiGet<MemoryItem[]>(`/api/student-memory/memories?${query.toString()}`);
};

export const getMemoryStats = () => apiGet<MemoryStats>('/api/student-memory/memories/stats');
export const getMemoryByKey = (key: string) => apiGet<MemoryItem>(`/api/student-memory/memories/by-key/${encodeURIComponent(key)}`);
export const getMemory = (id: string) => apiGet<MemoryDetail>(`/api/student-memory/memories/${encodeURIComponent(id)}`);
export const correctMemory = (id: string, content: string, reason?: string) =>
  apiPost<MemoryItem>(`/api/student-memory/memories/${encodeURIComponent(id)}/correct`, { content, reason });
export const archiveMemory = (id: string) =>
  apiPost<void>(`/api/student-memory/memories/${encodeURIComponent(id)}/archive`, {});
export const deleteMemory = (id: string) => apiDelete<void>(`/api/student-memory/memories/${encodeURIComponent(id)}`);
export const blockMemory = (id: string) =>
  apiPost<void>(`/api/student-memory/memories/${encodeURIComponent(id)}/block`, {});

export const listCandidates = (status: 'pending' | 'conflict') =>
  apiGet<CandidateItem[]>(`/api/student-memory/candidates?status=${status}`);
export const acceptCandidate = (id: string) =>
  apiPost<{ action: string; memory?: MemoryItem }>(`/api/student-memory/candidates/${encodeURIComponent(id)}/accept`, {});
export const rejectCandidate = (id: string, reason?: string) =>
  apiPost<void>(`/api/student-memory/candidates/${encodeURIComponent(id)}/reject`, { reason });

export const listJobs = () => apiGet<MemoryJob[]>('/api/student-memory/jobs');
export const listOutbox = () => apiGet<MemoryOutboxRow[]>('/api/student-memory/outbox');
export const restoreOutbox = (id: string) =>
  apiPost<void>(`/api/student-memory/outbox/${encodeURIComponent(id)}/restore`, {});

export const JOB_STATUS_LABEL: Record<string, string> = {
  pending: '等待整理',
  running: '正在整理',
  waiting_credentials: '等待有效 API 配置',
  done: '已完成',
  failed: '整理失败',
  superseded: '已被新版本取代',
};

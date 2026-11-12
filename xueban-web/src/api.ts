/* 智慧学伴 Gateway 唯一网络入口。
 * 前端只访问同源 /api/**；Vite 负责转发到 Node:8002，禁止直连 Python:8000。
 * 后续页面可复用 apiGet/apiPost/apiPatch/apiDelete，无需再写裸 fetch。
 */
import { embeddingRequestHeaders } from './embedding-storage';
import { toolRequestHeaders } from './tool-storage';
import type { ChatActivity, ChatApproval, ChatDagNode } from './types';
import { profileContext } from './student-profile';

export const BYOK_PROVIDERS = [
  'anthropic', 'openai', 'deepseek', 'zhipu', 'google', 'qwen',
  'moonshot', 'mistral', 'groq', 'openrouter', 'ollama', 'lmstudio', 'azure',
] as const;

export type ByokProvider = (typeof BYOK_PROVIDERS)[number];

export interface ApiErrorPayload {
  code?: string;
  message?: string;
  trace_id?: string;
  [key: string]: unknown;
}

export class ApiError extends Error {
  status: number;
  code: string;
  traceId?: string;
  payload: unknown;

  constructor(status: number, code: string, message: string, payload: unknown, traceId?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.traceId = traceId;
    this.payload = payload;
  }
}

export interface CompanyDto {
  id: string;
  name: string;
  industry?: string;
  mission?: string;
  owner_name?: string;
  status?: string;
}

export interface AuthUserDto {
  id: string;
  email?: string | null;
  display_name: string;
  auth_type: 'password' | 'guest';
  /** 学段年级档案（后端用户表），未设置为 null。 */
  profile?: { stage: 'primary' | 'junior' | 'senior' | 'college'; grade: number; setAt?: string | null } | null;
}

export interface AuthSessionDto {
  token: string;
  user: AuthUserDto;
  company: CompanyDto;
}

export interface ModelDto {
  id: string;
  label: string;
  vendor: string;
  tier: string;
}

export interface OrgMemberDto {
  id: string;
  name: string;
  title: string;
  avatar?: string;
  status: string;
  model?: string;
  skills?: string[];
}

export interface OrgDepartmentDto {
  id: string;
  name: string;
  charter?: string;
  head?: { id: string; name: string; title: string } | null;
  members: OrgMemberDto[];
}

export interface RosterEmployeeDto {
  id: string;
  company_id: string;
  department_id: string | null;
  manager_id?: string | null;
  name: string;
  role: string;
  title: string;
  avatar?: string;
  system_prompt?: string;
  status: string;
  employment?: string;
  model_name?: string;
  skills?: string[];
  tools?: string[];
}

export interface EmployeePatch {
  name?: string;
  title?: string;
  department_id?: string | null;
  manager_id?: string | null;
  system_prompt?: string;
  avatar?: string;
  status?: string;
  model_name?: string;
  skills?: string[];
  tools?: string[];
}

export interface EmployeeConfigVersionDto {
  id: string;
  employee_id: string;
  company_id: string;
  version: number;
  source: 'hire' | 'patch' | 'org_publish' | 'restore' | 'legacy_snapshot' | string;
  restored_from_version?: number | null;
  created_at: string;
  config: {
    name?: string;
    role?: string;
    title?: string;
    department_id?: string | null;
    manager_id?: string | null;
    system_prompt?: string;
    model_name?: string;
    model_provider?: string;
    skills?: string[];
    tools?: string[];
    status?: string;
    [key: string]: unknown;
  };
}

export interface TaskExecutionDto {
  id: string;
  task_id: string;
  company_id: string;
  employee_id: string;
  config_version_id: string;
  config_version: number;
  attempt: number;
  input_summary?: string | null;
  output_summary?: string | null;
  model?: string | null;
  started_at: string;
  ended_at?: string | null;
  duration_ms?: number | null;
  error?: string | null;
  status: string;
}

export interface XuebanOrgDraft {
  departments: Array<{ id: string; name: string; charter: string }>;
  employees: Array<{
    id: string;
    name: string;
    title: string;
    role: string;
    system_prompt: string;
    skills: string[];
    tools: string[];
    department_id: string;
    manager_id: string | null;
    model_name: string;
  }>;
}

export interface ProjectDto {
  id: string;
  company_id: string;
  name: string;
  goal?: string;
  client_name?: string;
  status: string;
  order_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ProjectTaskDto {
  id: string;
  title: string;
  brief?: string;
  priority?: string;
  status: string;
  output?: { text?: string; summary?: string; mode?: string; error?: string } | null;
  assignee?: { id: string; name: string; title?: string; avatar?: string } | null;
}

export interface ApprovalDto {
  id: string;
  company_id?: string;
  project_id?: string;
  task_id?: string;
  kind?: string;
  title: string;
  status: 'pending' | 'approved' | 'rejected';
  risk?: string;
  payload?: { summary?: string; by?: string; cost_cents?: number };
  requested_by?: string;
  decided_by?: string;
  decided_at?: string;
  note?: string;
  created_at?: string;
}

export interface ProjectDetailDto {
  project: ProjectDto;
  tasks: ProjectTaskDto[];
  approvals: ApprovalDto[];
}

export interface ScheduleRunDto {
  run_id: string;
  triggered_at: string;
  status: string;
  error?: string;
  conversation_id?: string;
  output_summary?: string;
}

export interface SchedulePatch {
  enabled?: boolean;
  cron_expression?: string | null;
  start_at?: string | null;
  agent_id?: string | null;
  task_description?: string;
  duration_min?: number;
}

export interface ScheduleDto {
  schedule_id: string;
  group_id: string;
  cron_expression: string | null;
  start_at: string | null;
  agent_id: string | null;
  task_description: string;
  duration_min: number;
  next_run_time: string | null;
  completed: boolean;
  enabled: boolean;
  runs: ScheduleRunDto[];
}

export interface KnowledgeSourceDto {
  source_id: string;
  filename: string;
  extension?: string;
  size_bytes?: number;
  ingest_status?: string;
  chunk_count?: number;
  error_message?: string;
  imported_at?: string;
}

export interface KnowledgeHitDto {
  text: string;
  source?: string;
  source_name?: string;
  score?: number;
}

export interface KnowledgePackDto {
  pack_id: string;
  name: string;
  description?: string;
  status: string;
  sources?: KnowledgeSourceDto[];
  stats?: { sources?: number; chunks?: number; bytes?: number };
  created_at?: string;
  updated_at?: string;
}

export interface DashboardOverviewDto {
  currency: 'CNY';
  pricing: { source: string; input_per_million: number; cached_input_per_million: number; output_per_million: number } | null;
  usage: {
    calls: number;
    input_tokens: number;
    cached_input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cost_est_cny: number;
    deltas: { calls: number | null; total_tokens: number | null; cost_est_cny: number | null };
  };
}

export interface DashboardSeriesDto {
  series: Array<{ day: number; calls: number; input_tokens: number; output_tokens: number; tokens: number; cost_est_cny: number }>;
}

export interface DashboardBreakdownDto {
  items: Array<{ key: string; calls: number; input_tokens: number; cached_input_tokens: number; output_tokens: number; total_tokens: number; cost_est_cny: number }>;
}

function apiBase(): string {
  const fromQuery = new URLSearchParams(window.location.search).get('api');
  return (fromQuery ?? '').replace(/\/$/, '');
}

function providerHeader(provider: string): string {
  return `X-${provider.charAt(0).toUpperCase()}${provider.slice(1)}-Key`;
}

/** 当前选中的自定义模型条目（多个自定义模型并存，Key 跟着条目走）。 */
function activeCustomEntry(): { model?: string; baseUrl?: string; key?: string } | null {
  try {
    const list = JSON.parse(localStorage.getItem('sf_custom_models') ?? '[]');
    if (!Array.isArray(list) || list.length === 0) return null;
    const id = localStorage.getItem('sf_custom_active');
    const entry = list.find((item: { id?: string }) => item?.id === id) ?? list[0];
    return entry && typeof entry === 'object' ? entry as { model?: string; baseUrl?: string; key?: string } : null;
  } catch {
    return null;
  }
}

export function setByokKey(provider: ByokProvider, key: string): void {
  if (key) localStorage.setItem(`sf_${provider}_key`, key);
  else localStorage.removeItem(`sf_${provider}_key`);
}

export function setDefaultProvider(provider: ByokProvider): void {
  localStorage.setItem('sf_default_provider', provider);
}

const AUTH_TOKEN_KEY = 'sf_xueban_session';
export const getAuthToken = () => localStorage.getItem(AUTH_TOKEN_KEY) ?? '';
export const setAuthToken = (token: string) => token ? localStorage.setItem(AUTH_TOKEN_KEY, token) : localStorage.removeItem(AUTH_TOKEN_KEY);

/** 需要 BYOK 密钥的接口白名单：其余路径一律不附带 provider key。 */
const BYOK_PATHS = [
  '/api/xueban-chat/',
  '/api/xueban-knowledge/',
  '/api/xueban-task-execution',
  '/api/groups-chat',
  '/api/run-sessions',
  '/api/run-suggestions',
  '/api/teams',
];
function allowsByok(path: string): boolean {
  return BYOK_PATHS.some(prefix => path.startsWith(prefix));
}

export async function apiFetch(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<Response> {
  if (!path.startsWith('/api/')) throw new Error(`Gateway 路径必须以 /api/ 开头：${path}`);
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const token = getAuthToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  // BYOK key 只发给真正会调用模型的接口；认证/配置/业务接口不携带密钥，
  // 避免密钥被日志、代理或无关业务误采集。
  const provider = localStorage.getItem('sf_default_provider');
  if (provider && BYOK_PROVIDERS.includes(provider as ByokProvider) && allowsByok(path)) {
    const key = localStorage.getItem(`sf_${provider}_key`);
    if (key) headers.set(providerHeader(provider), key);
  }
  if (path.startsWith('/api/xueban-chat/')) {
    const deepseekKey = localStorage.getItem('sf_deepseek_key')?.trim();
    if (deepseekKey) headers.set('X-DeepSeek-Key', deepseekKey);
  }
  // 自定义 OpenAI 兼容密钥只发往真实聊天接口：取当前选中的自定义模型条目。
  if (path.startsWith('/api/xueban-chat/') && localStorage.getItem('sf_model_preset') === 'custom') {
    const customKey = activeCustomEntry()?.key;
    if (customKey) headers.set('X-Custom-Api-Key', customKey);
  }
  if (path.startsWith('/api/xueban-knowledge/') || path.startsWith('/api/xueban-chat/')) {
    const embeddingHeaders = embeddingRequestHeaders();
    for (const [key, value] of Object.entries(embeddingHeaders)) headers.set(key, value);
  }
  if (path.startsWith('/api/xueban-chat/') || path.startsWith('/api/schedules')) {
    for (const [key, value] of Object.entries(toolRequestHeaders())) headers.set(key, value);
  }

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const abort = () => controller.abort();
  init.signal?.addEventListener('abort', abort, { once: true });
  try {
    return await fetch(apiBase() + path, { ...init, headers, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
    init.signal?.removeEventListener('abort', abort);
  }
}

function errorMessage(body: unknown, status: number): { code: string; message: string; traceId?: string } {
  let candidate: unknown = body;
  if (body && typeof body === 'object' && 'detail' in body) candidate = (body as { detail: unknown }).detail;
  if (candidate && typeof candidate === 'object' && 'error' in candidate) {
    const container = candidate as Record<string, unknown>;
    const nested = container.error;
    if (nested && typeof nested === 'object') candidate = nested;
    else if (typeof nested === 'string') {
      return {
        code: nested,
        message: typeof container.message === 'string'
          ? container.message
          : typeof container.detail === 'string'
            ? container.detail
            : nested,
      };
    }
  }
  if (Array.isArray(candidate)) return { code: 'VALIDATION_ERROR', message: JSON.stringify(candidate) };
  if (candidate && typeof candidate === 'object') {
    const value = candidate as ApiErrorPayload;
    return {
      code: typeof value.code === 'string' ? value.code : `HTTP_${status}`,
      message: typeof value.message === 'string' ? value.message : `请求失败（HTTP ${status}）`,
      traceId: typeof value.trace_id === 'string' ? value.trace_id : undefined,
    };
  }
  if (typeof candidate === 'string') return { code: `HTTP_${status}`, message: candidate };
  return { code: `HTTP_${status}`, message: `请求失败（HTTP ${status}）` };
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<T> {
  const response = await apiFetch(path, init, timeoutMs);
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  if (!response.ok) {
    const error = errorMessage(body, response.status);
    throw new ApiError(response.status, error.code, error.message, body, error.traceId);
  }
  return body as T;
}

export const apiGet = <T>(path: string) => request<T>(path);
export const apiPost = <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
export const apiPatch = <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) });
export const apiDelete = <T>(path: string) => request<T>(path, { method: 'DELETE' });

// XUEBAN 身份与教研组；密码只用于本次登录／注册请求，不写入浏览器存储。
export const loginXueban = (email: string, password: string) => apiPost<AuthSessionDto>('/api/xueban/auth/login', { email, password });
export const registerXueban = (payload: { email: string; password: string; display_name: string; company_name: string }) => apiPost<AuthSessionDto>('/api/xueban/auth/register', payload);
export const guestXueban = () => apiPost<AuthSessionDto>('/api/xueban/auth/guest', {});
export const saveXuebanProfile = (stage: string, grade: number) =>
  request<{ profile: AuthUserDto['profile'] }>('/api/xueban/auth/profile', {
    method: 'PUT',
    body: JSON.stringify({ stage, grade }),
  });

export const getXuebanMe = () => apiGet<{ user: AuthUserDto; company: CompanyDto }>('/api/xueban/auth/me');
export const logoutXueban = () => apiPost<{ ok: boolean }>('/api/xueban/auth/logout', {});

// 初始化与元数据。教研组列表同时充当全链路健康检查：网关与 Python 任一离线都会失败。
export const seedXueban = () => apiPost<{ company: CompanyDto }>('/api/xueban/seed');
export const getCompanies = () => apiGet<{ items: CompanyDto[] }>('/api/xueban/companies').then(r => r.items);
export const getModels = () => apiGet<{ items: ModelDto[] }>('/api/xueban/models').then(r => r.items);

// 教研团队。
export const getOrg = (companyId: string) => apiGet<{ departments: OrgDepartmentDto[]; headcount: number }>(`/api/xueban/company/${companyId}/org`);
export const getRoster = (companyId: string) => apiGet<{ items: RosterEmployeeDto[] }>(`/api/xueban/company/${companyId}/roster`).then(r => r.items);
export const createDepartment = (companyId: string, name: string, charter = '') => apiPost<{ department: OrgDepartmentDto }>(`/api/xueban/company/${companyId}/departments`, { name, charter });
export const updateDepartment = (companyId: string, departmentId: string, payload: { name?: string; charter?: string }) => apiPatch<{ department: OrgDepartmentDto }>(`/api/xueban/company/${companyId}/departments/${encodeURIComponent(departmentId)}`, payload);
export const deleteDepartment = (companyId: string, departmentId: string) => apiDelete<void>(`/api/xueban/company/${companyId}/departments/${encodeURIComponent(departmentId)}`);
export const hireEmployee = (companyId: string, payload: Record<string, unknown>) => apiPost<{ employee: RosterEmployeeDto }>(`/api/xueban/company/${companyId}/employees`, payload);
export const updateEmployee = (companyId: string, employeeId: string, payload: EmployeePatch) => apiPatch<{ employee: RosterEmployeeDto }>(`/api/xueban/company/${companyId}/employees/${encodeURIComponent(employeeId)}`, payload);
export const getEmployeeConfigVersions = (companyId: string, employeeId: string) => apiGet<{ items: EmployeeConfigVersionDto[] }>(`/api/xueban/company/${companyId}/employees/${encodeURIComponent(employeeId)}/config-versions`).then(r => r.items);
export const restoreEmployeeConfigVersion = (companyId: string, employeeId: string, version: number) => apiPost<{ employee: RosterEmployeeDto; version: EmployeeConfigVersionDto }>(`/api/xueban/company/${companyId}/employees/${encodeURIComponent(employeeId)}/config-versions/${version}/restore`, {});
export const fireEmployee = (companyId: string, employeeId: string, reassignTo?: string) => apiPost<{ ok: boolean; reassigned?: number }>(`/api/xueban/company/${companyId}/employees/${encodeURIComponent(employeeId)}/fire`, { reassign_to: reassignTo || undefined });
export const generateOrgDraft = (companyId: string, businessDescription: string) => request<{ draft: XuebanOrgDraft; model: string; mode: 'live' }>('/api/xueban-org/draft', {
  method: 'POST',
  headers: { 'X-DeepSeek-Key': localStorage.getItem('sf_deepseek_key')?.trim() || '' },
  body: JSON.stringify({ company_id: companyId, business_description: businessDescription }),
}, 100_000);
export const publishOrgDraft = (companyId: string, draft: XuebanOrgDraft) => apiPost<{
  ok: true;
  created: { departments: number; employees: number };
}>('/api/xueban-org/publish', { company_id: companyId, draft });

// 内部项目：老师派工走这里。
export const getProjects = (companyId: string) => apiGet<{ items: ProjectDto[] }>(`/api/xueban/company/${companyId}/projects?limit=50`).then(r => r.items);
export const createProject = (companyId: string, goal: string, options: { name?: string; client_name?: string } = {}) => apiPost<{ project: ProjectDto; tasks: ProjectTaskDto[] }>(`/api/xueban/company/${companyId}/projects`, { name: options.name || `内部需求｜${goal.slice(0, 24)}`, goal, client_name: options.client_name ?? '内部', auto_plan: true });
export const getProject = (projectId: string) => apiGet<ProjectDetailDto>(`/api/xueban/projects/${projectId}`);
export const updateProjectTask = (taskId: string, payload: { title?: string; brief?: string; assignee?: string; priority?: string }) => apiPatch<{ task: ProjectTaskDto }>(`/api/xueban/tasks/${encodeURIComponent(taskId)}`, payload);
export const getTaskExecutions = (taskId: string) => apiGet<{ items: TaskExecutionDto[] }>(`/api/xueban/tasks/${encodeURIComponent(taskId)}/executions`).then(r => r.items);
export const runProject = (projectId: string, retry = false) => apiPost<{ project: ProjectDto; stopped: string; pending_approvals: ApprovalDto[]; tasks: ProjectTaskDto[] }>(`/api/xueban/projects/${projectId}/run`, { retry });
export const deleteProject = (projectId: string) => apiDelete<void>(`/api/xueban/projects/${projectId}`);

// 教研项目审批。
export const getApprovals = (companyId: string, status = 'pending') => apiGet<{ items: ApprovalDto[] }>(`/api/xueban/company/${companyId}/approvals?status=${encodeURIComponent(status)}`).then(r => r.items);
export const decideApproval = (approvalId: string, decision: 'approved' | 'rejected', note = '') => apiPost<{ approval: ApprovalDto }>(`/api/xueban/approvals/${approvalId}/decide`, { decision, note, decided_by: '老师' });
// 学习计划。
export const getSchedules = () => apiGet<{ data: ScheduleDto[] }>('/api/schedules').then(r => r.data);
export const createSchedule = (payload: Record<string, unknown>) => apiPost<{ data: ScheduleDto }>('/api/schedules', payload);
export const patchSchedule = (scheduleId: string, payload: SchedulePatch) => apiPatch<{ data: ScheduleDto }>(`/api/schedules/${encodeURIComponent(scheduleId)}`, payload);
export const deleteSchedule = (scheduleId: string) => apiDelete<void>(`/api/schedules/${encodeURIComponent(scheduleId)}`);
// 立即执行要等教学主管 + 教研员的完整回合，30s 默认超时会把请求 abort 掉（表现为
// "signal is aborted without reason"），这里单独放宽到 10 分钟。
export const runSchedule = (scheduleId: string) =>
  request<{ data: ScheduleRunDto }>(`/api/schedules/${encodeURIComponent(scheduleId)}/run`, { method: 'POST', body: '{}' }, 600_000).then(r => r.data);
export const getScheduleRuns = (scheduleId: string) => apiGet<{ data: ScheduleRunDto[] }>(`/api/schedules/${encodeURIComponent(scheduleId)}/runs`).then(r => r.data);

/** 流式执行：先建对话，随后由前端在这条对话里用聊天流跑，结束后写回运行记录。 */
export const startScheduleRun = (scheduleId: string) =>
  apiPost<{ data: { conversation_id: string; task_description: string } }>(`/api/schedules/${encodeURIComponent(scheduleId)}/start`, {}).then(r => r.data);
export const finishScheduleRun = (scheduleId: string, conversationId: string) =>
  request<{ data: ScheduleRunDto }>(`/api/schedules/${encodeURIComponent(scheduleId)}/finish`,
    { method: 'POST', body: JSON.stringify({ conversation_id: conversationId }) }, 60_000).then(r => r.data);

// 教研组知识包：Node 校验归属，Python 负责文件解析与检索。
export const getKnowledgePacks = () => apiGet<{ data: { packs: KnowledgePackDto[] } }>('/api/xueban-knowledge/packs').then(r => r.data.packs);
export const createKnowledgePack = (name: string, description = '') => apiPost<{ data: KnowledgePackDto }>('/api/xueban-knowledge/packs', { name, description }).then(r => r.data);
export const updateKnowledgePack = (packId: string, payload: { name?: string; description?: string }) => apiPatch<{ data: KnowledgePackDto }>(`/api/xueban-knowledge/packs/${encodeURIComponent(packId)}`, payload).then(r => r.data);
export const deleteKnowledgePack = (packId: string) => apiDelete<{ data: { deleted: boolean; pack_id: string } }>(`/api/xueban-knowledge/packs/${encodeURIComponent(packId)}`);
export const deleteKnowledgeSource = (packId: string, sourceId: string) => apiDelete<{ data: { deleted: boolean; source_id: string } }>(`/api/xueban-knowledge/packs/${encodeURIComponent(packId)}/sources/${encodeURIComponent(sourceId)}`);
export const uploadKnowledgeFile = (packId: string, file: File) => {
  const body = new FormData();
  body.append('upload', file, file.name);
  body.append('metadata', JSON.stringify({ original_name: file.name }));
  return request<{ data: KnowledgeSourceDto }>(`/api/xueban-knowledge/packs/${encodeURIComponent(packId)}/files`, { method: 'POST', body }, 120_000).then(r => r.data);
};
export const queryKnowledgePack = (packId: string, query: string) => apiPost<{ data: { hits: KnowledgeHitDto[] }; meta?: { rag_backend?: string } }>(`/api/xueban-knowledge/packs/${encodeURIComponent(packId)}/query`, { query, top_k: 5 });
export const testEmbeddingConnection = () => request<{ data: { model: string; dimensions: number } }>('/api/xueban-knowledge/embedding/test', {
  method: 'POST', body: '{}',
}, 60_000);
/** 用当前模型把一张图片整理成可检索的课程资料文本（对话附件入库用）。 */
export const extractImageKnowledge = (payload: { data_url: string; hint?: string; provider?: string; model?: string }) =>
  request<{ data: { text: string } }>('/api/xueban-knowledge/extract', { method: 'POST', body: JSON.stringify(payload) }, 120_000).then(r => r.data.text);

// 当前教研组真实模型用量；成本为人民币官方刊例价估算。
export const getDashboardOverview = () => apiGet<DashboardOverviewDto>('/api/dashboard/overview?days=30');
export const getDashboardSeries = () => apiGet<DashboardSeriesDto>('/api/dashboard/timeseries?days=7&metric=tokens');
export const getDashboardBreakdown = () => apiGet<DashboardBreakdownDto>('/api/dashboard/breakdown?days=30&by=model');

// ── 套餐与积分：10,000 Token = 1 积分，按月发放 ─────────────────────────────
export interface BillingSummaryDto {
  plan: 'basic' | 'studio';
  plan_label: string;
  price_cents: number;
  credits_total: number;
  credits_used: number;
  credits_remaining: number;
  percent_used: number;
  tokens_used: number;
  tokens_per_credit: number;
  period_start: string;
  period_end: string;
  features: string[];
}

export interface PlanDto {
  key: 'basic' | 'studio' | 'custom';
  label: string;
  price_cents: number;
  credits?: number;
  features: string[];
  contact?: boolean;
}

export const getXuebanBilling = () => apiGet<{ data: BillingSummaryDto }>('/api/xueban-billing/summary').then(r => r.data);
export const getXuebanPlans = () => apiGet<{ items: PlanDto[] }>('/api/xueban-billing/plans').then(r => r.items);
export const upgradeXuebanPlan = (plan: 'basic' | 'studio') =>
  apiPost<{ data: BillingSummaryDto }>('/api/xueban-billing/upgrade', { plan }).then(r => r.data);


// 教研组真实模型对话：Node 持久化，Python 校验教研组归属。
export type XuebanChatKind = 'single' | 'group';
export interface XuebanConversationDto {
  id: string;
  company_id: string;
  kind: XuebanChatKind;
  title: string;
  created_at: string;
  updated_at: string;
  message_count?: number;
}
export interface XuebanChatMessageDto {
  id: string;
  conversation_id: string;
  sender_kind: 'user' | 'ceo' | 'agent' | 'system';
  sender_name: string;
  agent_id: string | null;
  content: string;
  channel?: 'chat' | 'workflow';
  run_id?: string | null;
  task_id?: string | null;
  task_title?: string | null;
  thinking?: string;
  status?: 'pending' | 'waiting_approval' | 'running' | 'done' | 'failed' | 'blocked' | 'rejected' | null;
  duration_ms?: number | null;
  metadata_json?: string | null;
  created_at: string;
}
export interface XuebanChatLogRunDto {
  run_id: string;
  messages: XuebanChatMessageDto[];
  activities: ChatActivity[];
  approvals: ChatApproval[];
}
export interface XuebanChatLogItemDto {
  conversation: XuebanConversationDto;
  runs: XuebanChatLogRunDto[];
  unassigned_messages: XuebanChatMessageDto[];
}
export const getXuebanChatLogs = (limit = 20, cursor = '') => apiGet<{ items: XuebanChatLogItemDto[]; next_cursor: string | null }>(`/api/xueban-chat/logs?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
export const decideXuebanChatApproval = (conversationId: string, approvalId: string, payload: { decision: 'approve' | 'reject' | 'custom'; custom_text?: string; outcome?: 'continue' | 'reject' }) => apiPost<{ approval: ChatApproval }>(`/api/xueban-chat/${encodeURIComponent(conversationId)}/approvals/${encodeURIComponent(approvalId)}`, payload);
export interface ChatApprovalListItemDto extends ChatApproval {
  conversation_title: string;
}
export const getXuebanChatApprovals = (limit = 100) => apiGet<{ items: ChatApprovalListItemDto[] }>(`/api/xueban-chat/approvals?limit=${limit}`).then(r => r.items);
export interface ChatDeliverableDto {
  id: string;
  title: string;
  kind: string;
  updated_at: string;
  final: { content: string; created_at: string } | null;
  employees: Array<{ id: string; name: string; task_title: string; content: string; status: string; created_at: string }>;
  pending_approvals: number;
}
export const getXuebanChatDeliverables = (limit = 40) => apiGet<{ items: ChatDeliverableDto[] }>(`/api/xueban-chat/deliverables?limit=${limit}`).then(r => r.items);

export const getXuebanConversations = () => apiGet<{ items: XuebanConversationDto[] }>('/api/xueban-chat').then(r => r.items);
export const createXuebanConversation = (kind: XuebanChatKind) => apiPost<{ conversation: XuebanConversationDto }>('/api/xueban-chat', { kind }).then(r => r.conversation);
export const getXuebanConversation = (id: string) => apiGet<{ conversation: XuebanConversationDto; messages: XuebanChatMessageDto[] }>(`/api/xueban-chat/${id}`);
export const deleteXuebanConversation = (id: string) => apiDelete<void>(`/api/xueban-chat/${id}`);
export const sendXuebanChat = (id: string, payload: {
  content: string;
  provider: ByokProvider;
  model: string;
  custom: boolean;
  base_url?: string;
}) => request<{ messages: XuebanChatMessageDto[] }>(`/api/xueban-chat/${id}/messages`, {
  method: 'POST', body: JSON.stringify(payload),
}, 300_000);

export type XuebanChatStreamEvent =
  | { type: 'conversation-restored'; messages: XuebanChatMessageDto[] }
  | { type: 'checkpoint'; message: XuebanChatMessageDto }
  | { type: 'activity'; activity: ChatActivity }
  | { type: 'approval'; approval: ChatApproval }
  | { type: 'message-start'; client_id: string; sender_kind: 'ceo' | 'agent'; sender_name: string; agent_id?: string; channel?: 'chat' | 'workflow'; run_id?: string; task_id?: string; task_title?: string; workflow?: boolean }
  | { type: 'thinking-delta' | 'content-delta'; client_id: string; text: string }
  | { type: 'message-end'; client_id: string; message: XuebanChatMessageDto }
  | { type: 'done'; plan?: { strategy?: string; source?: string; dag?: { nodes: ChatDagNode[] } } }
  | { type: 'error'; code: string; message: string };

export interface XuebanChatStreamPayload {
  content: string;
  provider: ByokProvider;
  model: string;
  custom: boolean;
  base_url?: string;
  thinking_effort?: 'off' | 'low' | 'medium' | 'high';
  /** 学段年级背景（由浏览器端计算好，服务端只读）。 */
  student_profile?: string;
}

async function streamXuebanChat(
  path: string,
  payload: XuebanChatStreamPayload,
  onEvent: (event: XuebanChatStreamEvent) => void,
  files: File[] = [],
  extraHeaders?: Record<string, string>,
): Promise<void> {
  let body: BodyInit = JSON.stringify(payload);
  if (files.length) {
    const form = new FormData();
    for (const [key, value] of Object.entries(payload)) form.append(key, String(value));
    for (const file of files) form.append('files', file, file.name);
    body = form;
  }
  const response = await apiFetch(path, {
    method: 'POST',
    headers: { Accept: 'application/x-ndjson', ...(extraHeaders ?? {}) },
    body,
  }, 300_000);
  if (!response.ok) {
    const text = await response.text();
    let body: unknown = text;
    try { body = JSON.parse(text); } catch { /* 保留文本错误。 */ }
    const error = errorMessage(body, response.status);
    throw new ApiError(response.status, error.code, error.message, body, error.traceId);
  }
  if (!response.body) throw new ApiError(502, 'STREAM_UNAVAILABLE', '浏览器未能读取模型流式响应', null);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const consume = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as XuebanChatStreamEvent;
    if (event.type === 'error') throw new ApiError(502, event.code, event.message, event);
    onEvent(event);
  };
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? '';
    for (const line of lines) consume(line);
    if (done) {
      if (buffer.trim()) consume(buffer);
      break;
    }
  }
}

const chatModelPayload = (content: string): XuebanChatStreamPayload => {
  let preset = 'deepseek';
  try { preset = localStorage.getItem('sf_model_preset') ?? 'deepseek'; } catch { /* 忽略存储异常 */ }
  const read = (key: string, fallback: string) => {
    try { return localStorage.getItem(key)?.trim() || fallback; } catch { return fallback; }
  };
  if (preset === 'custom') {
    const entry = activeCustomEntry();
    return {
      content, provider: 'openai', custom: true,
      model: entry?.model ?? read('sf_custom_model', ''), base_url: entry?.baseUrl ?? read('sf_custom_base_url', ''),
    };
  }
  if (preset === 'deepseek') {
    return {
      content, provider: 'deepseek', custom: false,
      model: read('sf_selected_model', 'deepseek-flash'), base_url: 'https://api.deepseek.com/v1',
    };
  }
  return {
    content, provider: 'deepseek', custom: false,
    model: read('sf_selected_model', 'deepseek-flash'), base_url: 'https://api.deepseek.com/v1',
    thinking_effort: (read('sf_thinking_effort', 'low') as 'off' | 'low' | 'medium' | 'high'),
    student_profile: profileContext(),
  };
};

export const sendXuebanChatStream = (
  id: string,
  payload: XuebanChatStreamPayload,
  onEvent: (event: XuebanChatStreamEvent) => void,
  files: File[] = [],
  extraHeaders?: Record<string, string>,
) => streamXuebanChat(`/api/xueban-chat/${id}/messages`, payload, onEvent, files, extraHeaders);

export const editXuebanChatMessageStream = (
  id: string,
  messageId: string,
  content: string,
  onEvent: (event: XuebanChatStreamEvent) => void,
) => streamXuebanChat(`/api/xueban-chat/${id}/messages/${encodeURIComponent(messageId)}/edit`, chatModelPayload(content), onEvent);

export const redoXuebanChatCheckpointStream = (
  id: string,
  checkpointId: string,
  onEvent: (event: XuebanChatStreamEvent) => void,
) => streamXuebanChat(`/api/xueban-chat/${id}/checkpoints/${encodeURIComponent(checkpointId)}/redo`, chatModelPayload(''), onEvent);

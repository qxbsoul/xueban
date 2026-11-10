/* 智慧学伴 · 教研组化学情工作台类型定义。 */

export type ViewKey =
  | 'workbench'
  | 'approvals'
  | 'logs'
  | 'org'
  | 'tasks'
  | 'deliverables'
  | 'dashboard'
  | 'knowledge'
  | 'settings';

export interface MsgCard {
  kind: 'deliverable';
  title: string;
  meta: string;
}

export type ChatActivityStatus = 'pending' | 'waiting_approval' | 'running' | 'done' | 'failed' | 'blocked' | 'rejected';

export interface ChatApproval {
  id: string;
  company_id: string;
  conversation_id: string;
  run_id: string;
  task_id: string;
  task_title: string;
  /** 需要审批的工单正文；历史记录可能为空。 */
  content: string;
  status: 'pending' | 'decided';
  decision: 'approve' | 'reject' | 'custom' | null;
  custom_text: string;
  outcome: 'continue' | 'reject' | null;
  created_at: string;
  decided_at: string | null;
}


export interface ChatDagNode {
  id: string;
  planner_task_id?: string | null;
  agent_id: string;
  name: string;
  title: string;
  prompt?: string;
  depends_on: string[];
  declared_depends_on?: string[];
  requires_approval: boolean;
  validation_error: string | null;
}

export interface ChatActivity {
  id: string;
  run_id: string;
  kind: 'tool' | 'dispatch' | 'model' | 'approval';
  name: string;
  title: string;
  status: ChatActivityStatus;
  actor: string;
  agent_id?: string;
  task_id?: string;
  depends_on?: string[];
  thinking?: string;
  approval_id?: string;
  /** 该节点执行前的 Checkpoint id；异常栏用于“从异常点回滚”。 */
  checkpoint_id?: string;
  detail?: string;
  input?: string;
  output?: string;
  duration_ms?: number;
  created_at: string;
}

export interface ChatAttachment {
  kind: 'image' | 'video' | 'text';
  name: string;
  mime: string;
  size: number;
  data_url?: string;
  text?: string;
}

export interface ChatCheckpoint {
  id: string;
  task_id: string;
  task_title: string;
  sequence: number;
  /** 恢复点描述：重跑前的任务标题。 */
  resume_before?: string;
  preserved_titles?: string[];
  rerun_titles?: string[];
}

export interface Msg {
  id?: string;
  attachments?: ChatAttachment[];
  from: 'ceo' | 'boss' | 'agent' | 'system';
  /** 消息真实来源教研员 id；头像/身份统一按它匹配，避免同名或“姓名（岗位）”不一致。 */
  agentId?: string;
  text: string;
  time: string;
  /** ISO 时间戳：用于计算一轮的总耗时。 */
  at?: string;
  senderName?: string;
  card?: MsgCard;
  thinking?: string;
  thinkingDone?: boolean;
  streaming?: boolean;
  activity?: ChatActivity;
  approval?: ChatApproval;
  checkpoint?: ChatCheckpoint;
  channel?: 'chat' | 'workflow';
  runId?: string;
}

export interface Activity {
  id: string;
  title: string;
  position: string;
  steps: string[];
  current: number;
  detail: string;
  status: 'running' | 'done' | 'blocked' | 'failed';
}

export interface ChatWorkflowItem {
  id: string;
  agentId?: string;
  /** 该产出对应的 DAG 任务 id；同一任务重做后有多条产出时用它归并到同一个节点。 */
  taskId?: string;
  name: string;
  taskTitle: string;
  content: string;
  thinking: string;
  status: ChatActivityStatus;
  /** 已被 Checkpoint 回滚覆盖的历史产物。 */
  superseded?: boolean;
  durationMs?: number;
  startedAt: number;
}

export interface ChatWorkflowRun {
  id: string;
  goal: string;
  plan: string;
  strategy?: string;
  source?: string;
  createdAt: number;
  items: ChatWorkflowItem[];
  activities: ChatActivity[];
  dag?: ChatDagNode[];
}

export interface ChatWorkflow {
  runs: ChatWorkflowRun[];
}

export const EMPTY_CHAT_WORKFLOW: ChatWorkflow = { runs: [] };

export type DlType = '简报' | '方案' | '脚本' | '素材';

export interface Deliverable {
  id: string;
  title: string;
  type: DlType;
  client: string;
  time: string;
  status: '已交付' | '待审核' | '失败' | '已取消';
  mark: string;
  color: string;
  sourceId?: string;
  rawStatus?: string;
  content?: string;
  contentLoaded?: boolean;
  contentMode?: 'live' | 'error';
}

export interface ErrItem {
  id: string;
  title: string;
  cause: string;
  impact: string;
  action: string;
  time: string;
  resolved: boolean;
}

export interface TaskItem {
  id: string;
  title: string;
  schedule: string;
  next: string;
  runs: number;
  position: string;
  enabled: boolean;
  once: boolean;
  mark: string;
  cronExpression?: string | null;
  startAt?: string | null;
  agentId?: string | null;
  durationMin?: number;
  completed?: boolean;
}

export interface KbItem {
  id: string;
  title: string;
  description?: string;
  desc: string;
  tags: string[];
  mark: string;
  color: string;
  sources?: Array<{
    source_id: string;
    filename: string;
    extension?: string;
    size_bytes?: number;
    ingest_status?: string;
    chunk_count?: number;
    error_message?: string;
  }>;
}

export interface Department {
  id: string;
  name: string;
  charter?: string;
}

export interface Position {
  id: string;
  name: string;
  deptId: string | null;
  managerId: string | null;
  avatar: string;
  enabled: boolean;
  soul: string;
  root?: boolean;
  /** 后端没有独立岗位表；这些字段把派生岗位映射回教研员 Agent 及其岗位角色。 */
  sourceEmployeeId?: string;
  sourceRole?: string;
}

export interface Staff {
  id: string;
  name: string;
  positionId: string;
  managerId: string | null;
  avatar: string;
  skills: string[];
  tools: string[];
}

export interface OrgState {
  departments: Department[];
  positions: Position[];
  staff: Staff[];
}

export interface ModelOption {
  id: string;
  label: string;
  vendor: string;
  tier: string;
}

export interface DashboardKpi {
  label: string;
  value: string;
  trend: string;
  dir: 'up' | 'down';
}

export interface DashboardSeriesPoint {
  day: number;
  label: string;
  calls: number;
  inputTokens: number;
  outputTokens: number;
  tokens: number;
  costCny: number;
}

export interface DashboardBreakdownItem {
  key: string;
  calls: number;
  inputTokens: number;
  cachedInputTokens: number;
  outputTokens: number;
  tokens: number;
  costCny: number;
}

export interface DashboardState {
  kpis: DashboardKpi[];
  series: DashboardSeriesPoint[];
  breakdown: DashboardBreakdownItem[];
  pricing: { source: string; inputPerMillion: number; cachedInputPerMillion: number; outputPerMillion: number } | null;
  loaded: boolean;
}

export const EMPTY_DASHBOARD: DashboardState = {
  kpis: [
    { label: '总 Token', value: '—', trend: '近 30 天真实模型返回用量', dir: 'up' },
    { label: '输入 Token', value: '—', trend: '含缓存命中输入', dir: 'up' },
    { label: '输出 Token', value: '—', trend: '模型实际返回', dir: 'up' },
    { label: '刊例成本估算', value: '—', trend: '人民币 · 不代表实际扣费', dir: 'down' },
    { label: '模型调用次数', value: '—', trend: '近 30 天真实请求', dir: 'up' },
  ],
  series: [],
  breakdown: [],
  pricing: null,
  loaded: false,
};

export interface ChatDeliverableEmployeeOutput {
  id: string;
  name: string;
  task_title: string;
  content: string;
  status: string;
  created_at: string;
}

/** 学情成果以「对话」为单位：最终成果为主，教研员中间产出为次要记录。 */
export interface ConversationDeliverable {
  id: string;
  title: string;
  kind: string;
  updated_at: string;
  final: { content: string; created_at: string } | null;
  employees: ChatDeliverableEmployeeOutput[];
  pending_approvals: number;
}

export type ModalState =
  | { kind: 'new-dept' }
  | { kind: 'rename-dept'; deptId: string }
  | { kind: 'delete-dept'; deptId: string }
  | { kind: 'new-position'; deptId?: string }
  | { kind: 'delete-position'; positionId: string }
  | { kind: 'new-staff'; positionId?: string }
  | { kind: 'delete-staff'; staffId: string }
  | { kind: 'new-task' }
  | { kind: 'delete-task'; taskId: string }
  | { kind: 'delete-conversation'; conversationId: string }
  | { kind: 'preview-conversation-deliverable'; deliverableId: string };

export interface Toast {
  id: string;
  msg: string;
}

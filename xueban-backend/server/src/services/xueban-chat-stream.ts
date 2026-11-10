import type { XuebanChatApproval, XuebanChatMessage, XuebanMessageChannel } from '../storage/xueban-chat';

export interface XuebanChatActivity {
  id: string;
  run_id: string;
  kind: 'tool' | 'dispatch' | 'model' | 'approval';
  name: string;
  title: string;
  status: 'pending' | 'waiting_approval' | 'running' | 'done' | 'failed' | 'blocked' | 'rejected';
  actor: string;
  agent_id?: string;
  task_id?: string;
  depends_on?: string[];
  thinking?: string;
  approval_id?: string;
  /** 该节点执行前的 Checkpoint id；异常栏用它提供“从异常点回滚”。 */
  checkpoint_id?: string;
  detail?: string;
  input?: string;
  output?: string;
  duration_ms?: number;
  created_at: string;
}

export type XuebanChatStreamEvent =
  | { type: 'conversation-restored'; messages: XuebanChatMessage[] }
  | { type: 'checkpoint'; message: XuebanChatMessage }
  | { type: 'activity'; activity: XuebanChatActivity }
  | { type: 'approval'; approval: XuebanChatApproval }
    | { type: 'message-start'; client_id: string; sender_kind: 'ceo' | 'agent'; sender_name: string; agent_id?: string; channel?: XuebanMessageChannel; run_id?: string; task_id?: string; task_title?: string; workflow?: boolean }
  | { type: 'thinking-delta'; client_id: string; text: string }
  | { type: 'content-delta'; client_id: string; text: string }
  | { type: 'message-end'; client_id: string; message: XuebanChatMessage }
  | { type: 'done'; plan?: unknown }
  | { type: 'error'; code: string; message: string };

const OPEN_TAG = '<sf:thinking>';
const CLOSE_TAG = '</sf:thinking>';

function safeSuffixLength(buffer: string, tag: string): number {
  const max = Math.min(buffer.length, tag.length - 1);
  for (let size = max; size > 0; size -= 1) {
    if (tag.startsWith(buffer.slice(-size))) return size;
  }
  return 0;
}

/** 将上游已标记的 reasoning_content 与正式答案增量分离，兼容标签跨 chunk。 */
export class ThinkingDeltaParser {
  private buffer = '';
  private phase: 'answer' | 'thinking' = 'answer';
  private answer = '';
  private reasoning = '';

  constructor(
    private readonly clientId: string,
    private readonly emit?: (event: XuebanChatStreamEvent) => void,
  ) {}

  push(text: string): void {
    this.buffer += text;
    while (this.buffer) {
      const tag = this.phase === 'answer' ? OPEN_TAG : CLOSE_TAG;
      const index = this.buffer.indexOf(tag);
      if (index >= 0) {
        this.flush(this.buffer.slice(0, index));
        this.buffer = this.buffer.slice(index + tag.length);
        this.phase = this.phase === 'answer' ? 'thinking' : 'answer';
        continue;
      }
      const keep = safeSuffixLength(this.buffer, tag);
      this.flush(this.buffer.slice(0, this.buffer.length - keep));
      this.buffer = this.buffer.slice(this.buffer.length - keep);
      break;
    }
  }

  finish(): string {
    this.flush(this.buffer);
    this.buffer = '';
    return this.answer.trim();
  }

  get thinking(): string {
    return this.reasoning.trim();
  }

  private flush(text: string): void {
    if (!text) return;
    if (this.phase === 'thinking') {
      this.reasoning += text;
      this.emit?.({ type: 'thinking-delta', client_id: this.clientId, text });
    } else {
      this.answer += text;
      this.emit?.({ type: 'content-delta', client_id: this.clientId, text });
    }
  }
}

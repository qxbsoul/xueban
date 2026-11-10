import type { Msg, MsgCard } from './types';

/**
 * 学情工作台对话的本地过渡存储。
 *
 * 当前后端项目 API 没有聊天历史契约，因此这里只负责浏览器恢复，不伪造服务端会话。
 * 后续聊天后端接入时，以同一 scope 替换为服务端 repository；store 和视图无需改写。
 * 缓存按教研组与会话 scope 隔离，仅用于恢复后端已确认的对话记录。
 */
const STORAGE_PREFIX = 'sf_workbench_chat:v1:';
const MAX_MESSAGES = 200;

function keyFor(scope: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(scope)}`;
}

function parseCard(value: unknown): MsgCard | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const card = value as Partial<MsgCard>;
  if (card.kind !== 'deliverable' || typeof card.title !== 'string' || typeof card.meta !== 'string') return undefined;
  return { kind: 'deliverable', title: card.title, meta: card.meta };
}

export function restoreChatMessages(scope: string): Msg[] | null {
  try {
    const raw = localStorage.getItem(keyFor(scope));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const messages = parsed.flatMap(item => {
      if (!item || typeof item !== 'object') return [];
      const value = item as Partial<Msg>;
      if (!['ceo', 'boss', 'agent', 'system'].includes(value.from || '') || typeof value.text !== 'string' || typeof value.time !== 'string') return [];
      const card = parseCard(value.card);
      return [{ from: value.from as Msg['from'], text: value.text, time: value.time, senderName: value.senderName, ...(card ? { card } : {}) }];
    });
    return messages.slice(-MAX_MESSAGES);
  } catch {
    return null;
  }
}

export function persistChatMessages(scope: string, messages: Msg[]): void {
  try {
    localStorage.setItem(keyFor(scope), JSON.stringify(messages.slice(-MAX_MESSAGES)));
  } catch {
    // 隐私模式或配额不足不阻断聊天；后续服务端历史可承担持久化。
  }
}

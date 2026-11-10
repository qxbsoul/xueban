export interface ConversationMeta {
  id: string;
  title: string;
  projectId?: string;
  kind?: 'single' | 'group';
  updatedAt: string;
  /** 消息条数；0 表示还是空对话，新开对话可直接复用。 */
  messageCount?: number;
  /** 本对话选中的模型（select 的值：模型 id 或 custom:<id>）。 */
  model?: string;
  /** 本对话的思考强度。 */
  thinkingEffort?: 'off' | 'low' | 'medium' | 'high';
}

export interface ConversationWorkspace {
  activeId: string;
  items: ConversationMeta[];
}

const PREFIX = 'sf_conversations:v1:';
const keyFor = (scope: string) => `${PREFIX}${encodeURIComponent(scope)}`;

export function restoreConversationWorkspace(scope: string): ConversationWorkspace {
  try {
    const raw = localStorage.getItem(keyFor(scope));
    if (!raw) return { activeId: '', items: [] };
    const parsed = JSON.parse(raw) as Partial<ConversationWorkspace>;
    const items = Array.isArray(parsed.items) ? parsed.items.flatMap(item => {
      if (!item || typeof item.id !== 'string' || typeof item.title !== 'string') return [];
      return [{
        id: item.id,
        title: item.title,
        projectId: typeof item.projectId === 'string' ? item.projectId : undefined,
        kind: item.kind === 'group' ? 'group' as const : 'single' as const,
        updatedAt: typeof item.updatedAt === 'string' ? item.updatedAt : new Date().toISOString(),
        messageCount: typeof item.messageCount === 'number' ? item.messageCount : undefined,
        model: typeof item.model === 'string' ? item.model : undefined,
        thinkingEffort: (item.thinkingEffort === 'off' || item.thinkingEffort === 'low' || item.thinkingEffort === 'medium' || item.thinkingEffort === 'high') ? item.thinkingEffort as 'off' | 'low' | 'medium' | 'high' : undefined,
      }];
    }) : [];
    return { activeId: typeof parsed.activeId === 'string' ? parsed.activeId : '', items };
  } catch {
    return { activeId: '', items: [] };
  }
}

export function persistConversationWorkspace(scope: string, workspace: ConversationWorkspace): void {
  try { localStorage.setItem(keyFor(scope), JSON.stringify(workspace)); } catch { /* 存储不可用不阻断项目流程 */ }
}

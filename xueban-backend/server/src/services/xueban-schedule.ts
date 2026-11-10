import { appendXuebanMessage, createXuebanConversation, listXuebanMessages } from '../storage/xueban-chat';
import { XuebanChatError, runXuebanChatTurn } from './xueban-chat';
import type { XuebanChatModelConfig } from './xueban-chat';

export interface ScheduledRunResult {
  conversationId: string;
  status: 'succeeded' | 'failed';
  error?: string;
  outputSummary: string;
  messageCount: number;
}

/**
 * 执行一次定时任务：建一条「定时任务｜…」工作台对话，跑完整 XUEBAN 回合，
 * 并且只在总经理产出正式答复时算成功。
 */
export async function executeScheduledTask(input: {
  companyId: string;
  description: string;
  agentId?: string;
  authorization: string;
  config: XuebanChatModelConfig;
}): Promise<ScheduledRunResult> {
  const conversation = createXuebanConversation(input.companyId, 'single', `定时任务｜${input.description}`.slice(0, 40));
  const finish = (status: 'succeeded' | 'failed', detail?: string): ScheduledRunResult => {
    const messages = listXuebanMessages(conversation.id);
    const final = [...messages].reverse().find(message => message.sender_kind === 'ceo'
      && message.channel !== 'workflow' && !String(message.metadata_json ?? '').includes('"dag"'));
    if (status === 'succeeded' && final) {
      return {
        conversationId: conversation.id, status, messageCount: messages.length,
        outputSummary: final.content.slice(0, 2000),
      };
    }
    const reason = detail || messages.filter(message => message.sender_kind === 'agent' && message.status === 'failed')[0]?.content || '本轮没有产出最终成果';
    appendXuebanMessage(conversation.id, {
      sender_kind: 'system', sender_name: '总经理', channel: 'workflow', status: 'failed',
      content: `定时任务执行失败：${reason}`,
    });
    return {
      conversationId: conversation.id, status: 'failed', error: reason.slice(0, 300),
      outputSummary: `定时任务执行失败：${reason}`.slice(0, 2000), messageCount: messages.length + 1,
    };
  };

  try {
    await runXuebanChatTurn({
      conversationId: conversation.id,
      companyId: input.companyId,
      content: input.description,
      config: input.config,
      authorization: input.authorization,
      assignedAgentId: input.agentId,
    });
  } catch (error) {
    // 配置/权限类错误（无 Key、越权、非法参数）仍然硬失败；
    // 模型执行类错误留在对话里，由调用方决定是否记录为失败的运行。
    if (error instanceof XuebanChatError && [401, 403, 404, 422].includes(error.status)) throw error;
    const detail = error instanceof XuebanChatError ? error.message : error instanceof Error ? error.message : String(error);
    return finish('failed', detail);
  }
  return finish('succeeded');
}

import { useMemo, useState } from 'react';
import type { ChatApprovalListItemDto } from '../api';
import ChatRichContent from './ChatRichContent';
import { useWorkbench } from '../store';

const FILTERS = ['待审批', '已批准', '已驳回', '全部'];

function timeText(raw?: string | number | null): string {
  if (!raw) return '时间未记录';
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? String(raw) : date.toLocaleString('zh-CN', { hour12: false });
}

function statusText(item: ChatApprovalListItemDto): '待审批' | '已批准' | '已驳回' {
  if (item.status === 'pending') return '待审批';
  return item.outcome === 'reject' ? '已驳回' : '已批准';
}

function decisionText(item: ChatApprovalListItemDto): string {
  if (item.status === 'pending') return '';
  if (item.decision === 'approve') return '老师已批准并继续';
  if (item.decision === 'reject') return '老师已驳回，该分支停止';
  return item.outcome === 'continue' ? '按自定义意见继续' : '按自定义意见停止';
}

/** 人工审批：需要老师确认的任务节点。 */
export default function ApprovalsView() {
  const {
    chatApprovals, chatApprovalsLoading, refreshChatApprovals, decideChatApproval,
    toast, gwStatus,
  } = useWorkbench();
  const [filter, setFilter] = useState('待审批');
  const [busyId, setBusyId] = useState('');

  const taskList = chatApprovals.filter(item => filter === '全部' || statusText(item) === filter);
  const taskPending = chatApprovals.filter(item => item.status === 'pending').length;
  const pendingCount = taskPending;

  const decideTask = async (item: ChatApprovalListItemDto, decision: 'approve' | 'reject') => {
    if (busyId) return;
    setBusyId(item.id);
    try {
      await decideChatApproval(item.id, decision, '', undefined, item.conversation_id);
    } catch {
      toast('审批提交失败，请重试');
    } finally {
      setBusyId('');
    }
  };

  const refresh = () => {
    void refreshChatApprovals().catch(() => toast('刷新失败，请稍后重试'));
  };

  return (
    <div className="view-inner">
      <div className="view-head">
        <h2>人工审批</h2>
        <div className="actions">
          <button className="btn btn-ghost btn-sm" disabled={chatApprovalsLoading || gwStatus !== 'ok'} onClick={refresh}>
            {chatApprovalsLoading ? '正在刷新…' : '刷新审批'}
          </button>
        </div>
      </div>

      <div className="filter-tabs">
        {FILTERS.map(item => (
          <button key={item} className={'ftab' + (filter === item ? ' active' : '')} onClick={() => setFilter(item)}>
            {item}
            {item === '待审批' && pendingCount > 0 ? ` ${pendingCount}` : ''}
          </button>
        ))}
      </div>

      <section className="approval-section">
        <div className="approval-section-head">
          <h3>任务审批</h3>
          <span>{taskPending} 待处理</span>
        </div>
        {taskList.length ? taskList.map(item => {
          const pending = item.status === 'pending';
          return (
            <div className="task-card approval-card" key={item.id}>
              <div className="task-ico">审</div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="task-name">{item.task_title || '待审批任务'}</div>
                <div className="task-meta">
                  <span>对话：<b>{item.conversation_title || '未命名对话'}</b></span>
                  <span>{timeText(item.created_at)}</span>
                  <span className={'pill ' + (pending ? 'pill-wait' : item.outcome === 'reject' ? 'pill-err' : 'pill-ok')}>
                    {statusText(item)}
                  </span>
                </div>
                <div className="approval-content">
                  <div className="approval-content-label">需要审批的内容</div>
                  {item.content ? (
                    <ChatRichContent content={item.content} className="approval-content-body" />
                  ) : (
                    <div className="approval-content-body approval-content-empty">这条历史审批没有存下工单内容，只保留了标题与决定结果。</div>
                  )}
                </div>
                {!pending && (
                  <div className="approval-decision">
                    <b>{decisionText(item)}</b>
                    {item.custom_text && <span>意见：{item.custom_text}</span>}
                    <span className="muted">{timeText(item.decided_at)}</span>
                  </div>
                )}
              </div>
              {pending && (
                <div className="task-right" style={{ minWidth: '118px' }}>
                  <button className="btn btn-dark btn-sm" disabled={Boolean(busyId)} onClick={() => void decideTask(item, 'approve')}>
                    {busyId === item.id ? '提交中…' : '批准并继续'}
                  </button>
                  <button className="btn btn-danger btn-sm" disabled={Boolean(busyId)} onClick={() => void decideTask(item, 'reject')}>
                    驳回此分支
                  </button>
                </div>
              )}
            </div>
          );
        }) : (
          <div className="panel panel-pad"><div className="empty-tip">
            {filter === '待审批' ? '当前没有待审批的任务；对话里需要老师确认的节点会出现在这里' : '该分类下暂无任务审批'}
          </div></div>
        )}
      </section>

    </div>
  );
}

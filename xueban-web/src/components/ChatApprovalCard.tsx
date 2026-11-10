import { useState } from 'react';
import { useWorkbench } from '../store';
import type { ChatApproval } from '../types';

const decisionText = (approval: ChatApproval) => {
  if (approval.decision === 'approve') return '批准并继续';
  if (approval.decision === 'reject') return '驳回并停止此分支';
  return approval.outcome === 'continue' ? '按自定义意见继续' : '按自定义意见停止此分支';
};

export default function ChatApprovalCard({ approval }: { approval: ChatApproval }) {
  const { decideChatApproval, chatApprovals } = useWorkbench();
  const [customOpen, setCustomOpen] = useState(false);
  const [customText, setCustomText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  // 以服务端最新审批状态为准：即使这条消息里的快照还是 pending，也不会再显示可点按钮。
  const live = chatApprovals.find(item => item.id === approval.id);
  const current = live ?? approval;
  const pending = current.status === 'pending';

  const submit = async (decision: 'approve' | 'reject' | 'custom') => {
    if (decision === 'custom' && !customText.trim()) return;
    setSubmitting(true);
    try {
      await decideChatApproval(approval.id, decision, customText.trim(), decision === 'custom' ? 'continue' : undefined);
      setCustomOpen(false);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className={`chat-approval-card ${pending ? 'pending' : 'decided'}`} aria-label="老师审批">
      <div className="chat-approval-heading"><span>老师审批</span><small>{pending ? '此节点与下游正在等待你的选择' : decisionText(current)}</small></div>
      <h4>{current.task_title || '确认后继续执行'}</h4>
      <div className="chat-approval-content">
        <span>需要审批的内容</span>
        {current.content
          ? <pre>{current.content}</pre>
          : <p className="chat-approval-content-empty">这条审批没有存下工单内容，只保留了标题与决定结果。</p>}
      </div>
      {pending ? (
        <>
          <div className="chat-approval-options">
            <button type="button" disabled={submitting} onClick={() => void submit('approve')}>批准并继续</button>
            <button type="button" disabled={submitting} onClick={() => void submit('reject')}>驳回此分支</button>
            <button type="button" disabled={submitting} aria-expanded={customOpen} onClick={() => setCustomOpen(value => !value)}>其他／自定义</button>
          </div>
          {customOpen && (
            <form className="chat-approval-custom" onSubmit={event => { event.preventDefault(); void submit('custom'); }}>
              <textarea autoFocus rows={3} value={customText} disabled={submitting} onChange={event => setCustomText(event.target.value)} placeholder="写明调整意见；提交后将保留意见并继续当前节点" />
              <div><button type="submit" disabled={submitting || !customText.trim()}>{submitting ? '正在提交…' : '按此意见继续'}</button><button type="button" disabled={submitting} onClick={() => setCustomOpen(false)}>取消</button></div>
            </form>
          )}
        </>
      ) : (
        <div className="chat-approval-result">{current.custom_text && <p>{current.custom_text}</p>}<time>{current.decided_at ? new Date(current.decided_at).toLocaleString('zh-CN', { hour12: false }) : ''}</time></div>
      )}
    </section>
  );
}

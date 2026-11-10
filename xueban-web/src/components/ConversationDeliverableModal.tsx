import { useMemo, useState } from 'react';
import { useWorkbench } from '../store';
import ChatRichContent from './ChatRichContent';

/** 学情成果预览：最终成果为主，教研员中间产出收在次要区域，可折叠查看。 */
export default function ConversationDeliverableModal({ deliverableId }: { deliverableId: string }) {
  const { conversationDeliverables, conversationDeliverablesLoading, refreshConversationDeliverables, closeModal, gwStatus } = useWorkbench();
  const item = conversationDeliverables.find(entry => entry.id === deliverableId);
  const [employeesOpen, setEmployeesOpen] = useState(false);
  const [expandedEmployeeId, setExpandedEmployeeId] = useState('');

  const subtitle = useMemo(() => {
    if (!item) return '';
    const date = item.updated_at ? new Date(item.updated_at) : null;
    const time = date && !Number.isNaN(date.getTime()) ? date.toLocaleString('zh-CN', { hour12: false }) : '时间未记录';
    return `${item.kind === 'group' ? '群聊' : '单聊'} · ${item.employees.length} 份教研员产出 · ${time}`;
  }, [item]);

  if (!item) {
    return (
      <>
        <div className="modal-body">
          <div className="empty-tip">
            {gwStatus === 'ok'
              ? conversationDeliverablesLoading ? '正在加载学情成果…' : '该对话的学情成果不存在或已被删除'
              : '后端未连接，无法加载学情成果'}
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn btn-ghost btn-sm" onClick={closeModal}>关闭</button>
          {gwStatus === 'ok' && (
            <button className="btn btn-dark btn-sm" disabled={conversationDeliverablesLoading} onClick={() => void refreshConversationDeliverables()}>
              重新加载
            </button>
          )}
        </div>
      </>
    );
  }

  const pending = item.pending_approvals > 0;

  return (
    <>
      <div className="modal-body deliverable-modal">
        <div className="deliverable-head">
          <h4>{item.title}</h4>
          <div className="deliverable-meta">
            <span>{subtitle}</span>
            <span className={'pill ' + (item.final ? 'pill-ok' : pending ? 'pill-wait' : 'pill-err')}>
              {item.final ? '已交付' : pending ? '待审批' : '未完成'}
            </span>
          </div>
        </div>

        <section className="deliverable-final" aria-label="最终成果">
          <div className="deliverable-section-title">最终成果（教学主管交付）</div>
          {item.final ? (
            <ChatRichContent content={item.final.content} className="deliverable-final-body" />
          ) : (
            <div className="empty-tip">
              {pending ? '该对话还有节点等待审批，审批并执行完成后才会产出最终成果。' : '该对话还没有产出最终成果。'}
            </div>
          )}
        </section>

        <section className="deliverable-employees" aria-label="教研员输出记录">
          <button type="button" className="deliverable-employees-toggle" aria-expanded={employeesOpen} onClick={() => setEmployeesOpen(value => !value)}>
            <span>教研员输出记录</span>
            <span>{item.employees.length ? `${item.employees.length} 份 · ${employeesOpen ? '收起' : '展开查看过程'} ` : '无'}</span>
          </button>
          {employeesOpen && item.employees.length > 0 && (
            <div className="deliverable-employee-list">
              {item.employees.map(output => (
                <div className="deliverable-employee" key={output.id}>
                  <button type="button" className="deliverable-employee-head" aria-expanded={expandedEmployeeId === output.id} onClick={() => setExpandedEmployeeId(prev => prev === output.id ? '' : output.id)}>
                    <strong>{output.name}</strong>
                    <span>{output.task_title || '岗位工单'}</span>
                    <span className={'pill ' + (output.status === 'done' ? 'pill-ok' : output.status === 'failed' ? 'pill-err' : 'pill-wait')}>
                      {output.status === 'done' ? '完成' : output.status === 'failed' ? '失败' : output.status === 'blocked' ? '已阻断' : '进行中'}
                    </span>
                  </button>
                  {expandedEmployeeId === output.id && (
                    <ChatRichContent content={output.content} className="deliverable-employee-body" />
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" onClick={closeModal}>关闭</button>
        {gwStatus === 'ok' && (
          <button className="btn btn-dark btn-sm" disabled={conversationDeliverablesLoading} onClick={() => void refreshConversationDeliverables()}>
            刷新
          </button>
        )}
      </div>
    </>
  );
}

import { useCallback, useEffect, useMemo, useState } from 'react';
import { getXuebanChatLogs, type XuebanChatLogItemDto, type XuebanChatLogRunDto } from '../api';
import { useWorkbench } from '../store';
import ChatRichContent from './ChatRichContent';

type StatusFilter = 'all' | 'running' | 'approval' | 'failed' | 'done' | 'other';

function timeText(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}

function runStatus(run: XuebanChatLogRunDto): Exclude<StatusFilter, 'all'> {
  if (run.approvals.some(item => item.status === 'pending') || run.messages.some(item => item.status === 'waiting_approval') || run.activities.some(item => item.status === 'waiting_approval' || item.status === 'blocked')) return 'approval';
  if (run.messages.some(item => item.status === 'failed' || item.status === 'rejected') || run.activities.some(item => item.status === 'failed' || item.status === 'rejected')) return 'failed';
  if (run.messages.some(item => item.status === 'running' || item.status === 'pending') || run.activities.some(item => item.status === 'running' || item.status === 'pending')) return 'running';
  if (run.messages.some(item => item.status === 'done') || run.activities.some(item => item.status === 'done')) return 'done';
  return 'other';
}

function stripSecrets(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripSecrets);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .filter(([key]) => !/(api[-_]?key|secret|password|authorization|access[-_]?token|refresh[-_]?token)/i.test(key))
    .map(([key, nested]) => [key, stripSecrets(nested)]));
}

export default function LogsView() {
  const { gwStatus, currentCompany, toast } = useWorkbench();
  const [items, setItems] = useState<XuebanChatLogItemDto[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [keyword, setKeyword] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');

  const load = useCallback(async (cursor = '', append = false) => {
    if (gwStatus !== 'ok') return;
    setLoading(true);
    setError('');
    try {
      const result = await getXuebanChatLogs(20, cursor);
      setItems(previous => append ? [...previous, ...result.items] : result.items);
      setNextCursor(result.next_cursor);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [gwStatus]);

  useEffect(() => { void load(); }, [load, currentCompany?.id]);

  const filteredItems = useMemo(() => {
    const query = keyword.trim().toLowerCase();
    return items.map(item => {
      const conversationHit = `${item.conversation.title} ${item.conversation.id}`.toLowerCase().includes(query);
      const runs = item.runs.filter(run => {
        if (status !== 'all' && runStatus(run) !== status) return false;
        if (!query || conversationHit) return true;
        return JSON.stringify(run).toLowerCase().includes(query);
      });
      const unassigned = item.unassigned_messages.filter(message => {
        if (status !== 'all' && status !== 'other') return false;
        return !query || conversationHit || JSON.stringify(message).toLowerCase().includes(query);
      });
      return { ...item, runs, unassigned_messages: unassigned };
    }).filter(item => item.runs.length || item.unassigned_messages.length);
  }, [items, keyword, status]);

  const exportJson = () => {
    if (!filteredItems.length) return toast('当前筛选条件下没有可导出的日志');
    try {
      const payload = stripSecrets({
        exported_at: new Date().toISOString(),
        company_id: currentCompany?.id,
        filters: { keyword: keyword.trim(), status },
        items: filteredItems,
      });
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `xueban-logs-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
      toast('已导出当前筛选日志，敏感密钥字段已排除');
    } catch (reason) {
      toast(`日志导出失败：${reason instanceof Error ? reason.message : String(reason)}`);
    }
  };

  return (
    <div className="view-inner logs-view">
      <div className="view-head"><h2>运行日志</h2><div className="actions"><button className="btn btn-ghost btn-sm" disabled={!filteredItems.length} onClick={exportJson}>导出当前 JSON</button><button className="btn btn-ghost btn-sm" disabled={loading || gwStatus !== 'ok'} onClick={() => void load()}>{loading ? '正在读取…' : '刷新日志'}</button></div></div>
      <div className="log-filters panel panel-pad">
        <div className="form-group"><label className="form-label" htmlFor="log-keyword">关键词</label><input id="log-keyword" className="form-input" value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索目标、消息、岗位或运行编号" /></div>
        <div className="form-group"><label className="form-label" htmlFor="log-status">状态（本地筛选）</label><select id="log-status" className="form-select" value={status} onChange={event => setStatus(event.target.value as StatusFilter)}><option value="all">全部状态</option><option value="running">运行中 / 等待执行</option><option value="approval">人工审批 / 阻塞</option><option value="failed">失败 / 驳回</option><option value="done">已完成</option><option value="other">其他 / 未关联运行</option></select></div>
        <span className="log-filter-count">显示 {filteredItems.length} / {items.length} 个对话</span>
      </div>
      {error && <div className="panel panel-pad log-error" role="alert">{error}<button className="btn btn-ghost btn-sm" onClick={() => void load()}>重试</button></div>}
      {!items.length && !loading && !error && <div className="panel panel-pad empty-tip">当前教研组还没有对话与执行日志</div>}
      {Boolean(items.length) && !filteredItems.length && <div className="panel panel-pad empty-tip">当前关键词或状态下没有匹配日志</div>}
      {filteredItems.map(item => (
        <section className="log-conversation" key={item.conversation.id}>
          <header><div><h3>{item.conversation.title}</h3><span>{item.conversation.kind === 'group' ? '群聊' : '单聊'} · {timeText(item.conversation.updated_at)}</span></div></header>
          {item.runs.map(run => {
            const goal = run.messages.find(message => message.sender_kind === 'user')?.content ?? '历史执行';
            const labels = { running: '运行中', approval: '待审批', failed: '失败', done: '已完成', other: '其他' };
            return (
              <details className="log-run" key={run.run_id} open={filteredItems.length === 1}>
                <summary><span>{goal}</span><small>{labels[runStatus(run)]} · {run.activities.length} 条活动 · {run.approvals.length} 次审批</small></summary>
                <div className="log-timeline">
                  {run.messages.map(message => (
                    <article className={`log-entry ${message.sender_kind}`} key={message.id}>
                      <div className="log-entry-meta"><strong>{message.sender_name}</strong><span>{message.task_title || message.channel}{message.status ? ` · ${message.status}` : ''}</span><time>{timeText(message.created_at)}</time></div>
                      {message.content && <ChatRichContent content={message.content} />}
                      {message.thinking && <details className="log-thinking"><summary>思考过程</summary><div>{message.thinking}</div></details>}
                    </article>
                  ))}
                  {run.approvals.map(approval => <div className="log-approval" key={approval.id}><strong>人工选择：{approval.status === 'pending' ? '等待选择' : approval.decision === 'approve' ? '批准' : approval.decision === 'reject' ? '驳回' : '自定义'}</strong><span>{approval.custom_text || approval.outcome || '等待选择'}</span></div>)}
                </div>
              </details>
            );
          })}
          {item.unassigned_messages.map(message => <article className="log-entry standalone" key={message.id}><div className="log-entry-meta"><strong>{message.sender_name}</strong><time>{timeText(message.created_at)}</time></div><ChatRichContent content={message.content} /></article>)}
        </section>
      ))}
      {nextCursor && <button className="btn btn-ghost log-more" disabled={loading} onClick={() => void load(nextCursor, true)}>加载更早记录</button>}
    </div>
  );
}

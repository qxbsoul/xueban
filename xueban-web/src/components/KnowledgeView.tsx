import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from 'react';
import { KB_TYPES } from '../data';
import { useWorkbench } from '../store';
import type { KnowledgeHitDto, KnowledgeSourceDto } from '../api';

const ACCEPT = '.txt,.md,.markdown,.log,.csv,.json,.jsonl,.html,.htm,.xlsx,.xlsm,.docx,.pptx,.pdf,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff';
const MAX_FILE_BYTES = 64 * 1024 * 1024;
const INGEST_LABELS: Record<string, string> = {
  done: '可检索', ready: '可检索', pending: '等待索引', indexing: '索引中', failed: '索引失败',
};

function fileSize(bytes = 0): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function ingestLabel(source: KnowledgeSourceDto): string {
  return INGEST_LABELS[source.ingest_status ?? ''] ?? source.ingest_status ?? '状态未知';
}

function uploadSummary(sources: KnowledgeSourceDto[]): string {
  const groups = new Map<string, number>();
  for (const source of sources) {
    const label = ingestLabel(source);
    groups.set(label, (groups.get(label) ?? 0) + 1);
  }
  return `服务端已接收 ${sources.length} 份资料：` + [...groups].map(([label, count]) => `${label} ${count} 份`).join('，');
}

export default function KnowledgeView() {
  const {
    knowledge, createKnowledge, renameKnowledge, removeKnowledge, removeKnowledgeSource,
    uploadKnowledge, searchKnowledge, kbFilter, setKbFilter,
  } = useWorkbench();
  const inputRef = useRef<HTMLInputElement>(null);
  const [targetId, setTargetId] = useState('');
  const [expandedId, setExpandedId] = useState('');
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [editingId, setEditingId] = useState('');
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [confirm, setConfirm] = useState<{ kind: 'pack' | 'source'; packId: string; sourceId?: string; label: string } | null>(null);
  const [query, setQuery] = useState('');
  const [hits, setHits] = useState<KnowledgeHitDto[]>([]);
  const [ragBackend, setRagBackend] = useState('');
  const [searched, setSearched] = useState(false);
  const list = knowledge.filter(k => kbFilter === '全部' || k.tags.includes(kbFilter));

  useEffect(() => {
    if (!knowledge.some(item => item.id === targetId)) setTargetId(knowledge[0]?.id ?? '');
    if (expandedId && !knowledge.some(item => item.id === expandedId)) setExpandedId('');
  }, [knowledge, targetId, expandedId]);

  const perform = async (key: string, operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(key);
    setError('');
    setNotice('');
    try {
      await operation();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy('');
    }
  };

  const upload = async (files: File[]) => {
    if (!files.length || busy) return;
    setHits([]);
    setSearched(false);
    const oversized = files.find(file => file.size > MAX_FILE_BYTES);
    if (oversized) {
      setError(`${oversized.name} 超过 64 MB，请压缩或拆分后上传`);
      return;
    }
    await perform('upload', async () => {
      const sources = await uploadKnowledge(files, targetId || undefined);
      setNotice(uploadSummary(sources));
      if (inputRef.current) inputRef.current.value = '';
    });
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void upload(Array.from(event.dataTransfer.files));
  };

  const onZoneKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (!busy) inputRef.current?.click();
    }
  };

  const search = async () => {
    const packId = expandedId || targetId;
    if (!packId || !query.trim() || busy) return;
    await perform('search', async () => {
      const result = await searchKnowledge(packId, query.trim());
      setHits(result.hits);
      setRagBackend(result.ragBackend);
      setSearched(true);
    });
  };

  const startEdit = (id: string) => {
    const pack = knowledge.find(item => item.id === id);
    if (!pack) return;
    setEditingId(id);
    setEditName(pack.title);
    setEditDescription(pack.description ?? '');
    setConfirm(null);
  };

  return (
    <div className="view-inner knowledge-page">
      <div className="view-head">
        <h2>课程资源库</h2>
        <div className="actions">
          <button className="btn btn-primary btn-sm" disabled={Boolean(busy)} onClick={() => setCreating(value => !value)}>
            {creating ? '收起新建' : '新建知识包'}
          </button>
        </div>
      </div>

      {creating && (
        <form className="panel panel-pad" style={{ marginBottom: 16 }} onSubmit={event => {
          event.preventDefault();
          void perform('create', async () => {
            await createKnowledge(newName, newDescription);
            setNewName('');
            setNewDescription('');
            setCreating(false);
          });
        }}>
          <div className="form-group">
            <label className="form-label" htmlFor="kb-new-name">知识包名称</label>
            <input id="kb-new-name" className="form-input" value={newName} maxLength={100} onChange={event => setNewName(event.target.value)} placeholder="例如：产品资料" required />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="kb-new-description">描述</label>
            <textarea id="kb-new-description" className="form-area" style={{ minHeight: 70 }} value={newDescription} maxLength={500} onChange={event => setNewDescription(event.target.value)} />
          </div>
          <button className="btn btn-dark btn-sm" type="submit" disabled={Boolean(busy) || !newName.trim()}>{busy === 'create' ? '创建中…' : '创建知识包'}</button>
        </form>
      )}

      <div
        className={`kb-dropzone${dragging ? ' dragging' : ''}${busy === 'upload' ? ' uploading' : ''}`}
        role="button"
        tabIndex={busy ? -1 : 0}
        aria-label="上传课程资源库资料"
        onClick={() => { if (!busy) inputRef.current?.click(); }}
        onKeyDown={onZoneKey}
        onDragEnter={event => { event.preventDefault(); setDragging(true); }}
        onDragOver={event => event.preventDefault()}
        onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
        onDrop={onDrop}
      >
        <input ref={inputRef} type="file" multiple accept={ACCEPT} hidden onChange={event => void upload(Array.from(event.target.files ?? []))} />
        <span className="kb-upload-mark" aria-hidden="true">↑</span>
        <div className="kb-upload-copy">
          <strong>{busy === 'upload' ? '正在上传，等待服务端返回索引状态…' : '拖动文件到这里，或点击选择'}</strong>
          <span>支持文档、表格、PDF 和图片，单文件不超过 64 MB</span>
        </div>
        {knowledge.length > 0 && (
          <select className="kb-target" aria-label="上传到课程资源库" value={targetId} disabled={Boolean(busy)} onClick={event => event.stopPropagation()} onChange={event => setTargetId(event.target.value)}>
            {knowledge.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select>
        )}
      </div>
      {error && <div className="kb-error" role="alert">{error}</div>}
      {notice && <div className="panel panel-pad" role="status" style={{ marginBottom: 16, color: 'var(--navy)' }}>{notice}</div>}

      <div className="filter-tabs">
        {KB_TYPES.map(t => <button key={t} className={'ftab' + (kbFilter === t ? ' active' : '')} onClick={() => setKbFilter(t)}>{t}</button>)}
      </div>

      <div className="panel kb-list">
        {list.length ? list.map(k => (
          <div className={`kb-pack${expandedId === k.id ? ' expanded' : ''}`} key={k.id}>
            <button className="kb-item" onClick={() => { setExpandedId(expandedId === k.id ? '' : k.id); setHits([]); setSearched(false); setRagBackend(''); setConfirm(null); }}>
              <span className="kb-ico" style={{ background: k.color, color: 'var(--navy)' }}>{k.mark}</span>
              <span className="kb-pack-main">
                <span className="kb-title">{k.title}</span>
                <span className="kb-desc">{k.desc}</span>
                <span className="kb-tags">{k.tags.map(t => <span className="tag" key={t}>{t}</span>)}</span>
              </span>
              <span className="kb-expand" aria-hidden="true">{expandedId === k.id ? '−' : '+'}</span>
            </button>
            {expandedId === k.id && (
              <div className="kb-detail">
                {k.description && <p style={{ color: 'var(--muted)', fontSize: 12 }}>{k.description}</p>}
                <div className="actions" style={{ marginBottom: 12 }}>
                  <button className="mini-btn" disabled={Boolean(busy)} onClick={() => startEdit(k.id)}>重命名 / 编辑描述</button>
                  <button className="mini-btn" disabled={Boolean(busy)} onClick={() => setConfirm({ kind: 'pack', packId: k.id, label: k.title })}>删除知识包</button>
                </div>
                {editingId === k.id && (
                  <form className="panel panel-pad" style={{ marginBottom: 14 }} onSubmit={event => {
                    event.preventDefault();
                    void perform(`edit:${k.id}`, async () => {
                      await renameKnowledge(k.id, editName, editDescription);
                      setEditingId('');
                    });
                  }}>
                    <label className="form-label">名称</label>
                    <input className="form-input" value={editName} maxLength={100} onChange={event => setEditName(event.target.value)} required />
                    <label className="form-label" style={{ marginTop: 10 }}>描述</label>
                    <textarea className="form-area" style={{ minHeight: 70 }} value={editDescription} maxLength={500} onChange={event => setEditDescription(event.target.value)} />
                    <div className="actions" style={{ marginTop: 10 }}>
                      <button className="btn btn-dark btn-sm" type="submit" disabled={Boolean(busy) || !editName.trim()}>{busy ? '保存中…' : '保存修改'}</button>
                      <button className="btn btn-ghost btn-sm" type="button" disabled={Boolean(busy)} onClick={() => setEditingId('')}>取消</button>
                    </div>
                  </form>
                )}
                {confirm?.packId === k.id && (
                  <div className="kb-error" role="alert" style={{ margin: '0 0 14px' }}>
                    <div>确认删除{confirm.kind === 'pack' ? `知识包「${confirm.label}」及其全部资料` : `来源「${confirm.label}」`}？此操作不可撤销。</div>
                    <div className="actions" style={{ marginTop: 8 }}>
                      <button className="btn btn-danger btn-sm" disabled={Boolean(busy)} onClick={() => void perform('delete', async () => {
                        if (confirm.kind === 'pack') await removeKnowledge(confirm.packId);
                        else if (confirm.sourceId) await removeKnowledgeSource(confirm.packId, confirm.sourceId);
                        setConfirm(null);
                      })}>{busy === 'delete' ? '删除中…' : '确认删除'}</button>
                      <button className="btn btn-ghost btn-sm" disabled={Boolean(busy)} onClick={() => setConfirm(null)}>取消</button>
                    </div>
                  </div>
                )}
                <div className="kb-sources">
                  {(k.sources ?? []).length ? (k.sources ?? []).map(source => (
                    <div className="kb-source" key={source.source_id}>
                      <span className="kb-source-name">{source.filename}</span>
                      <span>{source.chunk_count ?? 0} 片段 · {fileSize(source.size_bytes)}</span>
                      <span className={source.ingest_status === 'done' || source.ingest_status === 'ready' ? 'kb-source-ok' : 'kb-source-failed'} title={source.error_message}>
                        {source.error_message || ingestLabel(source)}
                        <button className="mini-btn" disabled={Boolean(busy)} onClick={() => setConfirm({ kind: 'source', packId: k.id, sourceId: source.source_id, label: source.filename })}>删除</button>
                      </span>
                    </div>
                  )) : <div className="empty-tip">还没有资料，从上方上传第一份文件</div>}
                </div>
                <form className="kb-search" onSubmit={event => { event.preventDefault(); void search(); }}>
                  <input value={query} onChange={event => setQuery(event.target.value)} placeholder="输入问题，验证这份资料能否被检索" aria-label="课程资源库检索问题" />
                  <button className="btn btn-secondary btn-sm" type="submit" disabled={Boolean(busy) || !query.trim()}>{busy === 'search' ? '检索中…' : '测试检索'}</button>
                </form>
                {ragBackend && <div className="kb-search-hint">实际 rag_backend：<b>{ragBackend}</b></div>}
                {hits.length > 0 && <div className="kb-hits">{hits.map((hit, index) => (
                  <div className="kb-hit" key={`${index}-${hit.source ?? ''}`}>
                    <div className="kb-hit-source">来源：{hit.source_name || hit.source || k.title}</div>
                    <p>{hit.text}</p>
                  </div>
                ))}</div>}
                {searched && hits.length === 0 && <div className="kb-search-hint">本次检索没有命中原文片段。</div>}
              </div>
            )}
          </div>
        )) : <div className="empty-tip">新建知识包，或直接上传文件自动创建教研组资源库</div>}
      </div>
    </div>
  );
}

import { useState } from 'react';
import { generateOrgDraft, publishOrgDraft, type XuebanOrgDraft } from '../api';

interface Props {
  companyId: string;
  hasModelKey: boolean;
  onClose: () => void;
  onPublished: () => Promise<void>;
}

function splitList(value: string): string[] {
  return [...new Set(value.split(/[,，]/).map(item => item.trim()).filter(Boolean))];
}

export default function AiOrgBuilder({ companyId, hasModelKey, onClose, onPublished }: Props) {
  const [description, setDescription] = useState('');
  const [draft, setDraft] = useState<XuebanOrgDraft | null>(null);
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState<'draft' | 'publish' | null>(null);
  const [error, setError] = useState('');
  const [published, setPublished] = useState<{ departments: number; employees: number } | null>(null);

  const generate = async () => {
    if (!hasModelKey) return setError('请先在设置中配置真实模型 API Key');
    if (description.trim().length < 10) return setError('请用至少 10 个字符描述业务、产品与客户');
    if (!companyId) return setError('教研组信息尚未加载，请刷新后重试');
    setBusy('draft');
    setError('');
    try {
      const result = await generateOrgDraft(companyId, description.trim());
      setDraft(result.draft);
      setModel(result.model);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '草案生成失败，请重试');
    } finally {
      setBusy(null);
    }
  };

  const publish = async () => {
    if (!draft || published) return;
    setBusy('publish');
    setError('');
    try {
      const result = await publishOrgDraft(companyId, draft);
      setPublished(result.created);
      try {
        await onPublished();
      } catch {
        setError('组织已发布，但列表刷新失败，请关闭后刷新教研团队。');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '发布失败，草案未写入组织');
    } finally {
      setBusy(null);
    }
  };

  const updateDepartment = (index: number, field: 'name' | 'charter', value: string) => {
    setDraft(current => current ? {
      ...current,
      departments: current.departments.map((item, i) => i === index ? { ...item, [field]: value } : item),
    } : current);
  };

  const updateEmployee = <K extends keyof XuebanOrgDraft['employees'][number]>(
    index: number,
    field: K,
    value: XuebanOrgDraft['employees'][number][K],
  ) => {
    setDraft(current => current ? {
      ...current,
      employees: current.employees.map((item, i) => i === index ? { ...item, [field]: value } : item),
    } : current);
  };

  return (
    <div className="modal-mask show" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <section className="modal wide org-ai-modal" role="dialog" aria-modal="true" aria-labelledby="org-ai-title">
        <div className="modal-head">
          <div>
            <h3 id="org-ai-title">AI 建制</h3>
            <div className="org-ai-subtitle">真实模型生成草案，人工确认后才创建部门与教研员</div>
          </div>
          <button className="modal-x" disabled={Boolean(busy)} onClick={onClose} aria-label="关闭">✕</button>
        </div>
        <div className="modal-body">
          <div className="form-group">
            <label className="form-label" htmlFor="org-business-description">业务描述</label>
            <textarea
              id="org-business-description"
              className="form-area org-ai-description"
              placeholder="例如：为七年级数学「有理数与整式」做学情诊断，梳理学习要求、分析模拟学情数据并给出个性化学习建议……"
              value={description}
              disabled={Boolean(busy) || Boolean(published)}
              onChange={event => setDescription(event.target.value)}
            />
          </div>
          <div className="org-ai-generate-row">
            <span className={hasModelKey ? 'org-ai-key ready' : 'org-ai-key'}>
              {hasModelKey ? '模型密钥已配置' : '尚未配置模型密钥'}
            </span>
            <button className="btn btn-dark btn-sm" disabled={Boolean(busy) || !hasModelKey || Boolean(published)} onClick={() => void generate()}>
              {busy === 'draft' ? '真实模型生成中…' : draft ? '重新生成草案' : '生成组织草案'}
            </button>
          </div>
          {error && <div className="org-ai-error" role="alert">{error}</div>}
          {busy === 'draft' && <div className="empty-tip org-ai-empty">正在调用真实模型设计组织，请稍候；期间不会修改现有组织。</div>}
          {!draft && busy !== 'draft' && <div className="empty-tip org-ai-empty">填写业务描述后生成草案。你可以检查并编辑每个部门、岗位、汇报关系和权限。</div>}
          {draft && (
            <div className={busy || published ? 'org-ai-draft is-readonly' : 'org-ai-draft'}>
              <div className="org-ai-draft-head">
                <strong>组织草案</strong>
                <span>{draft.departments.length} 个部门 · {draft.employees.length} 名教研员 · {model}</span>
              </div>
              <div className="org-ai-notice">草案尚未写入生产组织。确认发布采用后端事务，任一创建失败则全部回滚。</div>
              <section>
                <h4 className="org-ai-section-title">部门</h4>
                <div className="org-ai-departments">
                  {draft.departments.map((department, index) => (
                    <div className="org-ai-department" key={department.id}>
                      <input className="form-input" aria-label={`部门 ${index + 1} 名称`} value={department.name} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateDepartment(index, 'name', event.target.value)} />
                      <textarea className="form-area" aria-label={`${department.name} 职责`} value={department.charter} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateDepartment(index, 'charter', event.target.value)} />
                    </div>
                  ))}
                </div>
              </section>
              <section>
                <h4 className="org-ai-section-title">教研员与汇报关系</h4>
                <div className="org-ai-employees">
                  {draft.employees.map((employee, index) => (
                    <details className="org-ai-employee" key={employee.id} open={draft.employees.length <= 6}>
                      <summary>{employee.name || '未命名教研员'} · {employee.title || '未填写岗位'}</summary>
                      <div className="org-ai-employee-body">
                        <div className="org-ai-grid">
                          <label>姓名<input className="form-input" value={employee.name} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'name', event.target.value)} /></label>
                          <label>岗位<input className="form-input" value={employee.title} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'title', event.target.value)} /></label>
                          <label>角色标识<input className="form-input" value={employee.role} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'role', event.target.value)} /></label>
                          <label>模型<input className="form-input" value={employee.model_name} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'model_name', event.target.value)} /></label>
                          <label>所属部门<select className="form-select" value={employee.department_id} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'department_id', event.target.value)}>{draft.departments.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
                          <label>汇报对象<select className="form-select" value={employee.manager_id ?? ''} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'manager_id', event.target.value || null)}><option value="">教学主管 / 无草案上级</option>{draft.employees.filter(item => item.id !== employee.id).map(item => <option key={item.id} value={item.id}>{item.name} · {item.title}</option>)}</select></label>
                        </div>
                        <label className="org-ai-field">职责提示词<textarea className="form-area" value={employee.system_prompt} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'system_prompt', event.target.value)} /></label>
                        <div className="org-ai-grid">
                          <label>技能（逗号分隔）<input className="form-input" value={employee.skills.join(', ')} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'skills', splitList(event.target.value))} /></label>
                          <label>工具权限（逗号分隔）<input className="form-input" value={employee.tools.join(', ')} disabled={Boolean(busy) || Boolean(published)} onChange={event => updateEmployee(index, 'tools', splitList(event.target.value))} /></label>
                        </div>
                      </div>
                    </details>
                  ))}
                </div>
              </section>
            </div>
          )}
          {published && <div className="org-ai-success">已真实创建 {published.departments} 个部门、{published.employees} 名教研员，教研团队已更新。</div>}
        </div>
        <div className="modal-foot">
          <button className="btn btn-ghost btn-sm" disabled={Boolean(busy)} onClick={onClose}>{published ? '完成' : '取消'}</button>
          {draft && !published && <button className="btn btn-dark btn-sm" disabled={Boolean(busy)} onClick={() => void publish()}>{busy === 'publish' ? '事务发布中…' : '确认发布组织'}</button>}
        </div>
      </section>
    </div>
  );
}

import { useEffect, useState } from 'react';
import type { OrgState, Position, Staff } from '../types';
import { AVATAR_PICKS } from '../data';
import { getEmployeeConfigVersions, restoreEmployeeConfigVersion } from '../api';
import type { EmployeeConfigVersionDto } from '../api';
import { useWorkbench } from '../store';
import AiOrgBuilder from './AiOrgBuilder';
import EmployeeAvatar from './EmployeeAvatar';
import AvatarPicker from './AvatarPicker';
import { avatarIdFor, isAvatarId } from '../avatar-set';

export default function OrgView() {
  const {
    org,
    companyId,
    refreshOrg,
    apiSettings,
    selectedPosId,
    selectedStaffId,
    selectPosition,
    selectStaff,
    openModal,
  } = useWorkbench();
  const [aiBuilderOpen, setAiBuilderOpen] = useState(false);

  const selectedPos = org.positions.find(p => p.id === selectedPosId) || null;
  const selectedStaff = org.staff.find(s => s.id === selectedStaffId) || null;

  return (
    <div className="view-inner">
      <div className="view-head">
        <h2>教研团队</h2>
        <div className="actions">
          <button className="btn btn-dark btn-sm" onClick={() => setAiBuilderOpen(true)}>
            AI 建制
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => openModal({ kind: 'new-dept' })}>
            新建部门
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => openModal({ kind: 'new-position' })}>
            新建岗位
          </button>
          <button className="btn btn-dark btn-sm" onClick={() => openModal({ kind: 'new-staff' })}>
            添加教研员
          </button>
        </div>
      </div>

      <div className="org-layout">
        <div className="org-canvas">
          <div className="org-root-card">
            <button className="redit" onClick={() => selectPosition('p-ceo')}>
              查看岗位说明书
            </button>
            <div className="tagline">教研负责人</div>
            <div className="rname">教学主管 · AI 学伴主理</div>
            <div className="tagline" style={{ opacity: 0.9 }}>直接向你汇报 · 统管全部部门</div>
          </div>
          <div className="org-tree-line" />
          <div style={{ height: '22px' }} />
          <div className="dept-grid">
            {org.departments.map(d => (
              <DeptCard
                key={d.id}
                org={org}
                deptId={d.id}
                selectedPosId={selectedPosId}
                selectedStaffId={selectedStaffId}
                onSelectPosition={selectPosition}
                onSelectStaff={selectStaff}
              />
            ))}
          </div>
        </div>

        <aside className="org-inspector">
          <div className="panel">
            {selectedPos ? (
              <PositionInspector key={selectedPos.id} position={selectedPos} org={org} />
            ) : selectedStaff ? (
              <StaffInspector key={selectedStaff.id} staff={selectedStaff} org={org} />
            ) : (
              <>
                <div className="insp-head">
                  <div className="kind">岗位说明书</div>
                  <h3>选择左侧节点查看</h3>
                </div>
                <div className="panel-pad">
                  <div className="empty-tip">
                    点击岗位或教研员，即可编辑职责、汇报关系与启用状态。
                    <br />
                    部门、岗位、教研员都可以随时增删调整。
                  </div>
                </div>
              </>
            )}
          </div>
        </aside>
      </div>
      {aiBuilderOpen && companyId && (
        <AiOrgBuilder
          companyId={companyId}
          hasModelKey={apiSettings.hasKey}
          onClose={() => setAiBuilderOpen(false)}
          onPublished={refreshOrg}
        />
      )}
    </div>
  );
}

/* ─────────── 部门卡片 ─────────── */

function DeptCard({
  org,
  deptId,
  selectedPosId,
  selectedStaffId,
  onSelectPosition,
  onSelectStaff,
}: {
  org: OrgState;
  deptId: string;
  selectedPosId: string | null;
  selectedStaffId: string | null;
  onSelectPosition: (id: string) => void;
  onSelectStaff: (id: string) => void;
}) {
  const { openModal } = useWorkbench();
  const dept = org.departments.find(d => d.id === deptId)!;
  const positions = org.positions.filter(p => p.deptId === deptId);
  const staffCount = positions.reduce((n, p) => n + org.staff.filter(s => s.positionId === p.id).length, 0);

  return (
    <div className="dept-card">
      <div className="dept-head">
        <div>
          <div className="dept-name">{dept.name}</div>
          <div className="dept-stat">
            {positions.length} 个岗位 · {staffCount} 名教研员
          </div>
        </div>
        <div className="dept-ops">
          <button className="mini-btn" title="在此部门新建岗位" onClick={() => openModal({ kind: 'new-position', deptId })}>
            添加岗位
          </button>
          <button className="mini-btn" title="重命名" onClick={() => openModal({ kind: 'rename-dept', deptId })}>
            重命名
          </button>
          <button className="mini-btn" title="删除部门" onClick={() => openModal({ kind: 'delete-dept', deptId })}>
            删除
          </button>
        </div>
      </div>
      <div className="pos-list">
        {positions.length ? (
          positions.map(p => {
            const mgr = p.managerId ? org.positions.find(x => x.id === p.managerId) : null;
            return (
              <div
                key={p.id}
                className={
                  'pos-card' +
                  (selectedPosId === p.id ? ' selected' : '') +
                  (p.enabled ? '' : ' disabled')
                }
                onClick={() => onSelectPosition(p.id)}
              >
                <div className="pos-top">
                  <span className="pos-kind">岗位</span>
                  <span className="pos-name">{p.name}</span>
                  <span className="pos-report">汇报 → {mgr ? mgr.name : '教研组'}</span>
                </div>
                <div className="staff-chips">
                  {org.staff
                    .filter(s => s.positionId === p.id)
                    .map(s => (
                      <span
                        key={s.id}
                        className={'staff-chip' + (selectedStaffId === s.id ? ' selected' : '')}
                        onClick={e => {
                          e.stopPropagation();
                          onSelectStaff(s.id);
                        }}
                      >
                        <EmployeeAvatar avatar={s.avatar} name={s.name} size={22} />
                        {s.name}
                      </span>
                    ))}
                  <span
                    className="staff-chip add"
                    onClick={e => {
                      e.stopPropagation();
                      openModal({ kind: 'new-staff', positionId: p.id });
                    }}
                  >
                    添加教研员
                  </span>
                </div>
              </div>
            );
          })
        ) : (
          <div className="empty-tip">暂无岗位，点右上角「新建岗位」</div>
        )}
      </div>
    </div>
  );
}

/* ─────────── 岗位说明书（检查器） ─────────── */

function PositionInspector({ position, org }: { position: Position; org: OrgState }) {
  const { savePosition, openModal, toast } = useWorkbench();
  const [name, setName] = useState(position.name);
  const [deptId, setDeptId] = useState(position.deptId ?? org.departments[0]?.id ?? '');
  const [managerId, setManagerId] = useState(position.managerId ?? 'p-ceo');
  const [soul, setSoul] = useState(position.soul);
  const [enabled, setEnabled] = useState(position.enabled);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    if (!name.trim()) return toast('请填写岗位名称');
    setSaving(true);
    try {
      // 岗位是角色，不带头像；头像只属于教研员档案。
      await savePosition(position.id, { name, deptId, managerId, soul, enabled });
    } catch {
      // store 已展示真实错误，保留当前编辑内容供重试。
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="insp-head">
        <div className="kind">岗位说明书</div>
        <h3>
          {position.name}
        </h3>
      </div>
      <div className="panel-pad">
        <div className="form-group">
          <label className="form-label">岗位名称</label>
          <input className="form-input" value={name} disabled={position.root || saving} onChange={e => setName(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">所属部门</label>
          <select className="form-select" value={deptId} disabled={position.root || saving} onChange={e => setDeptId(e.target.value)}>
            {org.departments.map(d => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">汇报对象</label>
          {position.root ? (
            <select className="form-select" disabled>
              <option>— 教研负责人，无上级 —</option>
            </select>
          ) : (
            <select className="form-select" value={managerId} disabled={saving} onChange={e => setManagerId(e.target.value)}>
              {org.positions
                .filter(x => x.id !== position.id && !x.root)
                .map(x => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                  </option>
                ))}
              <option value="p-ceo">教学主管（教研负责人）</option>
            </select>
          )}
        </div>
        <div className="form-group">
          <label className="form-label">职责提示词（岗位灵魂）</label>
          <textarea className="form-area" value={soul} disabled={position.root || saving} onChange={e => setSoul(e.target.value)} />
        </div>
        <div className="form-group switch-row">
          <label className="form-label" style={{ margin: 0 }}>
            启用该岗位
          </label>
          <div className={enabled ? 'switch on' : 'switch'} onClick={() => { if (!position.root && !saving) setEnabled(!enabled); }}>
            <i />
          </div>
        </div>
        {position.root ? (
          <div className="empty-tip">教学主管由系统管理，此说明书仅供查看。</div>
        ) : (
          <div style={{ display: 'flex', gap: '9px', marginTop: '16px' }}>
            <button className="btn btn-dark btn-sm" style={{ flex: 1 }} disabled={saving} onClick={() => void submit()}>
              {saving ? '保存中…' : '保存修改'}
            </button>
            <button className="btn btn-danger btn-sm" disabled={saving} onClick={() => openModal({ kind: 'delete-position', positionId: position.id })}>
              删除岗位
            </button>
          </div>
        )}
      </div>
    </>
  );
}

/* ─────────── 教研员档案（检查器） ─────────── */

function StaffInspector({ staff, org }: { staff: Staff; org: OrgState }) {
  const { saveStaff, openModal, toast } = useWorkbench();
  const [name, setName] = useState(staff.name);
  const [positionId, setPositionId] = useState(staff.positionId);
  const [managerId, setManagerId] = useState(staff.managerId ?? 'p-ceo');
  const [skills, setSkills] = useState(staff.skills.join(', '));
  const [tools, setTools] = useState(staff.tools.join(', '));
  const [avatar, setAvatar] = useState<string>(isAvatarId(staff.avatar) ? staff.avatar : avatarIdFor(staff.name));
  const [saving, setSaving] = useState(false);
  const pos = org.positions.find(p => p.id === positionId);
  const parseList = (value: string) => [...new Set(value.split(/[,，]/).map(item => item.trim()).filter(Boolean))];

  const submit = async () => {
    if (!name.trim()) return toast('请填写姓名');
    setSaving(true);
    try {
      await saveStaff(staff.id, { name, positionId, managerId, skills: parseList(skills), tools: parseList(tools), avatar });
    } catch {
      // store 已展示真实错误，保留当前编辑内容供重试。
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="insp-head">
        <div className="kind">教研员档案</div>
        <h3>
          <EmployeeAvatar avatar={staff.avatar} name={staff.name} size={30} />
          <span>{staff.name}</span>
        </h3>
      </div>
      <div className="panel-pad">
        <AvatarPicker value={avatar} onChange={setAvatar} disabled={saving} label="教研员头像" />
        <div className="form-group">
          <label className="form-label">姓名</label>
          <input className="form-input" value={name} disabled={saving} onChange={e => setName(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">所属岗位</label>
          <select className="form-select" value={positionId} disabled={saving} onChange={e => setPositionId(e.target.value)}>
            {org.positions
              .filter(p => !p.root)
              .map(p => (
                <option key={p.id} value={p.id}>
                  {p.name}（{org.departments.find(d => d.id === p.deptId)?.name || '—'}）
                </option>
              ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">汇报对象（岗位）</label>
          <select className="form-select" value={managerId} disabled={saving} onChange={e => setManagerId(e.target.value)}>
            {org.positions
              .filter(p => !p.root && p.id !== staff.positionId)
              .map(p => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            <option value="p-ceo">教学主管（教研负责人）</option>
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">技能（逗号分隔）</label>
          <input className="form-input" value={skills} disabled={saving} placeholder="市场研究, 数据分析" onChange={e => setSkills(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">工具权限（逗号分隔）</label>
          <input className="form-input" value={tools} disabled={saving} placeholder="web_search, knowledge_search" onChange={e => setTools(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">当前岗位职责</label>
          <div className="preview-box" style={{ fontSize: '11.5px' }}>
            {pos ? pos.soul : '—'}
          </div>
        </div>
        <EmployeeConfigVersions employeeId={staff.id} />
        <div style={{ display: 'flex', gap: '9px', marginTop: '16px' }}>
          <button className="btn btn-dark btn-sm" style={{ flex: 1 }} disabled={saving} onClick={() => void submit()}>
            {saving ? '保存中…' : '保存修改'}
          </button>
          <button className="btn btn-danger btn-sm" disabled={saving} onClick={() => openModal({ kind: 'delete-staff', staffId: staff.id })}>
            删除教研员
          </button>
        </div>
      </div>
    </>
  );
}

function EmployeeConfigVersions({ employeeId }: { employeeId: string }) {
  const { companyId, refreshOrg, toast } = useWorkbench();
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<EmployeeConfigVersionDto[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  const load = async () => {
    if (!companyId) return;
    setLoading(true);
    try {
      const items = await getEmployeeConfigVersions(companyId, employeeId);
      setVersions(items);
      setSelected(current => current ?? items[0]?.version ?? null);
    } catch (error) {
      toast(`配置版本加载失败：${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void load();
    // 仅在展开或切换教研员时读取，避免组织轮询造成额外请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, employeeId, companyId]);

  const restore = async (version: number) => {
    if (!companyId) return;
    setRestoring(version);
    try {
      const result = await restoreEmployeeConfigVersion(companyId, employeeId, version);
      await refreshOrg();
      await load();
      setSelected(result.version.version);
      toast(`已恢复 v${version}，并保存为 v${result.version.version}`);
    } catch (error) {
      toast(`配置恢复失败：${error instanceof Error ? error.message : '未知错误'}`);
    } finally {
      setRestoring(null);
    }
  };

  const current = versions.find(item => item.version === selected) ?? versions[0];
  return (
    <div className="config-versions">
      <button className="btn btn-ghost btn-sm" disabled={loading} onClick={() => setOpen(value => !value)}>
        {loading ? '读取版本中…' : open ? '收起配置版本' : '查看配置版本'}
      </button>
      {open && (
        <div className="config-version-panel">
          {!versions.length && !loading ? <div className="empty-tip">暂无可查看的配置版本。</div> : (
            <>
              <div className="config-version-list">
                {versions.map(item => (
                  <button key={item.id} className={current?.id === item.id ? 'active' : ''} onClick={() => setSelected(item.version)}>
                    <strong>v{item.version}</strong><span>{item.source}</span>
                  </button>
                ))}
              </div>
              {current && <div className="config-version-detail">
                <div className="audit-meta">v{current.version} · {current.source} · {new Date(current.created_at).toLocaleString()}</div>
                <dl>
                  <dt>职责</dt><dd>{current.config.system_prompt || '—'}</dd>
                  <dt>模型</dt><dd>{current.config.model_name || '—'}</dd>
                  <dt>Skills</dt><dd>{current.config.skills?.join('、') || '—'}</dd>
                  <dt>Tools</dt><dd>{current.config.tools?.join('、') || '—'}</dd>
                  <dt>Manager</dt><dd>{current.config.manager_id || '—'}</dd>
                </dl>
                <button className="btn btn-dark btn-sm" disabled={restoring !== null} onClick={() => void restore(current.version)}>
                  {restoring === current.version ? '恢复中…' : `恢复为 v${current.version}（生成新版本）`}
                </button>
              </div>}
            </>
          )}
        </div>
      )}
    </div>
  );
}

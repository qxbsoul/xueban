import { useEffect, useState } from 'react';
import { useWorkbench } from '../store';
import ConversationDeliverableModal from './ConversationDeliverableModal';
import AvatarPicker from './AvatarPicker';

export default function ModalHost() {
  const { modal, closeModal } = useWorkbench();

  useEffect(() => {
    if (!modal) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeModal();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [modal, closeModal]);

  if (!modal) return null;
  const wide = modal.kind === 'preview-conversation-deliverable';

  return (
    <div
      className="modal-mask show"
      onClick={e => {
        if (e.target === e.currentTarget) closeModal();
      }}
    >
      <div className={wide ? 'modal wide' : 'modal'} role="dialog" aria-modal="true" aria-label={TITLE[modal.kind]}>
        <div className="modal-head">
          <h3>{TITLE[modal.kind]}</h3>
          <button type="button" className="modal-x" onClick={closeModal} aria-label="关闭" title="关闭">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        <ModalBody modal={modal} />
      </div>
    </div>
  );
}

const TITLE: Record<string, string> = {
  'new-dept': '新建部门',
  'rename-dept': '重命名部门',
  'delete-dept': '删除部门',
  'new-position': '新建岗位',
  'delete-position': '删除岗位',
  'new-staff': '添加教研员',
  'delete-staff': '删除教研员',
  'new-task': '新建学习计划',
  'delete-task': '删除学习计划',
  'delete-conversation': '删除对话',
  'preview-conversation-deliverable': '学情成果',
};

function ModalBody({ modal }: { modal: NonNullable<ReturnType<typeof useWorkbench>['modal']> }) {
  switch (modal.kind) {
    case 'new-dept':
      return <NewDeptModal />;
    case 'rename-dept':
      return <RenameDeptModal deptId={modal.deptId} />;
    case 'delete-dept':
      return <DeleteDeptModal deptId={modal.deptId} />;
    case 'new-position':
      return <NewPositionModal deptId={modal.deptId} />;
    case 'delete-position':
      return <DeletePositionModal positionId={modal.positionId} />;
    case 'new-staff':
      return <NewStaffModal positionId={modal.positionId} />;
    case 'delete-staff':
      return <DeleteStaffModal staffId={modal.staffId} />;
    case 'new-task':
      return <NewTaskModal />;
    case 'delete-task':
      return <DeleteTaskModal taskId={modal.taskId} />;
    case 'delete-conversation':
      return <DeleteConversationModal conversationId={modal.conversationId} />;
    case 'preview-conversation-deliverable':
      return <ConversationDeliverableModal deliverableId={modal.deliverableId} />;
  }
}

function DeleteConversationModal({ conversationId }: { conversationId: string }) {
  const { conversations, deleteConversation, closeModal } = useWorkbench();
  const [busy, setBusy] = useState(false);
  const conversation = conversations.find(item => item.id === conversationId);
  const remove = async () => {
    setBusy(true);
    try { await deleteConversation(conversationId); } catch { setBusy(false); }
  };
  return (
    <>
      <div className="modal-body">
        <div className="preview-box">确认删除「<b>{conversation?.title ?? '这个对话'}</b>」？对话消息将一并删除，教研项目和学情成果不受影响。</div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" onClick={closeModal} disabled={busy}>取消</button>
        <button className="btn btn-danger btn-sm" onClick={() => void remove()} disabled={busy}>{busy ? '删除中' : '确认删除'}</button>
      </div>
    </>
  );
}

function useOrgSubmit() {
  const [busy, setBusy] = useState(false);
  const submit = async (operation: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try {
      await operation();
    } catch {
      // store 已展示真实错误，弹窗保留供修正后重试。
    } finally {
      setBusy(false);
    }
  };
  return { busy, submit };
}

/* ─────────── 部门 ─────────── */

function NewDeptModal() {
  const { createDept, closeModal, toast } = useWorkbench();
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="form-group">
          <label className="form-label">部门名称</label>
          <input className="form-input" value={name} disabled={busy} onChange={e => setName(e.target.value)} placeholder="例如：渠道增长部" />
        </div>
        <div className="form-group">
          <label className="form-label">说明（可选）</label>
          <input className="form-input" value={desc} disabled={busy} onChange={e => setDesc(e.target.value)} placeholder="这个部门负责什么业务" />
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-dark btn-sm" disabled={busy} onClick={() => {
          if (!name.trim()) return toast('请填写部门名称');
          void submit(() => createDept(name.trim(), desc.trim()));
        }}>
          {busy ? '创建中…' : '创建部门'}
        </button>
      </div>
    </>
  );
}

function RenameDeptModal({ deptId }: { deptId: string }) {
  const { org, renameDept, closeModal, toast } = useWorkbench();
  const dept = org.departments.find(d => d.id === deptId);
  const [name, setName] = useState(dept?.name ?? '');
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="form-group">
          <label className="form-label">部门名称</label>
          <input className="form-input" value={name} disabled={busy} onChange={e => setName(e.target.value)} />
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-dark btn-sm" disabled={busy} onClick={() => {
          if (!name.trim()) return toast('请填写部门名称');
          void submit(() => renameDept(deptId, name.trim()));
        }}>
          {busy ? '保存中…' : '保存'}
        </button>
      </div>
    </>
  );
}

function DeleteDeptModal({ deptId }: { deptId: string }) {
  const { org, deleteDept, closeModal } = useWorkbench();
  const dept = org.departments.find(d => d.id === deptId);
  const posIds = org.positions.filter(p => p.deptId === deptId).map(p => p.id);
  const staffN = org.staff.filter(s => posIds.includes(s.positionId)).length;
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="preview-box">
          确认删除「<b>{dept?.name}</b>」？将真实移除其下 <b>{posIds.length}</b> 个岗位与 <b>{staffN}</b> 名岗位教研员，再删除部门。此操作不可撤销。
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void submit(() => deleteDept(deptId))}>
          {busy ? '删除中…' : '确认删除'}
        </button>
      </div>
    </>
  );
}

/* ─────────── 岗位 ─────────── */

function NewPositionModal({ deptId }: { deptId?: string }) {
  const { org, createPosition, closeModal, toast } = useWorkbench();
  const [name, setName] = useState('');
  const [dept, setDept] = useState(deptId ?? org.departments[0]?.id ?? '');
  const [managerId, setManagerId] = useState('p-ceo');
  const [soul, setSoul] = useState('');
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="form-group">
          <label className="form-label">岗位名称</label>
          <input className="form-input" value={name} disabled={busy} onChange={e => setName(e.target.value)} placeholder="例如：增长运营专员" />
        </div>
        <div className="form-group">
          <label className="form-label">所属部门</label>
          <select className="form-select" value={dept} disabled={busy} onChange={e => setDept(e.target.value)}>
            {org.departments.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">汇报对象</label>
          <select className="form-select" value={managerId} disabled={busy} onChange={e => setManagerId(e.target.value)}>
            {org.positions.filter(p => !p.root).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            <option value="p-ceo">教学主管（教研负责人）</option>
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">职责提示词（岗位灵魂）</label>
          <textarea className="form-area" value={soul} disabled={busy} onChange={e => setSoul(e.target.value)} placeholder="描述这个岗位负责什么、怎么干活、交付什么" />
        </div>
        <div className="empty-tip">将创建真实岗位教研员，初始姓名为「岗位名·待入职」。</div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-dark btn-sm" disabled={busy} onClick={() => {
          if (!name.trim()) return toast('请填写岗位名称');
          void submit(() => createPosition({ name: name.trim(), deptId: dept, managerId, soul }));
        }}>
          {busy ? '创建中…' : '创建岗位'}
        </button>
      </div>
    </>
  );
}

function DeletePositionModal({ positionId }: { positionId: string }) {
  const { org, deletePosition, closeModal } = useWorkbench();
  const p = org.positions.find(x => x.id === positionId);
  const n = org.staff.filter(s => s.positionId === positionId).length;
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="preview-box">
          确认删除岗位「<b>{p?.name}</b>」？其下 <b>{n}</b> 名岗位教研员将同时被真实移除。此操作不可撤销。
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void submit(() => deletePosition(positionId))}>
          {busy ? '删除中…' : '确认删除'}
        </button>
      </div>
    </>
  );
}

/* ─────────── 教研员 ─────────── */

function NewStaffModal({ positionId }: { positionId?: string }) {
  const { org, createStaff, closeModal, toast } = useWorkbench();
  const [name, setName] = useState('');
  const [pos, setPos] = useState(positionId ?? org.positions.find(p => !p.root)?.id ?? '');
  const [managerId, setManagerId] = useState('p-ceo');
  const [avatar, setAvatar] = useState('');
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="form-group">
          <label className="form-label">姓名 / 花名</label>
          <input className="form-input" value={name} disabled={busy} onChange={e => setName(e.target.value)} placeholder="例如：小林" />
        </div>
        <div className="form-group">
          <label className="form-label">所属岗位</label>
          <select className="form-select" value={pos} disabled={busy} onChange={e => setPos(e.target.value)}>
            {org.positions.filter(p => !p.root).map(p => (
              <option key={p.id} value={p.id}>{p.name}（{org.departments.find(d => d.id === p.deptId)?.name || '—'}）</option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">汇报对象（岗位）</label>
          <select className="form-select" value={managerId} disabled={busy} onChange={e => setManagerId(e.target.value)}>
            {org.positions.filter(p => !p.root).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            <option value="p-ceo">教学主管（教研负责人）</option>
          </select>
        </div>
        <AvatarPicker value={avatar} onChange={setAvatar} disabled={busy} label="教研员头像（不选则按名字自动分配）" />
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-dark btn-sm" disabled={busy} onClick={() => {
          if (!name.trim()) return toast('请填写姓名');
          void submit(() => createStaff({ name: name.trim(), positionId: pos, managerId, avatar: avatar || undefined }));
        }}>
          {busy ? '添加中…' : '添加'}
        </button>
      </div>
    </>
  );
}

function DeleteStaffModal({ staffId }: { staffId: string }) {
  const { org, deleteStaff, closeModal } = useWorkbench();
  const s = org.staff.find(x => x.id === staffId);
  const [reassignTo, setReassignTo] = useState('');
  const { busy, submit } = useOrgSubmit();

  return (
    <>
      <div className="modal-body">
        <div className="preview-box">确认移除教研员「<b>{s?.name}</b>」？</div>
        <div className="form-group" style={{ marginTop: '14px' }}>
          <label className="form-label">未完成任务接手人（如有任务则必选）</label>
          <select className="form-select" value={reassignTo} disabled={busy} onChange={event => setReassignTo(event.target.value)}>
            <option value="">不指定；有未完成任务时取消操作</option>
            {org.staff.filter(staff => staff.id !== staffId).map(staff => <option key={staff.id} value={staff.id}>{staff.name}</option>)}
          </select>
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" disabled={busy} onClick={closeModal}>取消</button>
        <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void submit(() => deleteStaff(staffId, reassignTo || undefined))}>
          {busy ? '移除中…' : '确认移除'}
        </button>
      </div>
    </>
  );
}

/* ─────────── 学习计划 ─────────── */

function NewTaskModal() {
  const { org, createTask, closeModal, toast } = useWorkbench();
  const [name, setName] = useState('');
  const [freq, setFreq] = useState('每天');
  const [time, setTime] = useState('09:00');
  const [runDate, setRunDate] = useState('');
  const [weekDay, setWeekDay] = useState('1');
  const [monthDay, setMonthDay] = useState('1');

  return (
    <>
      <div className="modal-body">
        <div className="form-group">
          <label className="form-label">任务名称</label>
          <input
            className="form-input"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="例如：每周一早上 8 点生成班级学情周报"
          />
        </div>
        <div className="form-group">
          <label className="form-label">执行频率</label>
          <select className="form-select" value={freq} onChange={e => setFreq(e.target.value)}>
            <option>每天</option>
            <option>每周</option>
            <option>每月</option>
            <option>一次性</option>
          </select>
          <div style={{ fontSize: '10.5px', color: 'var(--muted)', marginTop: '5px' }}>
            {freq === '一次性' ? '将在指定日期和时间执行一次' : '按此周期自动重复执行'}
          </div>
        </div>
        {freq === '一次性' && (
          <div className="form-group">
            <label className="form-label">执行日期</label>
            <input className="form-input" type="date" value={runDate} onChange={e => setRunDate(e.target.value)} />
          </div>
        )}
        {freq === '每周' && (
          <div className="form-group">
            <label className="form-label">星期</label>
            <select className="form-select" value={weekDay} onChange={e => setWeekDay(e.target.value)}>
              {['一', '二', '三', '四', '五', '六', '日'].map((day, index) => (
                <option key={day} value={index === 6 ? '0' : String(index + 1)}>星期{day}</option>
              ))}
            </select>
          </div>
        )}
        {freq === '每月' && (
          <div className="form-group">
            <label className="form-label">每月日期</label>
            <input className="form-input" type="number" min="1" max="28" value={monthDay} onChange={e => setMonthDay(e.target.value)} />
          </div>
        )}
        <div className="form-group">
          <label className="form-label">执行时间</label>
          <input className="form-input" type="time" value={time} onChange={e => setTime(e.target.value)} />
        </div>
        <div className="form-group">
          <label className="form-label">执行方式</label>
          <div className="form-static">由教学主管统一调度：拆分工单、派给合适的教研员、汇总后交付。</div>
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" onClick={closeModal}>取消</button>
        <button
          className="btn btn-dark btn-sm"
          onClick={() => {
            if (!name.trim()) return toast('请填写任务名称');
            createTask({ title: name.trim(), freq, time: time || '09:00', runDate, weekDay, monthDay });
          }}
        >
          创建任务
        </button>
      </div>
    </>
  );
}

function DeleteTaskModal({ taskId }: { taskId: string }) {
  const { tasks, deleteTask, closeModal } = useWorkbench();
  const t = tasks.find(x => x.id === taskId);

  return (
    <>
      <div className="modal-body">
        <div className="preview-box">
          确认删除「<b>{t?.title}</b>」？删除后不再自动执行。
        </div>
      </div>
      <div className="modal-foot">
        <button className="btn btn-ghost btn-sm" onClick={closeModal}>
          取消
        </button>
        <button className="btn btn-danger btn-sm" onClick={() => deleteTask(taskId)}>
          确认删除
        </button>
      </div>
    </>
  );
}

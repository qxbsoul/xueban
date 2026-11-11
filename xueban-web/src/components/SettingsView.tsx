import { useEffect, useState, type FormEvent } from 'react';
import { activeCustomModel, loadApiSettings, loadCustomModels, modelsForPreset, PRESET_OPTIONS, PROVIDER_PRESETS, type ApiPreset } from '../settings-storage';
import { testEmbeddingConnection } from '../api';
import {
  clearEmbeddingKey,
  DEFAULT_EMBEDDING,
  EMBEDDING_PROVIDERS,
  loadEmbeddingSettings,
  saveEmbeddingSettings,
  type EmbeddingProvider,
} from '../embedding-storage';
import { useWorkbench } from '../store';
import { loadToolSettings, saveToolSettings } from '../tool-storage';
import { effectiveProfile, STAGES, type SchoolStage } from '../student-profile';

type SettingsSection = 'general' | 'plan' | 'model' | 'embedding' | 'tools';

/** 套餐卡片的后备数据：接口未返回时也能渲染（正常情况下来自 /api/xueban-billing/plans）。 */
const planDate = (iso?: string): string => (iso ? new Date(iso).toLocaleDateString('zh-CN') : '—');

const FALLBACK_PLANS = [
  { key: 'basic' as const, label: '体验版', price_cents: 0, credits: 500, features: ['3 位预设 AI 教研员', '500 基础积分', '10,000 Token = 1 积分'] },
  { key: 'studio' as const, label: '工作室版', price_cents: 6600, credits: 3000, features: ['3000 积分 / 月', '多客户项目管理', '客户品牌课程资源库', '10,000 Token = 1 积分'] },
  { key: 'custom' as const, label: '专业定制版', price_cents: 0, features: ['专属 Skill 与教研员', '课程资源库迁移', '定制工作流与部署'], contact: true },
];

export default function SettingsView() {
  const { apiSettings, saveModelSettings, removeCustomModel, clearModelKey, billing, plans, refreshBilling, upgradePlan, toast, studentProfile, saveStudentProfile } = useWorkbench();
  const [section, setSection] = useState<SettingsSection>('general');
  const [upgradeTarget, setUpgradeTarget] = useState<'studio' | null>(null);
  const [showContact, setShowContact] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [modelForm, setModelForm] = useState(() => {
    const settings = loadApiSettings();
    return { preset: settings.preset, model: settings.model, baseUrl: settings.baseUrl };
  });
  const [customEntries, setCustomEntries] = useState(() => loadCustomModels());
  const [customId, setCustomId] = useState(() => loadApiSettings().customId ?? '');
  const [customLabel, setCustomLabel] = useState(() => {
    const settings = loadApiSettings();
    return settings.preset === 'custom' ? (activeCustomModel()?.label ?? '') : '';
  });
  const [confirmDeleteCustom, setConfirmDeleteCustom] = useState(false);
  const [profileStage, setProfileStage] = useState<SchoolStage>(studentProfile?.stage ?? 'junior');
  const [profileGrade, setProfileGrade] = useState<number>(studentProfile?.grade ?? 1);
  useEffect(() => {
    if (!studentProfile) return;
    setProfileStage(studentProfile.stage);
    setProfileGrade(studentProfile.grade);
  }, [studentProfile]);
  const profileEffective = studentProfile ? effectiveProfile(studentProfile) : null;
  const [feedback, setFeedback] = useState('');
  const [error, setError] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);
  const [embedding, setEmbedding] = useState(() => loadEmbeddingSettings());
  const [embeddingKey, setEmbeddingKey] = useState('');
  const [embeddingFeedback, setEmbeddingFeedback] = useState('');
  const [embeddingError, setEmbeddingError] = useState('');
  const [embeddingTesting, setEmbeddingTesting] = useState(false);
  const [toolSettings, setToolSettings] = useState(() => loadToolSettings());
  const [toolFeedback, setToolFeedback] = useState('');
  const hasKey = apiSettings.hasKey;
  const modelOptions = modelForm.preset === 'custom' ? [] : modelsForPreset(modelForm.preset).map(item => item.id);

  useEffect(() => {
    setApiKey('');
    setConfirmClear(false);
  }, [apiSettings]);

  useEffect(() => {
    setModelForm({ preset: apiSettings.preset, model: apiSettings.model, baseUrl: apiSettings.baseUrl });
    setCustomEntries(loadCustomModels());
    setCustomId(apiSettings.customId ?? '');
    if (apiSettings.preset === 'custom') setCustomLabel(activeCustomModel()?.label ?? '');
  }, [apiSettings.preset, apiSettings.model, apiSettings.baseUrl, apiSettings.customId]);

  const selectPreset = (preset: ApiPreset) => {
    setConfirmDeleteCustom(false);
    if (preset === 'custom') {
      const entries = loadCustomModels();
      const entry = activeCustomModel(entries);
      setCustomEntries(entries);
      setCustomId(entry?.id ?? '');
      setCustomLabel(entry?.label ?? '');
      setModelForm({ preset: 'custom', model: entry?.model ?? '', baseUrl: entry?.baseUrl ?? '' });
      return;
    }
    const presetDef = PROVIDER_PRESETS[preset];
    setCustomId('');
    setCustomLabel('');
    setModelForm({
      preset,
      model: presetDef.models[0].id,
      baseUrl: presetDef.baseUrl,
    });
  };

  const selectCustomEntry = (id: string) => {
    setConfirmDeleteCustom(false);
    setCustomId(id);
    if (!id) {
      setCustomLabel('');
      setModelForm(previous => ({ ...previous, model: '', baseUrl: '' }));
      return;
    }
    const entry = customEntries.find(item => item.id === id);
    if (!entry) return;
    setCustomLabel(entry.label);
    setModelForm(previous => ({ ...previous, model: entry.model, baseUrl: entry.baseUrl }));
  };

  const removeCustomEntry = () => {
    if (!customId) return;
    if (!confirmDeleteCustom) {
      setConfirmDeleteCustom(true);
      setFeedback('再次点击“确认删除”才会移除这个自定义模型。');
      return;
    }
    removeCustomModel(customId);
    setCustomId('');
    setConfirmDeleteCustom(false);
    setApiKey('');
    setFeedback('已删除这个自定义模型。');
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setFeedback('');
    setError('');
    const model = modelForm.model.trim();
    if (!model) {
      setError('请选择或填写模型名称。');
      return;
    }
    if (modelForm.preset === 'custom' && !modelForm.baseUrl.trim()) {
      setError('自定义模型必须填写 Base URL（例如 https://api.deepseek.com/v1）。');
      return;
    }
    try {
      saveModelSettings({
        mode: modelForm.preset === 'custom' ? 'custom' : 'official',
        preset: modelForm.preset,
        model,
        baseUrl: modelForm.preset === 'custom' ? modelForm.baseUrl.trim() : PROVIDER_PRESETS[modelForm.preset].baseUrl,
        apiKey,
        ...(modelForm.preset === 'custom' ? { label: customLabel.trim() || model, customId: customId || undefined } : {}),
      });
      setApiKey('');
      setShowKey(false);
      setConfirmDeleteCustom(false);
      setFeedback(modelForm.preset === 'custom'
        ? `自定义模型「${customLabel.trim() || model}」已保存；以后可以继续新建，互不覆盖。`
        : `${PROVIDER_PRESETS[modelForm.preset].label} 配置已保存到当前浏览器。`);
    } catch {
      setError('浏览器存储不可用，本次配置未保存。请检查隐私模式或存储权限。');
    }
  };

  const clearKey = () => {
    if (!confirmClear) {
      setConfirmClear(true);
      setFeedback('再次点击“确认清除”才会移除密钥。');
      return;
    }
    clearModelKey({ mode: apiSettings.mode, provider: apiSettings.provider });
    setApiKey('');
    setConfirmClear(false);
    setError('');
    setFeedback('密钥已从当前浏览器移除。');
  };

  const selectEmbeddingProvider = (provider: EmbeddingProvider) => {
    const preset = provider === 'custom' ? { baseUrl: '', models: [''] } : EMBEDDING_PROVIDERS[provider];
    setEmbedding(previous => ({
      ...previous, provider,
      baseUrl: provider === 'custom' ? previous.baseUrl : preset.baseUrl,
      model: provider === 'custom' ? previous.model : preset.models[0],
    }));
    setEmbeddingFeedback('');
    setEmbeddingError('');
  };
  const saveEmbedding = () => {
    setEmbeddingFeedback('');
    setEmbeddingError('');
    if (!embedding.baseUrl.trim() || !embedding.model.trim()) {
      setEmbeddingError('请填写完整的 Base URL 和模型名称。');
      return false;
    }
    if (!embedding.hasKey && !embeddingKey.trim()) {
      setEmbeddingError('请输入 Embedding API Key。');
      return false;
    }
    try {
      setEmbedding(saveEmbeddingSettings({ ...embedding, apiKey: embeddingKey }));
      setEmbeddingKey('');
      setEmbeddingFeedback('配置已保存；点击“测试连接”验证真实服务。');
      return true;
    } catch {
      setEmbeddingError('浏览器存储不可用，配置未保存。');
      return false;
    }
  };
  const testEmbedding = async () => {
    if (!saveEmbedding()) return;
    setEmbeddingTesting(true);
    setEmbeddingFeedback('');
    try {
      const result = await testEmbeddingConnection();
      setEmbeddingFeedback(`连接成功：${result.data.model}，返回 ${result.data.dimensions} 维向量。`);
    } catch (reason) {
      setEmbeddingError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setEmbeddingTesting(false);
    }
  };
  const saveTools = () => {
    saveToolSettings(toolSettings);
    setToolSettings(loadToolSettings());
    setToolFeedback('工具配置已保存。教研员还需在教研团队中获得对应工具权限。');
  };

  const confirmUpgrade = async () => {
    setUpgrading(true);
    try {
      await upgradePlan('studio');
      setUpgradeTarget(null);
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : '升级失败，请稍后重试');
    } finally {
      setUpgrading(false);
    }
  };

  return (
    <div className="view-inner settings-page">
      <div className="view-head"><h2>设置</h2></div>
      <div className="settings-shell">
        <nav className="panel settings-nav" aria-label="设置目录">
          <button type="button" className={section === 'general' ? 'settings-nav-item active' : 'settings-nav-item'} onClick={() => setSection('general')}>通用</button>
          <button type="button" className={section === 'plan' ? 'settings-nav-item active' : 'settings-nav-item'} onClick={() => { setSection('plan'); void refreshBilling(); }}>套餐与积分</button>
          <button type="button" className={section === 'model' ? 'settings-nav-item active' : 'settings-nav-item'} onClick={() => setSection('model')}>模型与 API</button>
          <button type="button" className={section === 'embedding' ? 'settings-nav-item active' : 'settings-nav-item'} onClick={() => setSection('embedding')}>课程资源库 Embedding</button>
          <button type="button" className={section === 'tools' ? 'settings-nav-item active' : 'settings-nav-item'} onClick={() => setSection('tools')}>工具与扩展</button>
        </nav>
        <div className="settings-content">
          {section === 'general' && (
            <section className="panel settings-main">
              <div className="settings-section-head"><h3>通用</h3></div>
              <div className="settings-form">
                <dl className="settings-facts general-facts">
                  <div><dt>应用</dt><dd>智慧学伴 学情工作台</dd></div>
                  <div><dt>运行环境</dt><dd>本地前后端联调</dd></div>
                </dl>
                <div className="settings-section-head" style={{ marginTop: 18 }}><h3>学习档案</h3></div>
                <div className="settings-grid">
                  <div className="form-group">
                    <label className="form-label" htmlFor="settings-stage">学段</label>
                    <select
                      id="settings-stage"
                      className="form-select"
                      value={profileStage}
                      onChange={event => {
                        const stage = event.target.value as SchoolStage;
                        setProfileStage(stage);
                        const max = STAGES.find(item => item.key === stage)?.grades ?? 1;
                        const grade = Math.min(profileGrade, max);
                        setProfileGrade(grade);
                        void saveStudentProfile(stage, grade);
                      }}
                    >
                      {STAGES.map(stage => <option key={stage.key} value={stage.key}>{stage.label}</option>)}
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label" htmlFor="settings-grade">年级</label>
                    <select
                      id="settings-grade"
                      className="form-select"
                      value={profileGrade}
                      onChange={event => {
                        const grade = Number(event.target.value);
                        setProfileGrade(grade);
                        void saveStudentProfile(profileStage, grade);
                      }}
                    >
                      {Array.from({ length: STAGES.find(item => item.key === profileStage)?.grades ?? 1 }, (_, index) => index + 1)
                        .map(grade => <option key={grade} value={grade}>{STAGES.find(item => item.key === profileStage)?.gradeName(grade)}</option>)}
                    </select>
                  </div>
                </div>
                <div className="settings-help">
                  {profileEffective
                    ? `当前：${profileEffective.stageLabel}${profileEffective.gradeName} · ${profileEffective.semester}。每年 9 月 1 日自动升一级，学期随日期自动切换。`
                    : '还没设置学段年级，教学主管会按通用难度回答；建议先设置。'}
                </div>
                <div className="settings-actions"><button type="button" className="btn btn-primary" onClick={() => setSection('model')}>管理模型与 API</button></div>
              </div>
            </section>
          )}
          {section === 'plan' && (
            <section className="panel settings-main plan-section">
              <div className="settings-section-head">
                <h3>套餐与积分</h3>
                <span className="settings-badge saved">当前：{billing?.plan_label ?? '读取中'}</span>
              </div>
              <div className="settings-form plan-body">
              <div className="plan-current">
                <div><span>当前套餐</span><b>{billing?.plan_label ?? '—'}</b></div>
                <div><span>本周期剩余</span><b>{billing ? `${billing.credits_remaining} / ${billing.credits_total} 积分` : '—'}</b></div>
                <div><span>已使用</span><b>{billing ? `${billing.percent_used}%` : '—'}</b></div>
                <div><span>本周期至</span><b>{billing ? planDate(billing.period_end) : '—'}</b></div>
              </div>
              {billing && <div className="credit-bar plan-bar"><i style={{ width: `${billing.percent_used}%` }} /></div>}

              <div className="plan-grid">
                {(plans.length ? plans : FALLBACK_PLANS).map(plan => {
                  const current = billing?.plan === plan.key;
                  const featured = plan.key === 'studio';
                  const price = plan.key === 'studio' ? '66RMB / 月' : plan.key === 'basic' ? '免费' : '定制';
                  return (
                    <article key={plan.key} className={'plan-card' + (featured ? ' featured' : '') + (current ? ' current' : '')}>
                      <header>
                        <h4>{plan.label}</h4>
                        {current && <span className="plan-current-tag">当前套餐</span>}
                      </header>
                      <div className="plan-price">{price}</div>
                      <ul className="plan-features">
                        {plan.features.map(feature => <li key={feature}>{feature}</li>)}
                      </ul>
                      {plan.key === 'custom' ? (
                        <button type="button" className="btn btn-ghost plan-action" onClick={() => setShowContact(true)}>联系开发者</button>
                      ) : current ? (
                        <button type="button" className="btn btn-ghost plan-action" disabled>当前套餐</button>
                      ) : plan.key === 'studio' ? (
                        <button type="button" className="btn btn-primary plan-action" onClick={() => setUpgradeTarget('studio')}>升级到工作室版</button>
                      ) : (
                        <button type="button" className="btn btn-ghost plan-action" onClick={() => void upgradePlan('basic').catch(() => toast('切换失败，请稍后重试'))}>切换到体验版</button>
                      )}
                    </article>
                  );
                })}
              </div>
              <div className="plan-note">套餐与积分按自然月发放；升级后本周期积分重置为新套餐额度。10,000 Token 折算 1 积分。</div>
              </div>
            </section>
          )}
          {section === 'model' && (
            <section className="panel settings-main">
              <div className="settings-section-head">
                <h3>模型与 API</h3>
                <span className={hasKey ? 'settings-badge saved' : 'settings-badge'}>{hasKey ? '密钥已保存' : '未配置密钥'}</span>
              </div>
              <form className="settings-form" onSubmit={submit} noValidate autoComplete="off">
                <div className="settings-grid">
                  <div className="form-group">
                    <label className="form-label" htmlFor="settings-provider">服务提供商</label>
                    <select id="settings-provider" className="form-select" value={modelForm.preset} onChange={event => selectPreset(event.target.value as ApiPreset)}>
                      {PRESET_OPTIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
                    </select>
                  </div>
                  {modelForm.preset === 'custom' ? (
                    <div className="form-group">
                      <label className="form-label" htmlFor="settings-custom-entry">已保存的自定义模型</label>
                      <select id="settings-custom-entry" className="form-select" value={customId} onChange={event => selectCustomEntry(event.target.value)}>
                        <option value="">＋ 新建自定义模型</option>
                        {customEntries.map(entry => <option key={entry.id} value={entry.id}>{entry.label}（{entry.model}）</option>)}
                      </select>
                    </div>
                  ) : (
                    <div className="form-group">
                      <label className="form-label" htmlFor="settings-model">模型</label>
                      <select id="settings-model" className="form-select" value={modelForm.model} onChange={event => setModelForm(previous => ({ ...previous, model: event.target.value }))}>
                        {modelsForPreset(modelForm.preset).map(model => <option key={model.id} value={model.id}>{model.label}</option>)}
                      </select>
                    </div>
                  )}
                </div>
                {modelForm.preset === 'custom' && (
                  <div className="settings-grid">
                    <div className="form-group">
                      <label className="form-label" htmlFor="settings-custom-label">名称</label>
                      <input
                        id="settings-custom-label"
                        className="form-input"
                        value={customLabel}
                        onChange={event => { setCustomLabel(event.target.value); setConfirmDeleteCustom(false); }}
                        placeholder="给这个模型起个名字，例如 教研组网关-DeepSeek"
                      />
                    </div>
                    <div className="form-group">
                      <label className="form-label" htmlFor="settings-custom-model">模型 ID</label>
                      <input
                        id="settings-custom-model"
                        className="form-input"
                        value={modelForm.model}
                        onChange={event => setModelForm(previous => ({ ...previous, model: event.target.value }))}
                        placeholder="例如 deepseek-flash"
                      />
                    </div>
                  </div>
                )}
                <div className="form-group">
                  <label className="form-label" htmlFor="settings-base-url">Base URL</label>
                  <input
                    id="settings-base-url"
                    className="form-input"
                    value={modelForm.baseUrl}
                    readOnly={modelForm.preset !== 'custom'}
                    onChange={event => setModelForm(previous => ({ ...previous, baseUrl: event.target.value }))}
                    placeholder={modelForm.preset === 'custom' ? 'https://api.deepseek.com/v1' : PROVIDER_PRESETS[modelForm.preset].baseUrl}
                  />
                  <div className="settings-help">
                    {modelForm.preset === 'custom'
                      ? '填写 OpenAI 兼容接口的 Base URL（通常以 /v1 结尾）。'
                      : `内置地址：${PROVIDER_PRESETS[modelForm.preset].baseUrl}`}
                  </div>
                </div>
                <div className="form-group">
                  <label className="form-label" htmlFor="settings-api-key">API Key</label>
                  <div className="settings-key-row">
                    <input
                      id="settings-api-key"
                      className="form-input"
                      type={showKey ? 'text' : 'password'}
                      value={apiKey}
                      onChange={event => { setApiKey(event.target.value); setConfirmClear(false); }}
                      placeholder={hasKey ? '已有密钥；留空保存则保持不变' : '输入你的 API Key'}
                      autoComplete="new-password"
                      spellCheck={false}
                    />
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowKey(value => !value)}>{showKey ? '隐藏' : '显示'}</button>
                  </div>
                  <div className="settings-help">密钥不会回显；仅保存在当前浏览器 localStorage，未加密，请勿在共享设备上使用。</div>
                </div>
                  {modelForm.preset === 'custom' && customEntries.length > 0 && (
                    <div className="form-group">
                      <label className="form-label">已保存的自定义 API</label>
                      <div className="settings-custom-list">
                        {customEntries.map(entry => (
                          <div key={entry.id} className={entry.id === customId ? 'settings-custom-item active' : 'settings-custom-item'}>
                            <div className="settings-custom-meta">
                              <strong>{entry.label}</strong>
                              <small>{entry.model} · {entry.baseUrl} · {entry.key ? '已保存密钥' : '无密钥'}</small>
                            </div>
                            <div className="settings-custom-actions">
                              <button type="button" className="btn btn-ghost btn-sm" onClick={() => selectCustomEntry(entry.id)}>使用</button>
                              <button
                                type="button"
                                className="btn btn-danger btn-sm"
                                onClick={() => {
                                  removeCustomModel(entry.id);
                                  setCustomEntries(loadCustomModels());
                                  if (entry.id === customId) {
                                    setCustomId('');
                                    setCustomLabel('');
                                    setModelForm(previous => ({ ...previous, model: '', baseUrl: '' }));
                                  }
                                  setFeedback(`已删除自定义 API「${entry.label}」。`);
                                }}
                              >
                                删除
                              </button>
                            </div>
                          </div>
                        ))}
                      </div>
                      <div className="settings-help">删除只影响这个浏览器里的配置，不会影响服务端或其他设备。</div>
                    </div>
                  )}
                  {modelForm.preset === 'custom' && (
                    <div className="settings-help">
                      保存后会出现在对话页的模型下拉里；可以继续点「新建自定义模型」添加多个，名称、Base URL、模型和 Key 各自保存，互不覆盖。
                    </div>
                  )}
                <div className="settings-notice">保存配置不代表已验证连接，首次真实对话时会向所选服务商发起请求。</div>
                {error && <div className="settings-feedback error" role="alert">{error}</div>}
                {feedback && <div className="settings-feedback" role="status">{feedback}</div>}
                <div className="settings-actions">
                  <button type="submit" className="btn btn-primary">保存配置</button>
                  {modelForm.preset === 'custom' && customId && (
                    <button type="button" className="btn btn-danger" onClick={removeCustomEntry}>
                      {confirmDeleteCustom ? '确认删除这个模型' : '删除这个模型'}
                    </button>
                  )}
                  <button type="button" className="btn btn-danger" disabled={!hasKey} onClick={clearKey}>{confirmClear ? '确认清除' : '清除密钥'}</button>
                  {confirmClear && <button type="button" className="btn btn-ghost" onClick={() => { setConfirmClear(false); setFeedback(''); }}>取消</button>}
                  {confirmDeleteCustom && <button type="button" className="btn btn-ghost" onClick={() => { setConfirmDeleteCustom(false); setFeedback(''); }}>取消删除</button>}
                </div>
              </form>
            </section>
          )}
          {section === 'embedding' && (
            <section className="panel settings-main">
              <div className="settings-section-head">
                <h3>课程资源库 Embedding</h3>
                <span className={embedding.hasKey ? 'settings-badge saved' : 'settings-badge'}>{embedding.hasKey ? '密钥已保存' : '未配置密钥'}</span>
              </div>
              <div className="settings-form">
                <div className="settings-grid">
                  <div className="form-group">
                    <label className="form-label" htmlFor="embedding-provider">服务提供商</label>
                    <select id="embedding-provider" className="form-select" value={embedding.provider} onChange={event => selectEmbeddingProvider(event.target.value as EmbeddingProvider)}>
                      {Object.entries(EMBEDDING_PROVIDERS).map(([id, preset]) => <option key={id} value={id}>{preset.label}</option>)}
                      <option value="custom">自定义 OpenAI 兼容接口</option>
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label" htmlFor="embedding-model">模型</label>
                    {embedding.provider === 'custom' ? (
                      <input id="embedding-model" className="form-input" value={embedding.model} onChange={event => setEmbedding(previous => ({ ...previous, model: event.target.value }))} placeholder="输入 Embedding 模型名称" />
                    ) : (
                      <select id="embedding-model" className="form-select" value={embedding.model} onChange={event => setEmbedding(previous => ({ ...previous, model: event.target.value }))}>
                        {EMBEDDING_PROVIDERS[embedding.provider].models.map(model => <option key={model} value={model}>{model}</option>)}
                      </select>
                    )}
                  </div>
                </div>
                <div className="form-group">
                  <label className="form-label" htmlFor="embedding-base-url">Base URL</label>
                  <input id="embedding-base-url" className="form-input" value={embedding.baseUrl} readOnly={embedding.provider !== 'custom'} onChange={event => setEmbedding(previous => ({ ...previous, baseUrl: event.target.value }))} placeholder={DEFAULT_EMBEDDING.baseUrl} />
                </div>
                <div className="form-group">
                  <label className="form-label" htmlFor="embedding-key">API Key</label>
                  <input id="embedding-key" className="form-input" type="password" value={embeddingKey} onChange={event => setEmbeddingKey(event.target.value)} placeholder={embedding.hasKey ? '已有密钥；留空保存则保持不变' : '输入你的 Embedding API Key'} autoComplete="new-password" spellCheck={false} />
                  <div className="settings-help">仅保存在当前浏览器 localStorage；Node 只在本次请求内转发，Python 只在内存中创建向量客户端，不写入数据库。</div>
                </div>
                <div className="settings-notice">上传文件时会生成真实向量；对话与课程资源库搜索将使用向量与关键词混合检索。更换模型后请使用新的课程资源库，避免向量维度不一致。</div>
                {embeddingError && <div className="settings-feedback error" role="alert">{embeddingError}</div>}
                {embeddingFeedback && <div className="settings-feedback" role="status">{embeddingFeedback}</div>}
                <div className="settings-actions">
                  <button type="button" className="btn btn-primary" onClick={saveEmbedding}>保存配置</button>
                  <button type="button" className="btn btn-ghost" disabled={embeddingTesting} onClick={() => void testEmbedding()}>{embeddingTesting ? '正在测试…' : '测试连接'}</button>
                  <button type="button" className="btn btn-danger" disabled={!embedding.hasKey} onClick={() => { setEmbedding(clearEmbeddingKey()); setEmbeddingKey(''); setEmbeddingFeedback('Embedding 密钥已清除，课程资源库将回退到关键词检索。'); setEmbeddingError(''); }}>清除密钥</button>
                </div>
              </div>
            </section>
          )}
          {section === 'tools' && (
            <section className="panel settings-main">
              <div className="settings-section-head"><h3>工具与扩展</h3><span className={toolSettings.tavilyKey || toolSettings.serpapiKey ? 'settings-badge saved' : 'settings-badge'}>{toolSettings.tavilyKey || toolSettings.serpapiKey ? '搜索已配置' : '搜索未配置'}</span></div>
              <div className="settings-form">
                <div className="settings-notice">可用工具：Web Search、网页读取、本地只读文件、GitHub 读取、消息读取和图片生成。教研员只会获得组织档案中明确授权的工具；Shell、写入、发布、邮件等高风险操作不会在此隐式开放。</div>
                <div className="form-group"><label className="form-label" htmlFor="tool-tavily">Tavily API Key（网页搜索，推荐）</label><input id="tool-tavily" className="form-input" type="password" value={toolSettings.tavilyKey} onChange={event => setToolSettings(previous => ({ ...previous, tavilyKey: event.target.value }))} autoComplete="new-password" /></div>
                <div className="form-group"><label className="form-label" htmlFor="tool-serpapi">SerpAPI Key（可选，优先于 Tavily）</label><input id="tool-serpapi" className="form-input" type="password" value={toolSettings.serpapiKey} onChange={event => setToolSettings(previous => ({ ...previous, serpapiKey: event.target.value }))} autoComplete="new-password" /></div>
                <div className="form-group"><label className="form-label" htmlFor="tool-github">GitHub Token（可选，用于 GitHub 读取）</label><input id="tool-github" className="form-input" type="password" value={toolSettings.githubToken} onChange={event => setToolSettings(previous => ({ ...previous, githubToken: event.target.value }))} autoComplete="new-password" /></div>
                <div className="settings-help">密钥仅保存在当前浏览器并随本次工具调用转发。未配置密钥时 Web Search 会用公开网页结果兜底（结果标注 provider: bing-html）；兜底抓不到时如实失败，不会伪造搜索结果。</div>
                {toolFeedback && <div className="settings-feedback" role="status">{toolFeedback}</div>}
                <div className="settings-actions"><button type="button" className="btn btn-primary" onClick={saveTools}>保存工具配置</button><button type="button" className="btn btn-danger" onClick={() => { const empty = { tavilyKey: '', serpapiKey: '', githubToken: '' }; saveToolSettings(empty); setToolSettings(empty); setToolFeedback('工具密钥已清除。'); }}>清除工具密钥</button></div>
              </div>
            </section>
          )}
        </div>
      </div>

      {upgradeTarget && (
        <div className="modal-mask show" onClick={event => { if (event.target === event.currentTarget) setUpgradeTarget(null); }}>
          <div className="modal" role="dialog" aria-modal="true" aria-label="升级套餐">
            <div className="modal-head">
              <h3>升级到工作室版</h3>
              <button type="button" className="modal-x" onClick={() => setUpgradeTarget(null)} aria-label="关闭" title="关闭">
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <div className="modal-body">
              <p className="plan-dialog-text">本次为模拟付款，不会真实扣款。确认后立即升级，本周期积分重置为 3000。</p>
              <dl className="settings-facts">
                <div><dt>套餐</dt><dd>工作室版</dd></div>
                <div><dt>金额</dt><dd>¥66.00 / 月</dd></div>
                <div><dt>积分</dt><dd>3000 / 月</dd></div>
              </dl>
            </div>
            <div className="modal-foot">
              <button type="button" className="btn btn-ghost" onClick={() => setUpgradeTarget(null)}>取消</button>
              <button type="button" className="btn btn-primary" disabled={upgrading} onClick={() => void confirmUpgrade()}>
                {upgrading ? '支付中…' : '确认支付并升级'}
              </button>
            </div>
          </div>
        </div>
      )}

      {showContact && (
        <div className="modal-mask show" onClick={event => { if (event.target === event.currentTarget) setShowContact(false); }}>
          <div className="modal" role="dialog" aria-modal="true" aria-label="专业定制版">
            <div className="modal-head">
              <h3>专业定制版</h3>
              <button type="button" className="modal-x" onClick={() => setShowContact(false)} aria-label="关闭" title="关闭">
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <div className="modal-body">
              <p className="plan-dialog-text">请与开发者联系。</p>
            </div>
            <div className="modal-foot">
              <button type="button" className="btn btn-primary" onClick={() => setShowContact(false)}>知道了</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

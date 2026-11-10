import { setByokKey, setDefaultProvider, type ByokProvider } from './api';

export type ApiConfigMode = 'official' | 'custom';
export type ApiPreset = 'deepseek' | 'custom';

export const DEEPSEEK_BASE_URL = 'https://api.deepseek.com/v1';
export const DEEPSEEK_DEFAULT_MODEL = 'deepseek-flash';

export interface ModelPreset {
  id: string;
  label: string;
}

export interface ProviderPreset {
  key: Exclude<ApiPreset, 'custom'>;
  label: string;
  baseUrl: string;
  models: ModelPreset[];
  /** 服务端内置了 Key，用户不填也能用。 */
  builtinKey?: boolean;
}

/** 内置的模型服务目录：DeepSeek 的常用模型与 Base URL。 */
export const PROVIDER_PRESETS: Record<Exclude<ApiPreset, 'custom'>, ProviderPreset> = {
  deepseek: {
    key: 'deepseek', label: 'DeepSeek', baseUrl: DEEPSEEK_BASE_URL,
    models: [
      { id: 'deepseek-flash', label: 'DeepSeek V4.1 Flash（推荐）' },
      { id: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro（官方逐步下线）' },
      { id: 'deepseek-reasoner', label: 'DeepSeek R1 推理' },
      { id: 'deepseek-chat', label: 'DeepSeek Chat 通用' },
    ],
  },
};

export const CUSTOM_PRESET = {
  key: 'custom' as const,
  label: '自定义（OpenAI 兼容）',
  baseUrl: '',
  models: [] as ModelPreset[],
};

export const PRESET_OPTIONS = [
  { value: 'deepseek' as const, label: 'DeepSeek' },
  { value: 'custom' as const, label: '自定义（OpenAI 兼容）' },
];

export interface ApiSettingsState {
  mode: ApiConfigMode;
  provider: ByokProvider;
  /** 设置页里选择的供应商（deepseek / 自定义）。 */
  preset: ApiPreset;
  model: string;
  baseUrl: string;
  hasKey: boolean;
  /** 自定义模型条目的 id（preset=custom 时有值）。 */
  customId?: string;
}

export interface ApiSettingsInput {
  mode: ApiConfigMode;
  preset: ApiPreset;
  model: string;
  baseUrl: string;
  apiKey: string;
  /** 自定义模型的显示名称（用户自己起名）。 */
  label?: string;
  /** 编辑已有的自定义模型条目；不传则新建。 */
  customId?: string;
}

/** 一条自定义模型配置：名字、模型 ID、Base URL、Key 各自独立保存，可并存多条。 */
export interface CustomModelEntry {
  id: string;
  label: string;
  model: string;
  baseUrl: string;
  key: string;
}

const CUSTOM_LIST_KEY = 'sf_custom_models';
const CUSTOM_ACTIVE_KEY = 'sf_custom_active';

export const PROVIDER_LABELS: Record<ByokProvider, string> = {
  anthropic: 'Anthropic Claude', openai: 'OpenAI', deepseek: 'DeepSeek', zhipu: '智谱 GLM',
  google: 'Google Gemini', qwen: '通义千问', moonshot: 'Moonshot AI',
  mistral: 'Mistral AI', groq: 'Groq', openrouter: 'OpenRouter', ollama: 'Ollama（本地）',
  lmstudio: 'LM Studio（本地）', azure: 'Azure OpenAI',
};

function read(key: string): string {
  try { return localStorage.getItem(key) ?? ''; } catch { return ''; }
}

function write(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch { /* 忽略隐私模式存储异常 */ }
}

export function modelsForPreset(preset: ApiPreset): ModelPreset[] {
  return preset === 'custom' ? [] : PROVIDER_PRESETS[preset].models;
}

function isCustomEntry(value: unknown): value is CustomModelEntry {
  if (!value || typeof value !== 'object') return false;
  const entry = value as Record<string, unknown>;
  return typeof entry.id === 'string' && typeof entry.model === 'string' && typeof entry.baseUrl === 'string';
}

/**
 * 已保存的自定义模型列表。旧版本只有一个「自定义」槽位（sf_custom_*），
 * 首次读取时迁移成一条独立条目，之后可以继续新建而不覆盖。
 */
export function loadCustomModels(): CustomModelEntry[] {
  try {
    const parsed = JSON.parse(read(CUSTOM_LIST_KEY) || '[]');
    if (Array.isArray(parsed) && parsed.length > 0) {
      return parsed.filter(isCustomEntry).map(entry => ({
        id: entry.id,
        label: entry.label || entry.model || '自定义模型',
        model: entry.model,
        baseUrl: entry.baseUrl,
        key: typeof entry.key === 'string' ? entry.key : '',
      }));
    }
  } catch { /* 数据损坏时走下面的迁移/空列表 */ }
  const legacyModel = read('sf_custom_model').trim();
  const legacyBase = read('sf_custom_base_url').trim();
  if (legacyModel || legacyBase) {
    const legacy: CustomModelEntry = {
      id: 'cm_legacy',
      label: legacyModel || '自定义模型',
      model: legacyModel,
      baseUrl: legacyBase,
      key: read('sf_custom_api_key'),
    };
    saveCustomModels([legacy]);
    write(CUSTOM_ACTIVE_KEY, legacy.id);
    return [legacy];
  }
  return [];
}

export function saveCustomModels(entries: CustomModelEntry[]): void {
  write(CUSTOM_LIST_KEY, JSON.stringify(entries));
}

export function activeCustomModel(entries: CustomModelEntry[] = loadCustomModels()): CustomModelEntry | null {
  const id = read(CUSTOM_ACTIVE_KEY);
  return entries.find(entry => entry.id === id) ?? entries[0] ?? null;
}

export function selectCustomModel(id: string): ApiSettingsState {
  const entry = loadCustomModels().find(item => item.id === id);
  if (!entry) return loadApiSettings();
  write(CUSTOM_ACTIVE_KEY, entry.id);
  write('sf_model_preset', 'custom');
  write('sf_api_config_mode', 'custom');
  write('sf_selected_model', entry.model);
  return loadApiSettings();
}

export function deleteCustomModel(id: string): ApiSettingsState {
  const entries = loadCustomModels();
  const remaining = entries.filter(entry => entry.id !== id);
  saveCustomModels(remaining);
  if (read(CUSTOM_ACTIVE_KEY) === id) write(CUSTOM_ACTIVE_KEY, remaining[0]?.id ?? '');
  // 删除旧版迁移条目时，顺手清掉遗留的单槽位字段，避免下次又被迁移回来。
  if (id === 'cm_legacy') {
    write('sf_custom_model', '');
    write('sf_custom_base_url', '');
    write('sf_custom_api_key', '');
  }
  return loadApiSettings();
}

/** 在所有供应商里找某个模型；对话页的「所有可选模型」下拉用 custom:<id> 标识自定义条目。 */
export function findModelPreset(value: string): { preset: ApiPreset; model: string; baseUrl: string; customId?: string } | null {
  if (value.startsWith('custom:')) {
    const entry = loadCustomModels().find(item => item.id === value.slice('custom:'.length));
    if (entry) return { preset: 'custom', model: entry.model, baseUrl: entry.baseUrl, customId: entry.id };
    return null;
  }
  for (const preset of Object.values(PROVIDER_PRESETS)) {
    if (preset.models.some(model => model.id === value)) {
      return { preset: preset.key, model: value, baseUrl: preset.baseUrl };
    }
  }
  return null;
}

function resolvePreset(): ApiPreset {
  const raw = read('sf_model_preset');
  if (raw === 'deepseek' || raw === 'custom') return raw;
  return read('sf_api_config_mode') === 'custom' ? 'custom' : 'deepseek';
}

export function loadApiSettings(): ApiSettingsState {
  const preset = resolvePreset();
  if (preset === 'custom') {
    const entry = activeCustomModel();
    if (entry) {
      return {
        mode: 'custom', provider: 'openai', preset, model: entry.model,
        baseUrl: entry.baseUrl, hasKey: Boolean(entry.key), customId: entry.id,
      };
    }
    // 一条自定义模型都还没保存时，退回 DeepSeek，避免对话页出现空模型。
    return { mode: 'official', provider: 'deepseek', preset: 'deepseek', model: DEEPSEEK_DEFAULT_MODEL, baseUrl: DEEPSEEK_BASE_URL, hasKey: Boolean(read('sf_deepseek_key')) };
  }
  const providerPreset = PROVIDER_PRESETS[preset];
  const stored = read('sf_selected_model');
  const model = providerPreset.models.some(item => item.id === stored) ? stored : providerPreset.models[0].id;
  return {
    mode: 'official', provider: providerPreset.key, preset, model, baseUrl: providerPreset.baseUrl,
    hasKey: providerPreset.builtinKey ? true : Boolean(read(`sf_${providerPreset.key}_key`)),
  };
}

export function saveApiSettings(input: ApiSettingsInput): ApiSettingsState {
  if (input.preset === 'custom') {
    const model = input.model.trim();
    const baseUrl = input.baseUrl.trim();
    if (!model || !baseUrl) throw new Error('自定义模型需要填写模型名称和 Base URL');
    const entries = loadCustomModels();
    const existing = input.customId ? entries.find(entry => entry.id === input.customId) : undefined;
    const entry: CustomModelEntry = existing
      ? {
          ...existing,
          label: input.label?.trim() || existing.label || model,
          model,
          baseUrl,
          key: input.apiKey.trim() || existing.key,
        }
      : {
          id: `cm_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
          label: input.label?.trim() || model,
          model,
          baseUrl,
          key: input.apiKey.trim(),
        };
    saveCustomModels(existing ? entries.map(item => item.id === entry.id ? entry : item) : [...entries, entry]);
    write(CUSTOM_ACTIVE_KEY, entry.id);
    write('sf_model_preset', 'custom');
    write('sf_api_config_mode', 'custom');
    write('sf_selected_model', model);
    return loadApiSettings();
  }
  write('sf_model_preset', input.preset);
  write('sf_api_config_mode', 'official');
  setDefaultProvider(input.preset);
  if (input.apiKey.trim()) setByokKey(input.preset as ByokProvider, input.apiKey.trim());
  write('sf_selected_model', input.model.trim() || PROVIDER_PRESETS[input.preset].models[0].id);
  return loadApiSettings();
}

export function hasStoredApiKey(mode: ApiConfigMode, provider: ByokProvider): boolean {
  return mode === 'custom'
    ? Boolean(activeCustomModel()?.key)
    : Boolean(read(`sf_${provider}_key`));
}

export function clearApiKey(target: { mode: ApiConfigMode; provider: ByokProvider }): ApiSettingsState {
  if (target.mode === 'custom') {
    const entries = loadCustomModels();
    const active = activeCustomModel(entries);
    if (active) saveCustomModels(entries.map(entry => entry.id === active.id ? { ...entry, key: '' } : entry));
  } else {
    setByokKey(target.provider, '');
  }
  return loadApiSettings();
}

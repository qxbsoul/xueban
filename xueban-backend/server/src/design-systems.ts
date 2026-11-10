/**
 * design-systems.ts — Design System registry (Story 15.5 + Story 15.11)
 *
 * Story 15.5 introduced 4 hardcoded Design Systems (tailwind / material /
 * shadcn / none) with `injection_prompt` strings appended to a skill's
 * system_prompt at run-time (see routes/run-sessions.ts).
 *
 * Story 15.11 layers FS-loading on top:
 *   - The 4 built-ins live in `HARDCODED_DS` as the immutable baseline.
 *   - At boot, `seedBuiltinDesignSystems()` writes them to
 *     `.xueban/design-systems/*.md` so users can edit (idempotent).
 *   - `reloadDesignSystems()` scans that dir and merges:  FS-loaded entries
 *     OVERRIDE same-id built-ins (AC1).
 *   - `DESIGN_SYSTEMS` (exported) is a MUTABLE Record<string, DesignSystem>
 *     that always reflects the latest merge — preserves the existing import
 *     shape used by routes/run-sessions.ts and the route layer.
 *
 * `compatible_skills` filters which skills the DS surfaces for in the
 * DesignSystemPicker UI. AC3 semantics: an empty array means "compatible with
 * all skills". Built-ins ship with explicit non-empty arrays for backward
 * compatibility with the Story 15.5 contract (web-prototype only, etc.).
 */

import {
  loadDesignSystemsFromFs,
  seedBuiltinDesignSystems as seedBuiltinDesignSystemsImpl,
} from './loaders/design-system-loader';

export interface DesignSystem {
  ds_id: string;
  name: string;
  description: string;
  compatible_skills: string[];
  /** Appended to skill.system_prompt at run-time. Empty string for "none". */
  injection_prompt: string;
}

/**
 * Story 15.5 baseline. Story 15.11 renamed `DESIGN_SYSTEMS` → `HARDCODED_DS`
 * and made the live registry mutable so FS overrides can land at runtime.
 * The 4 entries here are the safety net: if the FS dir is wiped, the server
 * still serves these.
 */
export const HARDCODED_DS: Record<string, DesignSystem> = {
  tailwind: {
    ds_id: 'tailwind',
    name: 'Tailwind CSS',
    description: 'Utility-first CSS framework，CDN 版本可直接使用',
    compatible_skills: ['web-prototype'],
    injection_prompt: `## Design System: Tailwind CSS

使用 CDN 版本（生成的 HTML 必须包含此 script tag）：
<script src="https://cdn.tailwindcss.com"></script>

颜色规范：
- 主色：blue-600 (#2563EB)
- 背景：gray-950 或 white
- 文字：gray-900（浅色主题）/ gray-50（深色主题）
- 组件：使用 Tailwind utility classes，不要写自定义 CSS

布局规范：
- 容器：max-w-7xl mx-auto px-4 sm:px-6 lg:px-8
- 响应式：mobile-first，sm:/md:/lg: 前缀
- 间距：统一使用 Tailwind spacing scale（4px 基准）

代码示例（Button）：
<button class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg font-medium transition-colors">
  按钮文字
</button>

必须通过 <script src="https://cdn.tailwindcss.com"></script> 引入，生成的 HTML 必须可以独立运行。`,
  },

  material: {
    ds_id: 'material',
    name: 'Material Design 3',
    description: 'Google Material Design 3，适合 SaaS 产品',
    compatible_skills: ['web-prototype'],
    injection_prompt: `## Design System: Material Design 3

引入 Roboto 字体（CDN）：
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap">

颜色规范（M3 Baseline）：
- Primary: #6750A4
- Surface: #FFFBFE
- On-Primary: #FFFFFF
- 使用 Material elevation（box-shadow）表达层次

圆角：
- small=4px, medium=12px, large=16px, extra-large=28px

排版：Roboto 字体；Display / Headline / Title / Body / Label 类型层级。
组件风格保持 Material 3 的 expressive 语言（filled / tonal / outlined / text 按钮分级）。`,
  },

  shadcn: {
    ds_id: 'shadcn',
    name: 'shadcn/ui 风格',
    description: '现代简洁的深色 UI，Vercel/Linear 风格',
    compatible_skills: ['web-prototype'],
    injection_prompt: `## Design System: shadcn/ui 风格（纯 CSS 实现）

颜色（深色主题 CSS 变量）：
:root {
  --background: 0 0% 3.9%;
  --foreground: 0 0% 98%;
  --primary: 0 0% 98%;
  --primary-foreground: 0 0% 9%;
  --border: 0 0% 14.9%;
  --radius: 0.5rem;
}

风格规则：
- 背景 #0a0a0a，卡片 #1a1a1a，边框 #262626
- 圆角 6-8px，阴影极简（1px 边框代替阴影）
- 字体：system-ui / -apple-system
- 不使用彩色强调色，全黑白灰色调
- 按钮：白底黑字（primary）或黑边透明（ghost）`,
  },

  none: {
    ds_id: 'none',
    name: '无约束',
    description: '让 Claude 自由发挥样式风格',
    compatible_skills: ['web-prototype', 'report', 'agent-team-blueprint'],
    injection_prompt: '',
  },
};

/**
 * The LIVE registry used by routes and tests. Starts as a clone of
 * HARDCODED_DS so existing Story 15.5 behaviour is preserved before the first
 * reload. `reloadDesignSystems()` rebuilds this from
 * `HARDCODED_DS ∪ FS-loaded` (FS overrides) on every call.
 *
 * Kept as `Record<string, DesignSystem>` for compatibility with
 * routes/run-sessions.ts (`DESIGN_SYSTEMS[id]` indexing) and the existing
 * Story 15.5 test (`design-systems.test.ts`).
 */
export let DESIGN_SYSTEMS: Record<string, DesignSystem> = { ...HARDCODED_DS };

/** Last reload's error list (exposed for /reload response). */
let lastLoadErrors: Array<{ file: string; reason: string }> = [];

/**
 * Resolve a design_system_id to its injection prompt suffix.
 * Returns '' for unknown ids or 'none' (caller should not append a separator).
 */
export function getInjectionPrompt(
  ds_id: string | undefined | null,
): string {
  if (!ds_id) return '';
  const ds = DESIGN_SYSTEMS[ds_id];
  return ds?.injection_prompt ?? '';
}

/** Public re-export so server/index.ts can call this on boot (AC5). */
export function seedBuiltinDesignSystems(
  dsDirOverride?: string,
): { written: string[]; skipped: string[] } {
  return seedBuiltinDesignSystemsImpl(
    Object.values(HARDCODED_DS),
    dsDirOverride,
  );
}

/**
 * Rebuild `DESIGN_SYSTEMS` from `HARDCODED_DS ∪ FS-loaded`.
 * FS entries override same-id built-ins (AC1, FS-priority merge).
 *
 * @param dsDirOverride  test-only: redirect the FS scan target
 */
export function reloadDesignSystems(dsDirOverride?: string): {
  reloaded: number;
  failed: number;
  errors: Array<{ file: string; reason: string }>;
  overrides: string[];
} {
  const result = loadDesignSystemsFromFs(dsDirOverride);

  const next: Record<string, DesignSystem> = { ...HARDCODED_DS };
  const overrides: string[] = [];

  for (const fs of result.loaded) {
    if (next[fs.ds_id]) {
      overrides.push(fs.ds_id);
    }
    // 2026-05-11 review P1-4: 'none' 是 15.5 决议的"literally nothing"语义，
    // injection_prompt 必须保持空字符串。seed 文件的 voice 占位段会被反向
    // 注入到 system prompt 破坏 15.5 契约 — 强制保留 HARDCODED_DS.none。
    if (fs.ds_id === 'none') {
      next.none = {
        ...HARDCODED_DS.none,
        // 允许 FS 修改名称/描述/兼容列表，但 injection_prompt 锁死为空。
        name: fs.name || HARDCODED_DS.none.name,
        description: fs.description || HARDCODED_DS.none.description,
        compatible_skills: fs.compatible_skills.length > 0
          ? fs.compatible_skills
          : HARDCODED_DS.none.compatible_skills,
        injection_prompt: '',
      };
      continue;
    }
    // Strip loader-only fields (detected_sections, source, source_path) so the
    // shape going to routes/run-sessions remains the lean DesignSystem shape.
    next[fs.ds_id] = {
      ds_id: fs.ds_id,
      name: fs.name,
      description: fs.description,
      compatible_skills: fs.compatible_skills,
      injection_prompt: fs.injection_prompt,
    };
  }

  DESIGN_SYSTEMS = next;
  lastLoadErrors = result.errors;

  for (const id of overrides) {
    console.log(`[design-system-loader] override hardcoded DS: ${id}`);
  }

  return {
    reloaded: result.loaded.length,
    failed: result.errors.length,
    errors: result.errors,
    overrides,
  };
}

/** Read-only accessor for the most recent reload's errors (diagnostic). */
export function getLastLoadErrors(): Array<{ file: string; reason: string }> {
  return lastLoadErrors;
}

/**
 * List DS, optionally filtered by skill compatibility (AC3).
 * `compatible_skills: []` means "compatible with all".
 */
export function listDesignSystems(skillId?: string): DesignSystem[] {
  const all = Object.values(DESIGN_SYSTEMS);
  if (!skillId) return all;
  return all.filter(
    (ds) =>
      ds.compatible_skills.length === 0 ||
      ds.compatible_skills.includes(skillId),
  );
}

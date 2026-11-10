/**
 * rework-detect.ts — conditional rework 的零 LLM 驳回判定（2026-11-05）。
 *
 * 背景：DAG 里 conditional 边（如 QA→Dev）= 「评审不通过 → 返工」的回归边，
 * dag-layers 已把它排除出前向拓扑。本模块给执行器提供确定性判定：读评审
 * agent 的产出文本，命中任一驳回标记 → 该边触发返工。
 *
 * 为什么零 LLM：判定本身不值得一次模型调用（贵、慢、且不可复现）。约定
 * 结构化标记 `[verdict:reject]` / `NEEDS_REWORK` 为硬信号，中文自然语
 * （驳回/打回/不通过…）为软信号——评审 agent 的 soul 里写明输出约定即可
 * 稳定命中；都不命中视为通过（宁可少返工，不无限循环）。
 */

/** 硬信号：结构化标记（大小写不敏感）。 */
export const REWORK_MARKERS: readonly string[] = [
  '[verdict:reject]',
  '[verdict: reject]',
  'needs_rework',
  'needs-rework',
  // 软信号：中文自然语（评审意见常用词）
  '驳回',
  '打回',
  '不通过',
  '未通过',
  '需要返工',
  '需返工',
  '请返工',
  '退回重做',
];

/** 判定一段评审文本是否为「驳回/需返工」。空文本 = 通过（没意见）。 */
export function detectRejection(text: string): boolean {
  const t = (text || '').toLowerCase();
  if (!t.trim()) return false;
  return REWORK_MARKERS.some((m) => t.includes(m.toLowerCase()));
}

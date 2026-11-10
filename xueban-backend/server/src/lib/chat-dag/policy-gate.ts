/**
 * policy-gate.ts — chat DAG 执行器的纯 policy 查表(批 2 Phase 2a)。
 *
 * matrix[sender][receiver] ∈ {permit,deny,warn}。缺项默认 permit(不过度拦截);
 * 未知值归一为 permit。Phase 2a 只消费 permit/deny/warn 三态;审核关卡/驳回(🔶/↩️)
 * 留给 Phase 3。
 */
export type GateVerdict = 'permit' | 'deny' | 'warn';

export function gate(
  matrix: Record<string, Record<string, string>> | null | undefined,
  from: string,
  to: string,
): GateVerdict {
  const v = matrix?.[from]?.[to];
  if (v === 'deny') return 'deny';
  if (v === 'warn') return 'warn';
  return 'permit';
}

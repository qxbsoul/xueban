/**
 * memory-bank.ts — 多模态记忆库检索辅助（对话框知识库接线，2026-11-06）。
 *
 * run-sessions / groups-chat 组装提示词前调用 knowledgeBlock()：按用户消息
 * 去 Python `/api/memory-bank/packs/{id}/query` 检索命中，渲染成「知识库参考」
 * 文本块，与经验记忆块并列注入 skill 层尾部，让 Agent 回答真正引用知识库内容。
 *
 * fail-soft 契约（与经验记忆块一致）：Python 未起 / 超时 / 单包失败 → 跳过该包；
 * 全军覆没或零命中 → 返回 ''，绝不阻塞 run。
 */
import { PYTHON_BACKEND_URL } from '../python-backend';

export interface MemoryBankHitLite {
  text: string;
  source: string;
  score: number;
}

/** 每包命中条数（对话场景 4 条够用，控上下文长度）。 */
const TOP_K = 4;
/** 单包检索超时（毫秒）。记忆库是 sidecar，挂了不该拖死 run 启动。 */
const TIMEOUT_MS = 15_000;
/** pack id 数量上限（防御：别让一个 payload 拖一串请求）。 */
const MAX_PACKS = 8;
/** 单条命中文本截断（字符）。 */
const HIT_TEXT_CAP = 400;
/** 整块渲染上限（字符），超限丢尾部命中。 */
const BLOCK_CAP = 4000;

/** 请求体/会话字段里的 pack id 数组 → 干净的 id 列表（非法项丢弃）。 */
export function coercePackIds(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    if (typeof item !== 'string') continue;
    const id = item.trim();
    if (!id || id.length > 200) continue;
    if (!out.includes(id)) out.push(id);
    if (out.length >= MAX_PACKS) break;
  }
  return out;
}

/**
 * 对每个 pack 发一次 top_k 检索，聚平命中（按 score 降序）。
 * 单包失败只跳过该包；查询串空 / 无 pack → 直接 []。
 */
export async function fetchMemoryBankHits(
  packIds: string[],
  query: string,
  extraHeaders: Record<string, string> = {},
): Promise<MemoryBankHitLite[]> {
  const q = query.trim();
  if (!q || packIds.length === 0) return [];
  const hits: MemoryBankHitLite[] = [];
  await Promise.all(
    packIds.map(async (packId) => {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
        const res = await fetch(
          `${PYTHON_BACKEND_URL}/api/memory-bank/packs/${encodeURIComponent(packId)}/query`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...extraHeaders },
            body: JSON.stringify({ query: q, top_k: TOP_K }),
            signal: controller.signal,
          },
        );
        clearTimeout(timer);
        if (!res.ok) return;
        const body = (await res.json()) as {
          data?: { hits?: Array<{ text?: unknown; source?: unknown; score?: unknown }> };
        };
        for (const h of body.data?.hits ?? []) {
          if (typeof h.text !== 'string' || !h.text.trim()) continue;
          hits.push({
            text: h.text.trim(),
            source: typeof h.source === 'string' && h.source ? h.source : packId,
            score: typeof h.score === 'number' ? h.score : 0,
          });
        }
      } catch {
        /* fail-soft：单包超时/网络错不阻塞整体 */
      }
    }),
  );
  hits.sort((a, b) => b.score - a.score);
  return hits;
}

/** 命中列表 → 「知识库参考」块；零命中返回 ''。 */
export function renderKnowledgeBlock(hits: MemoryBankHitLite[]): string {
  if (hits.length === 0) return '';
  const lines: string[] = [
    '## 知识库参考（记忆库检索命中，回答时优先引用；引用时注明「来源」）',
    '',
  ];
  for (const h of hits) {
    const text =
      h.text.length > HIT_TEXT_CAP ? `${h.text.slice(0, HIT_TEXT_CAP)}…` : h.text;
    lines.push(`- （来源：${h.source}）${text}`);
  }
  let block = lines.join('\n');
  if (block.length > BLOCK_CAP) block = `${block.slice(0, BLOCK_CAP)}\n…（已截断）`;
  return block;
}

/** 检索 + 渲染一把梭（fail-soft，异常返回 ''）。 */
export async function knowledgeBlock(
  packIds: string[] | undefined,
  query: string,
  extraHeaders: Record<string, string> = {},
): Promise<string> {
  try {
    const hits = await fetchMemoryBankHits(packIds ?? [], query, extraHeaders);
    return renderKnowledgeBlock(hits);
  } catch {
    return '';
  }
}

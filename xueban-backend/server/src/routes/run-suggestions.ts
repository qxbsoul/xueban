/**
 * routes/run-suggestions.ts — GET /api/runs/:runId/suggestions（2026-08-23）
 *
 * 完成态「后续建议」：取该 run 的 goal / 产物摘要，经 transport/api-clients
 * 的 callProvider（与 routes/llm.ts 同一 LLM 代理通道）让模型产出 2-4 条
 * 简短中文建议。LLM 不可用 / 失败 / 解析为空 → 502 + 空数组，严禁静态假建议。
 *
 * run 上下文来源（与 runs.ts 的 graph/task-tree 投影同一取数路径）：
 *   1. storage/runs 的 RunRecord（skill-assembler 持久化的 run，按 run_id
 *      或 session_id 匹配——前端 chat 路径手里的 id 是 session_id）；
 *   2. run-sessions.getRunReplay 的 daemon 事件日志（chat 主路径 run），
 *      经 lib/run-projection.foldTimelineEvents 折叠成终态时间线再提取。
 *
 * 路由只做 HTTP shape 转换；prompt / 解析 / LLM 编排在 lib/run-suggestions.ts。
 * createRunSuggestionsRouter(deps) 支持注入 generate，测试可 mock LLM。
 */

import { Router, Request, Response } from 'express';
import { listRuns } from '../storage/runs';
import { getRunReplay } from './run-sessions';
import { foldTimelineEvents } from '../lib/run-projection';
import {
  generateRunSuggestions,
  suggestionContextFromTimeline,
  type RunSuggestionContext,
} from '../lib/run-suggestions';
import { DEFAULT_MODELS } from '../transport/api-clients';
import { resolveProviderKey } from '../transport/byokKey';

export interface RunSuggestionsDeps {
  generate: (
    ctx: RunSuggestionContext,
    headers: Record<string, string | string[] | undefined>,
    signal: AbortSignal,
  ) => Promise<string[] | null>;
}

/** 默认生成路径：zhipu GLM（对齐前端 src/api/llm.ts 一句话人设的 provider 选择）。 */
async function defaultGenerate(
  ctx: RunSuggestionContext,
  headers: Record<string, string | string[] | undefined>,
  signal: AbortSignal,
): Promise<string[] | null> {
  return generateRunSuggestions({
    ctx,
    provider: 'zhipu',
    model: DEFAULT_MODELS.zhipu,
    apiKey: resolveProviderKey('zhipu', headers),
    signal,
  });
}

/** 汇总 RunRecord + daemon 事件日志 → 建议上下文；两者皆无 → null（404）。 */
function collectSuggestionContext(runId: string): RunSuggestionContext | null {
  const record = listRuns().find((r) => r.run_id === runId || r.session_id === runId);
  const replay = getRunReplay(runId);
  if (!record && !replay) return null;

  let ctx: RunSuggestionContext = {
    goal: record?.goal ?? '',
    artifacts: [],
  };
  if (replay) {
    const fromTimeline = suggestionContextFromTimeline(
      foldTimelineEvents(replay.events).values(),
    );
    if (!ctx.goal) ctx = { ...ctx, goal: fromTimeline.goal };
    ctx = {
      ...ctx,
      artifacts: fromTimeline.artifacts,
      ...(fromTimeline.summary ? { summary: fromTimeline.summary } : {}),
    };
  }
  // RunRecord 的最终产物并入（去重，record 在前——它是持久化的交付真相）。
  if (record?.artifact_filename && !ctx.artifacts.includes(record.artifact_filename)) {
    ctx = { ...ctx, artifacts: [record.artifact_filename, ...ctx.artifacts] };
  }
  return ctx;
}

export function createRunSuggestionsRouter(deps?: Partial<RunSuggestionsDeps>): Router {
  const generate = deps?.generate ?? defaultGenerate;
  const router = Router();

  router.get('/:runId/suggestions', async (req: Request, res: Response) => {
    const ctx = collectSuggestionContext(req.params.runId);
    if (!ctx) {
      res.status(404).json({ error: `Run ${req.params.runId} not found` });
      return;
    }
    const ac = new AbortController();
    req.on('close', () => ac.abort());
    try {
      const suggestions = await generate(ctx, req.headers, ac.signal);
      if (!suggestions || suggestions.length === 0) {
        res.status(502).json({ error: 'suggestions_unavailable', suggestions: [] });
        return;
      }
      res.json({ suggestions });
    } catch (err) {
      res.status(502).json({
        error: 'suggestions_unavailable',
        message: (err as Error).message,
        suggestions: [],
      });
    }
  });

  return router;
}

export default createRunSuggestionsRouter();

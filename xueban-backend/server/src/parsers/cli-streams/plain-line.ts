/**
 * plain-line.ts — Catch-all CLI stdout parser (Story 15.19 v2)
 *
 * Strategy:
 *  - Accumulate raw text from stdout.
 *  - Feed each chunk through `parseAndExtract()` so any embedded `<sf:*>` tags
 *    or `<artifact>` blocks the user's CLI prompt produced are still picked
 *    up — this keeps the contract identical to `anthropic-direct`.
 *  - On stream close, drain any residual buffer and emit a synthetic
 *    `<sf:complete>` if the underlying CLI didn't include one.
 *
 * Used as the fallback for: gemini, qwen-coder, cline, aider, cursor,
 * windsurf-cli, and (temporarily) cursor-acp until Story 15.23 lands.
 */

import type { Readable } from 'node:stream';
import { parseAndExtract, type SseEvent } from '../../parser';

export interface CliStreamArtifactCb {
  (filename: string, content: string, type: string): void;
}

/**
 * Generic stream parser. Treats incoming chunks as plain UTF-8 text; defers
 * `<sf:*>` extraction to the existing `parseAndExtract` so the SSE shape is
 * uniform across all executors.
 */
export async function* parsePlainLine(
  stdout: Readable,
  sessionId: string,
  artifactCb: CliStreamArtifactCb,
): AsyncGenerator<SseEvent> {
  let buffer = '';
  let sawComplete = false;

  for await (const chunk of stdout) {
    buffer += chunk.toString('utf8');
    const { buffer: remaining, events } = parseAndExtract(buffer, sessionId, artifactCb);
    buffer = remaining;
    for (const e of events) {
      if (e.event === 'complete') sawComplete = true;
      yield e;
    }
  }

  // Drain residual buffer once stream closes.
  if (buffer.trim()) {
    const { events } = parseAndExtract(buffer, sessionId, artifactCb);
    for (const e of events) {
      if (e.event === 'complete') sawComplete = true;
      yield e;
    }
  }

  // Synthesize complete if CLI never emitted one — keeps front-end happy.
  if (!sawComplete) {
    yield {
      event: 'complete',
      data: {
        session_id: sessionId,
        run_id: `run-${sessionId.slice(0, 8)}`,
        redirect: `/editor?session=${sessionId}`,
      },
    };
  }
}

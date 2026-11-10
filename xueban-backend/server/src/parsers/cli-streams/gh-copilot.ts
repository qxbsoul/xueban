/**
 * gh-copilot.ts — `gh copilot` extension stdout parser (Story 15.19 v2)
 *
 * `gh copilot suggest` prints natural-language responses with no structured
 * format. We:
 *  - emit a synthetic "running" step on first byte (so the front-end shows
 *    progress even though Copilot doesn't tell us when it's working)
 *  - feed text through `parseAndExtract` so any `<sf:*>` tags the user's
 *    prompt convinced Copilot to emit are still picked up
 *  - on EOF, emit a "done" step + synthetic complete if not already seen.
 */

import type { Readable } from 'node:stream';
import { parseAndExtract, type SseEvent } from '../../parser';
import type { CliStreamArtifactCb } from './plain-line';

const STEP_NAME = 'gh-copilot 生成中';

export async function* parseGhCopilot(
  stdout: Readable,
  sessionId: string,
  artifactCb: CliStreamArtifactCb,
): AsyncGenerator<SseEvent> {
  let buffer = '';
  let emittedRunning = false;
  let sawComplete = false;
  const start = Date.now();

  for await (const chunk of stdout) {
    if (!emittedRunning) {
      emittedRunning = true;
      yield {
        event: 'assemble',
        data: { step: STEP_NAME, status: 'running' },
      };
    }
    buffer += chunk.toString('utf8');
    const { buffer: remaining, events } = parseAndExtract(buffer, sessionId, artifactCb);
    buffer = remaining;
    for (const e of events) {
      if (e.event === 'complete') sawComplete = true;
      yield e;
    }
  }

  if (buffer.trim()) {
    const { events } = parseAndExtract(buffer, sessionId, artifactCb);
    for (const e of events) {
      if (e.event === 'complete') sawComplete = true;
      yield e;
    }
  }

  if (emittedRunning) {
    yield {
      event: 'assemble',
      data: {
        step: STEP_NAME,
        status: 'done',
        elapsed_ms: Date.now() - start,
      },
    };
  }

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

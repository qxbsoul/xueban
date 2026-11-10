/**
 * parsers/cli-streams/index.ts — CLI stdout parser dispatcher (Story 15.19 v2)
 *
 * Maps `StreamFormat` → concrete `(stdout, sessionId, artifactCb) → AsyncGenerator`
 * parser. Adding a new format = add a case here + a sibling parser file.
 */

import type { Readable } from 'node:stream';
import type { StreamFormat } from '../../cli-registry';
import type { SseEvent } from '../../parser';
import { parseClaudeStreamJson } from './claude-stream-json';
import { parseCodexStreamJson } from './codex-stream-json';
import { parseGhCopilot } from './gh-copilot';
import { parsePlainLine, type CliStreamArtifactCb } from './plain-line';

export type { CliStreamArtifactCb } from './plain-line';

export function getStreamParser(
  format: StreamFormat,
): (stdout: Readable, sessionId: string, artifactCb: CliStreamArtifactCb) => AsyncGenerator<SseEvent> {
  switch (format) {
    case 'claude-stream-json':
      return parseClaudeStreamJson;
    case 'codex-stream-json':
      return parseCodexStreamJson;
    case 'gh-copilot':
      return parseGhCopilot;
    case 'cursor-acp':
      // TODO Story 15.23 — full ACP subset parser. Temporarily falls back to
      // plain-line so cursor-agent still flows through end-to-end (the user
      // gets raw text instead of structured ACP events).
      return parsePlainLine;
    case 'plain-line':
    default:
      return parsePlainLine;
  }
}

export async function* dispatchParser(
  format: StreamFormat,
  stdout: Readable,
  sessionId: string,
  artifactCb: CliStreamArtifactCb,
): AsyncGenerator<SseEvent> {
  const parser = getStreamParser(format);
  yield* parser(stdout, sessionId, artifactCb);
}

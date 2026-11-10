/**
 * parser.ts — Streaming XML-tag parser for Claude SSE output (Story 15.2)
 *
 * Consumes incremental text emitted by Claude's content_block_delta stream and
 * extracts the following Xueban control tags into SSE events:
 *
 *   <sf:classify .../>     → classify
 *   <sf:step .../>         → assemble
 *   <sf:node .../>         → node
 *   <sf:edge .../>         → edge
 *   <artifact ...>...</>   → blueprint (+ yaml-line × N when type="yaml")
 *   <sf:complete .../>     → complete
 *
 * The parser is *buffer-aware*: callers feed it the accumulated buffer; it
 * returns the residual buffer (with completed tags stripped) and the array of
 * SSE events produced this tick. Open/incomplete tags stay in the buffer for
 * the next call.
 *
 * The artifact callback is fired synchronously when an <artifact> closes — the
 * caller writes the file to .xueban/projects/<session_id>/<filename>.
 */

import type { OutputKind } from './lib/contracts';

export interface SseEvent {
  event: string;
  data: unknown;
}

export type ArtifactCallback = (
  filename: string,
  content: string,
  type: string,
) => void;

// ─── S2.2 step-gating state ──────────────────────────────────────────────────
//
// The parser is called incrementally per text chunk, so it can't keep state
// in a local. We track the *currently open* step (running but not yet done)
// per sessionId in this module-level map. When the matching `done` event
// arrives, we check whether the contracted output_kind ever appeared during
// that step's window; if not, we synthesize an `error` event with code
// STEP_NO_OUTPUT so the front-end can offer a step-level retry (S4.1).
//
// Memory: one entry per active session; cleared on `sf:complete` and (for
// safety) when a new `running` step replaces the previous one without an
// explicit `done`. No TTL — bounded by run lifetime + the session-store cleanup.

interface StepGateState {
  step_index: number;
  step_name: string;
  output_kind: OutputKind;
  /** Set true the first time we see an event matching `output_kind`. */
  produced: boolean;
}

const stepGate = new Map<string, StepGateState>();
// Independent counter per session — `step_index` is positional (0-based by
// order of `running` events) because the LLM's <sf:step> tags do not carry
// a numeric index. This matches step-store.ts's <n>.json filenames.
const stepCounter = new Map<string, number>();

function isOutputKind(s: string | undefined): s is OutputKind {
  return s === 'nodes' || s === 'edges' || s === 'yaml' || s === 'classify' || s === 'none';
}

/** Force-clear all parser state for a session (called by step-store on session end). */
export function resetParserState(sessionId: string): void {
  stepGate.delete(sessionId);
  stepCounter.delete(sessionId);
}

// ─── attribute parsing ───────────────────────────────────────────────────────

function parseAttrs(attrStr: string): Record<string, string> {
  const result: Record<string, string> = {};
  const regex = /([\w][\w-]*)\s*=\s*["']([^"']*)["']/g;
  let m: RegExpExecArray | null;
  while ((m = regex.exec(attrStr)) !== null) {
    result[m[1]] = m[2];
  }
  return result;
}

// ─── main parser ─────────────────────────────────────────────────────────────

/**
 * Parse a buffer of streamed LLM text and extract Xueban control tags.
 *
 * @param buffer            Accumulated text-delta buffer (caller maintains).
 * @param sessionId         Session id (used for per-session step-gating state).
 * @param artifactCallback  Fired synchronously when an <artifact> block closes.
 * @param currentNodeId     Phase 2 eng-review A4: optional id of the workflow
 *                          node currently producing chunks. When the orchestration
 *                          layer (workflow scheduler / DAG runner) supplies this,
 *                          the parser attaches it as `node_id` on chunk-class
 *                          events (text/assemble/agent-substep/question-form/
 *                          thinking-chunk) so the front-end can route concurrent
 *                          chunks from a parallel-DAG to the right AgentDetail
 *                          panel. When undefined the field is omitted, keeping
 *                          legacy single-agent callers byte-equivalent (full
 *                          backward compatibility).
 */
export function parseAndExtract(
  buffer: string,
  sessionId: string,
  artifactCallback: ArtifactCallback,
  currentNodeId?: string,
): { buffer: string; events: SseEvent[] } {
  const events: SseEvent[] = [];

  // Phase 2 A4 helper — return a spread of `{ node_id }` only when the caller
  // supplied a non-empty currentNodeId. We deliberately *omit* the key entirely
  // (rather than emit `node_id: undefined`) so JSON.stringify output stays
  // identical to the pre-Phase-2 contract for legacy callers, and any
  // downstream schema validation that uses `hasOwnProperty`/strict equality
  // does not see an extra key.
  const nodeIdField = (): { node_id?: string } =>
    currentNodeId ? { node_id: currentNodeId } : {};

  // S2.2 — mark the currently-open step's gate as having produced its expected
  // output_kind. Defined at the top so all tag handlers below can call it.
  const markProduced = (kind: OutputKind): void => {
    const gate = stepGate.get(sessionId);
    if (gate && gate.output_kind === kind) {
      gate.produced = true;
    }
  };

  // sf:classify  (self-closing, no children)
  buffer = buffer.replace(/<sf:classify\s+((?:[^>"']|"[^"]*"|'[^']*')+?)\/>/g, (_match, attrs: string) => {
    const a = parseAttrs(attrs);
    events.push({
      event: 'classify',
      data: {
        output_type: a.output_type ?? 'workflow',
        mode: a.mode ?? 'team',
        confidence: parseFloat(a.confidence ?? '0.8'),
        complexity: parseInt(a.complexity ?? '2', 10),
      },
    });
    markProduced('classify');
    return '';
  });

  // sf:step  (self-closing)
  //
  // S2.2 — output_kind gating. When `status="running"` we open a gate entry
  // for this session/step. When `status="done"` we close it; if the declared
  // output_kind != 'none' but the gate never saw a matching artifact event,
  // we emit a STEP_NO_OUTPUT error frame *in addition to* the normal
  // assemble:done event so the front-end can react (S4.1 step retry).
  buffer = buffer.replace(/<sf:step\s+((?:[^>"']|"[^"]*"|'[^']*')+?)\/>/g, (_match, attrs: string) => {
    const a = parseAttrs(attrs);
    const stepName = a.name ?? '';
    const status = a.status ?? 'running';
    const declared = a.output_kind;
    // Default to 'none' when unspecified (back-compat: skills that haven't been
    // migrated to S2.1 still parse cleanly, they just lose the gating benefit).
    const outputKind: OutputKind = isOutputKind(declared) ? declared : 'none';

    // Step index is positional — incremented once per 'running' event.
    let stepIndex: number;
    if (status === 'running') {
      const prev = stepCounter.get(sessionId);
      stepIndex = prev === undefined ? 0 : prev + 1;
      stepCounter.set(sessionId, stepIndex);
      // If a previous step never closed, drop it silently — the LLM moved on
      // without emitting `done`, and the new `running` is the source of truth.
      stepGate.set(sessionId, {
        step_index: stepIndex,
        step_name: stepName,
        output_kind: outputKind,
        produced: false,
      });
    } else {
      // For 'done' / 'failed' the index is whatever is currently open (or the
      // last counter value if the LLM emitted 'done' without a matching 'running').
      stepIndex = stepCounter.get(sessionId) ?? 0;
    }

    events.push({
      event: 'assemble',
      data: {
        step: stepName,
        step_index: stepIndex,
        output_kind: outputKind,
        status,
        elapsed_ms: a.elapsed_ms ? parseInt(a.elapsed_ms, 10) : null,
        ...nodeIdField(),
      },
    });

    // Gate close on done — emit STEP_NO_OUTPUT if a non-'none' kind never produced.
    if (status === 'done') {
      const gate = stepGate.get(sessionId);
      if (
        gate &&
        gate.step_name === stepName &&
        gate.output_kind !== 'none' &&
        !gate.produced
      ) {
        events.push({
          event: 'error',
          data: {
            code: 'STEP_NO_OUTPUT',
            message:
              `Step "${stepName}" 声明 output_kind="${gate.output_kind}" 但本步未产出对应内容。`,
            step_index: gate.step_index,
            step_name: stepName,
            expected: gate.output_kind,
          },
        });
      }
      stepGate.delete(sessionId);
    }
    return '';
  });

  // sf:node  (self-closing)
  //
  // 2026-05-18 — agent-B extension. AgentPanel needs to render 5 slots
  // (Identity / Persona / Model / Tools / Memory). The LLM may now emit
  // optional `model`, `memory`, `tools_picked`, `tools_candidate` attributes
  // alongside the legacy `chips`. All new fields are optional — old skills
  // that only emit chips still parse cleanly, and the front-end falls back
  // to chips-derived values when the new fields are absent.
  buffer = buffer.replace(/<sf:node\s+((?:[^>"']|"[^"]*"|'[^']*')+?)\/>/g, (_match, attrs: string) => {
    const a = parseAttrs(attrs);
    const splitCsv = (s: string | undefined): string[] =>
      (s ?? '').split(',').map(x => x.trim()).filter(Boolean);
    // S6.2 — io_input / io_output may be JSON strings; tolerate raw text
    // when LLM forgets to escape, fall back to the raw string.
    const parseMaybeJson = (s: string | undefined): unknown => {
      if (!s) return undefined;
      try {
        return JSON.parse(s);
      } catch {
        return s;
      }
    };
    events.push({
      event: 'node',
      data: {
        node_id: a.id ?? '',
        type: (a.type as 'coordinator' | 'agent') ?? 'agent',
        title: a.title ?? '',
        sub: a.sub ?? '',
        chips: splitCsv(a.chips),
        status: 'building',
        avatar_char: a.avatar_char ?? (a.title ? a.title.charAt(0) : '?'),
        // Optional extension fields (undefined when LLM omits them so the
        // front-end can detect missing-data and run its fallback path).
        model: a.model || undefined,
        memory: a.memory || undefined,
        tools_picked: a.tools_picked ? splitCsv(a.tools_picked) : undefined,
        tools_candidate: a.tools_candidate ? splitCsv(a.tools_candidate) : undefined,
        persona: a.persona || undefined,
        // S6.2 — v3 stacked design extensions
        skill_ref: a.skill_ref || undefined,
        temperature: a.temperature ? parseFloat(a.temperature) : undefined,
        max_tokens: a.max_tokens ? parseInt(a.max_tokens, 10) : undefined,
        context_window: a.context_window ? parseInt(a.context_window, 10) : undefined,
        io_input: parseMaybeJson(a.io_input),
        io_output: parseMaybeJson(a.io_output),
      },
    });
    markProduced('nodes');
    return '';
  });

  // sf:agent-persona  (paired tag, body is multi-line system-prompt text)
  //
  // 2026-05-18 — emitted by the assembler when the agent's persona/system
  // prompt is too long to fit in an attribute. Body is taken verbatim
  // (trimmed) and routed to the front-end via a dedicated `agent-persona`
  // event keyed by `node_id` so the AgentPanel can merge it onto the
  // matching node. Order is not guaranteed — the panel must tolerate
  // persona arriving before or after the node it belongs to.
  //
  // 2026-05-20 (S6.2) — optional provenance attrs (`source`, `tokens`,
  // `cached`) so the v3 stacked AgentDetail can render
  // "from reader.skill.yaml#persona 632 tokens · cached".
  buffer = buffer.replace(
    /<sf:agent-persona\s+((?:[^>"']|"[^"]*"|'[^']*')+?)>([\s\S]*?)<\/sf:agent-persona>/g,
    (_match, attrs: string, body: string) => {
      const a = parseAttrs(attrs);
      events.push({
        event: 'agent-persona',
        data: {
          node_id: a.node_id ?? '',
          persona: body.trim(),
          source: a.source || undefined,
          tokens: a.tokens ? parseInt(a.tokens, 10) : undefined,
          cached: a.cached === 'true' ? true : a.cached === 'false' ? false : undefined,
        },
      });
      return '';
    },
  );

  // sf:agent-substep  (self-closing) — S6.2
  //
  // Granular per-agent substep progress for the v3 stacked AgentDetail. The
  // left-pane StepList expands this into tree rows ("reader · identity +
  // persona / reader · model / reader · tools"); the right pane uses the
  // `substep` value to anchor-scroll to the matching section (persona /
  // model / tools / memory).
  //
  //   substep ∈ identity | persona | model | tools | memory | io
  //
  // Provenance attrs `source` / `tokens` / `cached` mirror sf:agent-persona
  // so each section header can show "from <skill>.yaml#<slot> NNN tokens".
  buffer = buffer.replace(/<sf:agent-substep\s+((?:[^>"']|"[^"]*"|'[^']*')+?)\/>/g, (_match, attrs: string) => {
    const a = parseAttrs(attrs);
    // Phase 2 A4 — agent-substep ALREADY carries node_id from the LLM-emitted
    // tag attribute (the parser was extended for this in 2026-05-20 S6.2). The
    // tag's attribute remains the authoritative source. We fall back to the
    // currentNodeId supplied by the orchestrator only when the LLM omits the
    // attribute, so a parallel-DAG node that emits an unscoped <sf:agent-substep/>
    // still gets routed to the right AgentDetail panel.
    events.push({
      event: 'agent-substep',
      data: {
        node_id: a.node_id || currentNodeId || '',
        substep: a.substep ?? '',
        status: a.status ?? 'running',
        elapsed_ms: a.elapsed_ms ? parseInt(a.elapsed_ms, 10) : null,
        source: a.source || undefined,
        tokens: a.tokens ? parseInt(a.tokens, 10) : undefined,
        cached: a.cached === 'true' ? true : a.cached === 'false' ? false : undefined,
      },
    });
    return '';
  });

  // ─── P1 (root-cure plan §5): tool calls are NO LONGER parsed out of text ────
  //
  // We deleted the `<tool_use>` / `<tool_result>` / `<function_calls>` XML
  // extractors that used to live here. Tool invocations + results are now
  // FIRST-CLASS structured events sourced from typed `tool-use` / `tool-result`
  // TurnChunks (ConversationRuntime surfaces the model's call + the dispatch
  // result; ApiClientCallable maps native tool_use) and emitted directly by
  // `assembler.ts` pipeChunksToSse — they never round-trip through this text
  // parser. This kills the structured→XML-text→regex回环 (OpenDesign alignment:
  // tools come from structure, not from re-parsing flattened text).
  //
  // Consequence: if a model literally emits `<tool_use>…</tool_use>` as prose
  // (inline-XML models with no native tool API — rare), it now flows through as
  // a normal `text` event instead of a tool chip. That is an accepted trade per
  // the plan; the structured path is the supported route.

  // sf:thinking  (paired tag, body is the LLM's chain-of-thought for the
  // current step). Emitted as a streaming-friendly chunk via the dedicated
  // `thinking-chunk` event so the front-end can accumulate a real reasoning
  // log in ThinkCard (设计点 6 / design-v1 §4.3). May appear between steps,
  // multiple times per session. No attrs required; `step` attribute optional
  // for future per-step grouping.
  buffer = buffer.replace(
    /<sf:thinking(\s+(?:[^>"']|"[^"]*"|'[^']*')*?)?>([\s\S]*?)<\/sf:thinking>/g,
    (_match, attrs: string | undefined, body: string) => {
      const a = parseAttrs(attrs ?? '');
      events.push({
        event: 'thinking-chunk',
        data: {
          step: a.step ?? null,
          text: body.trim(),
          ...nodeIdField(),
        },
      });
      return '';
    },
  );

  // sf:question-form  (paired tag, body is JSON) — S12
  //
  // LLM emits this during Phase 1 when the user goal is too ambiguous to
  // proceed. Body is a JSON object matching the FormSchema in
  // src/components/run-session/QuestionFormModal.tsx (id / title /
  // description / questions: [{id, label, type, options?, required?}]).
  // Front-end renders an interactive modal; user fills in answers and
  // POSTs to /api/run-sessions/:id/messages as a follow-up turn carrying
  // the JSON answers, which the LLM consumes on the next iteration.
  //
  // Borrowed from open-design `<question-form>` pattern (apps/daemon/src/
  // prompts/discovery.ts). See design-doc S12.
  buffer = buffer.replace(
    /<sf:question-form\s+((?:[^>"']|"[^"]*"|'[^']*')+?)>([\s\S]*?)<\/sf:question-form>/g,
    (_match, attrs: string, body: string) => {
      const a = parseAttrs(attrs);
      let parsedBody: unknown;
      try {
        parsedBody = JSON.parse(body.trim());
      } catch (err) {
        parsedBody = { __parse_error: (err as Error).message, raw: body.trim() };
      }
      events.push({
        event: 'question-form',
        data: {
          id: a.id ?? 'unknown',
          title: a.title ?? '',
          body: parsedBody,
          ...nodeIdField(),
        },
      });
      return '';
    },
  );

  // sf:edge  (self-closing)
  buffer = buffer.replace(/<sf:edge\s+((?:[^>"']|"[^"]*"|'[^']*')+?)\/>/g, (_match, attrs: string) => {
    const a = parseAttrs(attrs);
    events.push({
      event: 'edge',
      data: {
        from: a.from ?? '',
        to: a.to ?? '',
        status: 'active',
      },
    });
    markProduced('edges');
    return '';
  });

  // <artifact ...>...</artifact>  — only when fully closed
  const artifactRe = /<artifact([^>]*)>([\s\S]*?)<\/artifact>/g;
  buffer = buffer.replace(artifactRe, (_match, tagAttrs: string, content: string) => {
    const a = parseAttrs(tagAttrs);
    const type = a.type ?? 'yaml';
    const filename =
      a.filename ??
      (type === 'html' ? 'output.html' : type === 'markdown' ? 'output.md' : 'output.yml');
    const trimmed = content.trim();

    // Side-effect: write file to disk
    try {
      artifactCallback(filename, trimmed, type);
    } catch (err) {
      // We don't want a write failure to crash the parser — surface as error event.
      events.push({
        event: 'error',
        data: {
          message: `Failed to persist artifact ${filename}: ${(err as Error).message}`,
          code: 'ARTIFACT_WRITE_FAILED',
        },
      });
    }

    // Primary event: blueprint (the front-end displays `yaml` regardless of type)
    events.push({
      event: 'blueprint',
      data: {
        yaml: trimmed,
        filename,
        artifact_type: type,
        artifact_url: `/projects/${sessionId}/${filename}`,
      },
    });

    // YAML row stream
    if (type === 'yaml') {
      const lines = trimmed.split('\n');
      lines.forEach(line => {
        events.push({
          event: 'yaml-line',
          data: { line, total_lines: lines.length },
        });
      });
      markProduced('yaml');
    }

    return '';
  });

  // 2026-05-11 Layer 1 — strip <sf:discovery>...</sf:discovery>. Claude is
  // told by DISCOVERY_CHARTER to emit this block at the start of every
  // first reply (intent restatement + plan + ambiguities). The block is
  // metadata for an inspector view — we don't surface it in the chat
  // bubble, but if we leave it un-stripped the text-emit branch below
  // holds back forever waiting for an unknown tag to close. We emit a
  // `discovery` event so downstream tools could read it; current UI just
  // ignores. Use [\s\S] so newlines inside the block match.
  buffer = buffer.replace(/<sf:discovery>([\s\S]*?)<\/sf:discovery>/g, (_match, content: string) => {
    events.push({ event: 'discovery', data: { body: content.trim() } });
    return '';
  });

  // sf:complete  (self-closing, attrs optional). Allow attribute values that
  // themselves contain `/` (e.g. redirect="/editor"). We accept any chars that
  // are not `>` until we hit the literal `/>` terminator.
  buffer = buffer.replace(/<sf:complete\s*((?:[^>"']|"[^"]*"|'[^']*')*?)\/>/g, (_match, attrs: string) => {
    const a = parseAttrs(attrs ?? '');
    events.push({
      event: 'complete',
      data: {
        session_id: sessionId,
        run_id: `run-${sessionId.slice(0, 8)}`,
        redirect: a.redirect ?? `/editor?session=${sessionId}`,
      },
    });
    resetParserState(sessionId);
    return '';
  });

  // 2026-05-11 Layer 1 — defensive: any FULLY CLOSED unknown <sf:foo>...</sf:foo>
  // block left in the buffer would otherwise deadlock the text emit branch
  // below (it would hold back at `<`). Strip them and emit a generic
  // `unknown-tag` event so we don't lose information silently. Self-closing
  // unknown tags (<sf:foo .../>) get the same treatment.
  buffer = buffer.replace(/<sf:([\w-]+)>([\s\S]*?)<\/sf:\1>/g, (_match, name: string, content: string) => {
    events.push({ event: 'unknown-tag', data: { name, body: content.trim() } });
    return '';
  });
  buffer = buffer.replace(/<sf:([\w-]+)\s+((?:[^>"']|"[^"]*"|'[^']*')*?)\/>/g, (_match, name: string, attrs: string) => {
    events.push({ event: 'unknown-tag', data: { name, attrs: parseAttrs(attrs) } });
    return '';
  });

  // P4 fix (audit/thinking-transport-fix-plan-2026-05-27.md §2.6): strip leaked
  // SSE wire frames. Xueban serializes its own SSE as
  // `event: <name>\ndata: <one-line json>\n\n` (run-sessions.ts:742). When the
  // LLM parrots that shape back as plain text (observed after a reconnect
  // re-run feeds prior frame-shaped output back as context), this subtractive
  // parser would otherwise leak it verbatim into the answer bubble — the
  // `event: assemble\ndata: {…}` the user saw. These frames are never
  // legitimate answer content. The match is tight (an `event:` line immediately
  // followed by a `data:` line whose payload opens with `{`/`[`) so normal prose
  // — even prose that happens to mention "event:" — is not touched. This is a
  // T1 stop-the-bleed guard; the structural fix is backend additive
  // normalization (audit §2.7, T3).
  if (/^[ \t]*event:[ \t]*[\w.-]+[ \t]*\r?\n[ \t]*data:[ \t]*[{[]/m.test(buffer)) {
    // T3 (raw first-class kind): don't silently drop the leaked frames —
    // collect them and surface as a single `raw` event so they render in a
    // collapsed raw block (debuggable) instead of polluting the answer.
    let leaked = '';
    buffer = buffer.replace(
      /^[ \t]*event:[ \t]*[\w.-]+[ \t]*\r?\n[ \t]*data:[ \t]*[{[][^\n]*\r?\n?/gm,
      (m) => {
        leaked += m;
        return '';
      },
    );
    if (leaked.trim()) {
      events.push({ event: 'raw', data: { text: leaked.trim(), source: 'sse-frame-leak', ...nodeIdField() } });
    }
  }

  // 2026-05-11 Layer 1 — Claude Code-style conversation mode.
  // After stripping all known + unknown closed tags, the residual non-tag
  // text is the LLM's plain natural-language reply. Emit as `text` events.
  // We only hold back content from the first `<sf:` or `<artifact` prefix
  // onward — those could be partial tags still streaming. Other `<`
  // (literal markup the LLM might write, or math like `<5`) flows through.
  const partialTagIdx = findPartialTagStart(buffer);
  const safeText = partialTagIdx === -1 ? buffer : buffer.slice(0, partialTagIdx);
  if (safeText.length > 0) {
    events.push({ event: 'text', data: { text: safeText, ...nodeIdField() } });
    buffer = partialTagIdx === -1 ? '' : buffer.slice(partialTagIdx);
  }

  return { buffer, events };
}

// Find the earliest position where the buffer contains a potentially-incomplete
// known tag. Two cases:
//   (1) full prefix already in buffer: `<sf:` or `<artifact`
//   (2) buffer ends mid-prefix: `…<`, `…<s`, `…<sf`, `…<a`, `…<artifa` etc.
//       Without (2), streamed LLM output that splits `<sf:step …/>` into
//       per-token chunks (`<sf`, `:`, `step`, ` name`, …) escapes the parser
//       and gets emitted as raw `text` events — bug observed 2026-05-18.
// Returns -1 if no such prefix exists.
function findPartialTagStart(buf: string): number {
  // Known full prefixes — anything that starts one of these is held back as
  // a potential streaming tag until the matching close is seen. P1 removed the
  // tool wrappers (<tool_use>/<tool_result>/<function_call>): tools are now
  // structured TurnChunks, not text, so we no longer hold text back for them.
  // The parser's only remaining held tags are the business/artifact tags.
  const prefixes = ['<sf:', '<artifact'];
  let known = -1;
  for (const p of prefixes) {
    const idx = buf.indexOf(p);
    if (idx !== -1 && (known === -1 || idx < known)) known = idx;
  }

  // Tail prefix: `<` followed by chars that could still grow into `<sf:…` or
  // `<artifact…`. We hold back from the `<` even if it's a literal `<5` math
  // expression — minor cost (text emit delayed until next chunk) for a major
  // correctness win.
  const tailMatch = buf.match(/<[a-zA-Z][a-zA-Z0-9:_-]*$/);
  const tail = tailMatch ? buf.length - tailMatch[0].length : -1;
  // Bare trailing `<` (no chars after) is also a potential partial tag start.
  const bareLt = buf.endsWith('<') ? buf.length - 1 : -1;

  const candidates = [known, tail, bareLt].filter(x => x !== -1);
  return candidates.length === 0 ? -1 : Math.min(...candidates);
}

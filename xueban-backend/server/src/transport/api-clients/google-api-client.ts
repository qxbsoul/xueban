/**
 * google-api-client.ts — ApiClient (S5) implementation backed by the Google
 * Gemini generateContent protocol via `@google/generative-ai`.
 *
 * S14.1 (skill-team-conversion-design-v1.md §3.1.b §5 S5/S6) — companion to
 * AnthropicApiClient and OpenAiCompatApiClient. Gemini speaks its own /v1beta
 * protocol; no OpenAI-compat endpoint is available for the modern function-
 * calling shape we need, so this client sits alongside the OpenAI-compat
 * shim rather than going through it.
 *
 * Gemini protocol cheat-sheet (vs Anthropic):
 *   Concept          | Anthropic                      | Gemini
 *   ─────────────────┼────────────────────────────────┼───────────────────────────
 *   message roles    | system / user / assistant      | systemInstruction (top) /
 *                    | / tool                         | user / model
 *   tool result      | role='user' + tool_result      | role='user' + part:
 *                    | block                          | functionResponse{name,response}
 *   tool call        | content_block tool_use         | candidates[0].content.parts[]
 *                    |                                | .functionCall {name, args}
 *   tool spec        | {name, description,            | {name, description,
 *                    | input_schema}                  | parameters} inside
 *                    |                                | functionDeclarations[]
 *   stop reason      | message_delta.delta            | candidates[0].finishReason
 *                    | .stop_reason                   |
 *   usage            | message_start (in) +           | usageMetadata on last chunk
 *                    | message_delta (out)            |
 *   streaming unit   | per-event SSE                  | per-chunk via .stream
 *                    |                                | AsyncIterable
 *
 * Tool_use id: Gemini does NOT return a separate tool-call id — the
 * function name + position is the identity. We synthesize an id of the form
 * `g_<turnSeq>_<callIdx>` so downstream code (which keys by id) keeps working.
 * The runtime then uses this same id when constructing the tool_result, which
 * we strip back off before sending the functionResponse to Gemini (Gemini
 * only cares about the function name, not the id).
 *
 * Safety settings: we DO NOT pass `safetySettings` explicitly — that lets the
 * Gemini SDK apply its defaults (which today block none of the categories at
 * threshold OFF for v1 SDK, BLOCK_MEDIUM_AND_ABOVE in v0.x). Tests assert
 * that no safetySettings field is forwarded so a future change to inject one
 * is loud.
 */

import { GoogleGenerativeAI } from '@google/generative-ai';
import type {
  ApiClient,
  AssistantEvent,
} from '../../lib/conversation-runtime';
import type {
  ConversationMessage,
  TokenUsage,
} from '../../lib/conversation-types';
import type { ToolSpec } from '../../lib/tool-spec';
import { DEFAULT_MODELS } from './types';

export interface GoogleApiClientOptions {
  /** BYOK key. If absent and env GOOGLE_API_KEY also empty, stream() throws. */
  apiKey?: string;
  /** Model id (e.g. 'gemini-2.5-flash'). Falls back to DEFAULT_MODELS.google. */
  model?: string;
  /** Per-turn output cap → mapped to generationConfig.maxOutputTokens. */
  max_tokens?: number;
  /** 0..2 sampling temp; mapped to generationConfig.temperature when defined. */
  temperature?: number;
}

// ─── Gemini wire types (locally redeclared to keep imports light) ─────────

type GeminiPart =
  | { text: string }
  | { functionCall: { name: string; args: Record<string, unknown> } }
  | {
      functionResponse: {
        name: string;
        response: Record<string, unknown>;
      };
    };

interface GeminiContent {
  role: 'user' | 'model';
  parts: GeminiPart[];
}

interface GeminiTool {
  functionDeclarations: Array<{
    name: string;
    description: string;
    parameters: object;
  }>;
}

// ─── Translation: ToolSpec → Gemini functionDeclaration ────────────────────

function toGeminiTools(specs: ToolSpec[]): GeminiTool[] {
  if (specs.length === 0) return [];
  return [
    {
      functionDeclarations: specs.map((t) => ({
        name: t.name,
        description: t.description,
        parameters: t.input_schema,
      })),
    },
  ];
}

// ─── Translation: ConversationMessage[] → Gemini contents[] ───────────────

/**
 * Map our ContentBlock-shaped history to Gemini's `contents` array.
 *
 *   - `role: 'user'` text blocks  → {role:'user', parts:[{text}]}
 *   - `role: 'assistant'` blocks  → {role:'model', parts:[{text}? ,{functionCall}*]}
 *   - `role: 'tool'` tool_results → {role:'user', parts:[{functionResponse}+]}
 *
 * Adjacent tool messages are merged into a single user envelope to mirror the
 * Anthropic fold (Gemini also expects strict user/model alternation; multiple
 * functionResponse parts in one user content is the legal way to deliver
 * multiple parallel tool results).
 *
 * `system` role messages are SKIPPED here — Gemini takes the system prompt
 * as a separate `systemInstruction` field on the model construction call.
 */
export function toGeminiContents(messages: ConversationMessage[]): GeminiContent[] {
  const out: GeminiContent[] = [];
  let lastWasFoldedTool = false;

  for (const m of messages) {
    if (m.role === 'system') continue;

    if (m.role === 'user') {
      const text = m.blocks
        .map((b) => (b.kind === 'text' ? b.text : ''))
        .join('');
      out.push({ role: 'user', parts: text.length > 0 ? [{ text }] : [] });
      lastWasFoldedTool = false;
    } else if (m.role === 'assistant') {
      const parts: GeminiPart[] = [];
      for (const b of m.blocks) {
        if (b.kind === 'text' && b.text.length > 0) {
          parts.push({ text: b.text });
        } else if (b.kind === 'tool_use') {
          const args =
            b.input && typeof b.input === 'object'
              ? (b.input as Record<string, unknown>)
              : {};
          parts.push({ functionCall: { name: b.name, args } });
        }
      }
      out.push({ role: 'model', parts });
      lastWasFoldedTool = false;
    } else if (m.role === 'tool') {
      // Build functionResponse parts. The `response` field expects a JSON
      // object — we attempt to parse `output` as JSON, falling back to a
      // string-wrapped envelope if it's plain text.
      const responseParts: GeminiPart[] = [];
      for (const b of m.blocks) {
        if (b.kind !== 'tool_result') continue;
        let response: Record<string, unknown>;
        const raw = b.output;
        try {
          const parsed = JSON.parse(raw);
          response =
            parsed && typeof parsed === 'object' && !Array.isArray(parsed)
              ? (parsed as Record<string, unknown>)
              : { result: parsed };
        } catch {
          response = { result: raw };
        }
        if (b.is_error) response.is_error = true;
        responseParts.push({
          functionResponse: { name: b.tool_name, response },
        });
      }
      if (lastWasFoldedTool && out.length > 0) {
        out[out.length - 1].parts.push(...responseParts);
      } else {
        out.push({ role: 'user', parts: responseParts });
        lastWasFoldedTool = true;
      }
    }
  }
  return out;
}

// ─── usage extraction ─────────────────────────────────────────────────────

/**
 * Gemini usageMetadata → our TokenUsage shape.
 *   promptTokenCount     → input_tokens
 *   candidatesTokenCount → output_tokens
 *   cachedContentTokenCount → cache_read_input_tokens
 */
function extractUsage(raw: unknown): TokenUsage | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const u = raw as Record<string, unknown>;
  const out: TokenUsage = {};
  if (typeof u.promptTokenCount === 'number') out.input_tokens = u.promptTokenCount;
  if (typeof u.candidatesTokenCount === 'number') out.output_tokens = u.candidatesTokenCount;
  if (typeof u.cachedContentTokenCount === 'number')
    out.cache_read_input_tokens = u.cachedContentTokenCount;
  return Object.keys(out).length > 0 ? out : undefined;
}

// ─── stop_reason normalization ────────────────────────────────────────────

/**
 * Map Gemini finishReason → our normalized stop_reason.
 *   STOP            → end_turn   (model finished normally; also when only
 *                                   functionCall parts are present in modern
 *                                   SDK versions)
 *   MAX_TOKENS      → max_tokens
 *   SAFETY          → safety
 *   RECITATION      → recitation
 *   OTHER / blank   → unknown
 * If the candidate emitted functionCall parts we override 'end_turn' →
 * 'tool_use' so the runtime knows to keep iterating.
 */
function normalizeFinishReason(reason: string | undefined, hadToolCall: boolean): string {
  if (hadToolCall) return 'tool_use';
  if (!reason) return 'unknown';
  switch (reason) {
    case 'STOP':
      return 'end_turn';
    case 'MAX_TOKENS':
      return 'max_tokens';
    case 'SAFETY':
      return 'safety';
    case 'RECITATION':
      return 'recitation';
    default:
      return reason.toLowerCase();
  }
}

// ─── Client ───────────────────────────────────────────────────────────────

export class GoogleApiClient implements ApiClient {
  private turnSeq = 0;

  constructor(private readonly opts: GoogleApiClientOptions = {}) {}

  /**
   * Stream one Gemini turn. See file header for protocol translation
   * contract.
   *
   * Tool call id synthesis: Gemini does not assign ids to functionCalls in
   * the response — we synthesize one of the form `g_<turn>_<idx>` so the
   * downstream runtime (which keys pending tools by id) keeps working.
   * Since the toolResult round-trips through ContentBlock.tool_name (not
   * id), the id is purely internal and Gemini never sees it.
   */
  async *stream(args: {
    system_prompt: string;
    messages: ConversationMessage[];
    tools: ToolSpec[];
    signal: AbortSignal;
  }): AsyncIterable<AssistantEvent> {
    const apiKey = this.opts.apiKey ?? process.env.GOOGLE_API_KEY;
    if (!apiKey) {
      throw new Error('Google API key not configured');
    }

    const modelId =
      this.opts.model ?? process.env.XUEBAN_DEFAULT_MODEL ?? DEFAULT_MODELS.google;
    const maxOutputTokens = this.opts.max_tokens ?? 8192;

    const genAI = new GoogleGenerativeAI(apiKey);
    const tools = toGeminiTools(args.tools);
    // SDK `ModelParams.tools[].functionDeclarations[].parameters` is typed as
    // a concrete `FunctionDeclarationSchema` requiring `type` + `properties`.
    // Our internal ToolSpec.input_schema already matches that shape at runtime
    // (all S4 SkillAnchorTool specs declare type: 'object' + properties), so
    // we cast to satisfy the compiler without imposing a stricter contract
    // on callers.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const modelParams: any = {
      model: modelId,
      ...(args.system_prompt.length > 0
        ? { systemInstruction: args.system_prompt }
        : {}),
      ...(tools.length > 0 ? { tools } : {}),
      generationConfig: {
        maxOutputTokens,
        ...(this.opts.temperature !== undefined
          ? { temperature: this.opts.temperature }
          : {}),
      },
    };
    const model = genAI.getGenerativeModel(modelParams);

    const contents = toGeminiContents(args.messages);
    this.turnSeq += 1;
    const turnId = this.turnSeq;

    let result: { stream: AsyncIterable<unknown> };
    try {
      // Pass signal via request options if the SDK accepts it.
      result = (await model.generateContentStream(
        { contents },
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        { signal: args.signal } as any,
      )) as { stream: AsyncIterable<unknown> };
    } catch (err) {
      throw err instanceof Error ? err : new Error(String(err));
    }

    let finishReason: string | undefined;
    let hadToolCall = false;
    let toolCallIdx = 0;
    // Coalesce usageMetadata across chunks: Gemini sends it on the final
    // chunk but a few proxies sprinkle partials mid-stream. Take the last
    // observed value so the runtime's addUsage() doesn't double-count.
    let lastUsage: TokenUsage | undefined;

    try {
      for await (const chunkRaw of result.stream) {
        if (args.signal.aborted) return;
        const chunk = chunkRaw as {
          candidates?: Array<{
            content?: { parts?: Array<{ text?: string; functionCall?: { name: string; args?: Record<string, unknown> } }> };
            finishReason?: string;
          }>;
          usageMetadata?: unknown;
        };

        const candidate = chunk.candidates?.[0];
        const parts = candidate?.content?.parts;
        if (Array.isArray(parts)) {
          for (const part of parts) {
            if (typeof part.text === 'string' && part.text.length > 0) {
              yield { kind: 'text_delta', text: part.text };
            } else if (part.functionCall && typeof part.functionCall.name === 'string') {
              hadToolCall = true;
              const id = `g_${turnId}_${toolCallIdx++}`;
              yield {
                kind: 'tool_use',
                id,
                name: part.functionCall.name,
                input: part.functionCall.args ?? {},
              };
            }
          }
        }

        if (candidate?.finishReason) finishReason = candidate.finishReason;

        const usage = extractUsage(chunk.usageMetadata);
        if (usage) lastUsage = usage;
      }
    } catch (err) {
      // Re-throw — runtime decides whether to surface as 'error' SSE. We do
      // NOT emit message_stop here; emitting after an error would tell the
      // runtime the turn ended cleanly and the error event would race the
      // stop event in the consumer.
      throw err instanceof Error ? err : new Error(String(err));
    }

    // Successful stream end. Emit usage (if any) BEFORE message_stop so the
    // runtime's totalUsage is updated before it attaches usage to the
    // assistant message.
    if (lastUsage) yield { kind: 'usage', usage: lastUsage };
    yield {
      kind: 'message_stop',
      stop_reason: normalizeFinishReason(finishReason, hadToolCall),
    };
  }
}

/**
 * 教材解析 Agent 管线（2026-11-16）。
 *
 * 分工：
 * - blocks 由程序从原文提取并分配 ID；
 * - Agent 只负责语义组织（章节/小节/知识点/目标）；
 * - 程序负责 JSON 校验、块引用校验、重复与前置关系校验、循环检测、持久化。
 */
import { callProvider, PROVIDERS_NO_KEY, type ProviderId, type ProviderChunk } from '../transport/api-clients';
import type { LearningSourceBlock } from '../storage/learning';
import { buildOutlinePrompt, buildSectionPrompt, buildLessonDesignPartPrompt, AGENT_SYSTEM_PROMPT, type LessonDesignPart } from '../prompts/learning-parse';

export interface AgentCallStat {
  task: string;
  input_chars: number;
  input_blocks: number;
  attachments: number;
  max_tokens: number;
  output_chars: number;
  output_preview?: string;
  thinking_text?: string;
  prompt_preview?: string;
  duration_ms: number;
  truncated: boolean;
  attempts: number;
  aborted: boolean;
}

export interface AgentModelConfig {
  provider: ProviderId;
  model?: string;
  apiKey?: string;
  baseUrl?: string;
  timeoutMs?: number;
  maxAttempts?: number;
  maxTokens?: number;
  /** 默认 off；auto/always 只对显式声明支持的 provider 生效。 */
  reasoningMode?: 'off' | 'auto' | 'always';
  supportsThinking?: boolean;
  sectionConcurrency?: number;
  designConcurrency?: number;
  onCall?: (stat: AgentCallStat) => void;
  onCallStart?: (task: string) => void;
}

export type AgentCall = (
  provider: ProviderId,
  input: {
    systemPrompt: string;
    userMessage: string;
    attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
    model?: string;
    api_key?: string;
    base_url?: string;
    max_tokens?: number;
    temperature?: number;
    thinking?: 'enabled' | 'disabled';
    reasoning_effort?: 'low' | 'high' | 'max';
    signal?: AbortSignal;
  },
) => AsyncIterable<ProviderChunk>;

export interface AgentKnowledgePoint {
  title: string;
  explanation: string;
  concepts: string[];
  formulas: string[];
  methods: string[];
  prerequisites: string[];
  block_ids: string[];
  origin: 'textbook' | 'agent_inferred';
  status: 'parsed' | 'needs_review';
  confidence: number;
  source_ranges?: Array<{ block_id: string; start_offset: number; end_offset: number }>;
}

export type LessonSourceKind = 'textbook' | 'derived' | 'supplemental' | 'needs_review';
export type DesignStatus = 'complete' | 'partial' | 'needs_review';

export interface LessonDesignItem {
  kind: string;
  title: string;
  body: string;
  bullets: string[];
  formulas: string[];
  source: LessonSourceKind;
  block_ids: string[];
}

export interface LessonDesignExample {
  title: string;
  problem: string;
  steps: string[];
  answer: string;
  source: LessonSourceKind;
  block_ids: string[];
}

export interface LessonDesignNote {
  text: string;
  source: LessonSourceKind;
  block_ids: string[];
}

export interface LessonDesignActivity {
  prompt: string;
  purpose: string;
  source: LessonSourceKind;
  block_ids: string[];
}

export interface LessonDesignObjective {
  text: string;
  kind: 'knowledge' | 'method' | 'application';
}

export interface AgentLessonDesign {
  objectives: LessonDesignObjective[];
  preparation: LessonDesignNote[];
  introduction: LessonDesignNote | null;
  teaching_flow: LessonDesignItem[];
  examples: LessonDesignExample[];
  misconceptions: LessonDesignNote[];
  activities: LessonDesignActivity[];
  summary: LessonDesignNote | null;
  self_check: Array<{ question: string; answer_hint: string; source: LessonSourceKind; block_ids: string[] }>;
  status: DesignStatus;
}

export interface LessonDesignStats {
  total: number;
  complete: number;
  partial: number;
  needs_review: number;
  failed: number;
  part_failures?: number;
}

export interface AgentSection {
  title: string;
  summary: string;
  block_ids: string[];
  objectives: string[];
  knowledge_points: AgentKnowledgePoint[];
  status: 'parsed' | 'needs_review';
  lesson_design?: AgentLessonDesign;
  design_status?: DesignStatus;
}

export interface AgentChapter {
  title: string;
  summary: string;
  sections: AgentSection[];
}

export interface AgentCourseInfo {
  title: string;
  subject: string;
  grade: string;
  book_info: string;
}

export interface AgentParseResult {
  course: AgentCourseInfo;
  chapters: AgentChapter[];
  validation_errors: string[];
  batch_count: number;
  lesson_designs?: LessonDesignStats;
  coverage?: CoverageStats;
  model_calls?: AgentCallStat[];
}

export class LearningAgentError extends Error {
  code: string;
  attempts: number;
  constructor(code: string, message: string, attempts = 0) {
    super(message);
    this.name = 'LearningAgentError';
    this.code = code;
    this.attempts = attempts;
  }
}

const MAX_PROMPT_CHARS = 14_000;
const MAX_SECTION_CHARS = 10_000;
const SEGMENT_OVERLAP = 180;
const MAX_SPLIT_DEPTH = 2;

export interface SourceSegment {
  blockId: string;
  blockType: string;
  start: number;
  end: number;
  text: string;
  line: number;
  page: number | null;
  status?: string;
  tableStatus?: string;
}

export interface SourceChunk {
  segments: SourceSegment[];
  chars: number;
}

export interface CoverageRange {
  block_id: string;
  start_offset: number;
  end_offset: number;
  reason?: string;
}

export interface CoverageStats {
  total_blocks: number;
  total_segments: number;
  total_chars: number;
  processed_segments: number;
  processed_chars: number;
  failed_segments: number;
  failed_chars: number;
  covered_chars: number;
  unprocessed_chars: number;
  points: number;
  cited_blocks: number;
  failed_ranges: CoverageRange[];
  processed_ranges: CoverageRange[];
}

function splitLongBlock(block: LearningSourceBlock, maxChars: number): SourceSegment[] {
  const text = block.text;
  if (text.length <= maxChars) {
    return [{
      blockId: block.id, blockType: block.block_type, start: 0, end: text.length,
      text, line: block.line_start, page: block.page,
      status: block.status, tableStatus: block.table_status,
    }];
  }
  const segments: SourceSegment[] = [];
  let start = 0;
  while (start < text.length) {
    const end = Math.min(text.length, start + maxChars);
    segments.push({
      blockId: block.id, blockType: block.block_type, start, end,
      text: text.slice(start, end), line: block.line_start, page: block.page,
      status: block.status, tableStatus: block.table_status,
    });
    if (end >= text.length) break;
    start = Math.max(start + 1, end - SEGMENT_OVERLAP);
  }
  return segments;
}

/**
 * 把全部原文块切成覆盖完整的 segments；超长块继续按字符区间拆分。
 * 每个片段保留 block_id + start/end，绝不通过 slice 丢弃原文。
 */
export function chunkSourceBlocks(blocks: LearningSourceBlock[], maxChars: number): SourceChunk[] {
  const chunks: SourceChunk[] = [];
  let current: SourceSegment[] = [];
  let currentChars = 0;
  const push = () => {
    if (current.length) chunks.push({ segments: current, chars: currentChars });
    current = [];
    currentChars = 0;
  };
  blocks.forEach((block) => {
    splitLongBlock(block, maxChars).forEach((segment) => {
      if (current.length && currentChars + segment.text.length > maxChars) push();
      current.push(segment);
      currentChars += segment.text.length;
    });
  });
  push();
  return chunks;
}

function promptBlocks(chunk: SourceChunk) {
  return chunk.segments.map((segment) => ({
    id: segment.blockId,
    type: segment.blockType,
    text: segment.text,
    line: segment.line,
    page: segment.page,
    status: segment.status,
    table_status: segment.tableStatus,
    offset_start: segment.start,
    offset_end: segment.end,
  }));
}

function splitSourceChunk(chunk: SourceChunk): [SourceChunk, SourceChunk] | null {
  const segments = chunk.segments;
  if (segments.length > 1) {
    const middle = Math.ceil(segments.length / 2);
    const left = segments.slice(0, middle);
    const right = segments.slice(middle);
    if (!left.length || !right.length) return null;
    return [
      { segments: left, chars: left.reduce((sum, item) => sum + item.text.length, 0) },
      { segments: right, chars: right.reduce((sum, item) => sum + item.text.length, 0) },
    ];
  }
  const only = segments[0];
  if (!only || only.text.length < 2) return null;
  const middle = Math.floor(only.text.length / 2);
  const leftText = only.text.slice(0, middle);
  const rightText = only.text.slice(middle);
  const left: SourceSegment = { ...only, end: only.start + middle, text: leftText };
  const right: SourceSegment = { ...only, start: only.start + middle, text: rightText };
  return [
    { segments: [left], chars: leftText.length },
    { segments: [right], chars: rightText.length },
  ];
}

function coverageFrom(blocks: LearningSourceBlock[], chunks: SourceChunk[], processed: Set<string>,
  failed: CoverageRange[], points: number, cited: Set<string>): CoverageStats {
  const totalChars = blocks.reduce((sum, block) => sum + block.text.length, 0);
  const processedChars = [...processed].reduce((sum, key) => {
    const [, start, end] = key.split(':').map(Number);
    return sum + Math.max(0, end - start);
  }, 0);
  const failedChars = failed.reduce((sum, item) => sum + Math.max(0, item.end_offset - item.start_offset), 0);
  const segments = Math.max(
    chunks.reduce((sum, chunk) => sum + chunk.segments.length, 0),
    processed.size + failed.length,
  );
  return {
    total_blocks: blocks.length,
    total_segments: segments,
    total_chars: totalChars,
    processed_segments: processed.size,
    processed_chars: processedChars,
    failed_segments: failed.length,
    failed_chars: failedChars,
    covered_chars: Math.max(0, Math.min(totalChars, processedChars + failedChars)),
    unprocessed_chars: Math.max(0, totalChars - processedChars - failedChars),
    points,
    cited_blocks: cited.size,
    failed_ranges: failed,
    processed_ranges: [...processed].map((key) => {
      const [block_id, start, end] = key.split(':');
      return { block_id, start_offset: Number(start), end_offset: Number(end) };
    }),
  };
}

function rangeKey(segment: SourceSegment): string {
  return `${segment.blockId}:${segment.start}:${segment.end}`;
}

function extractThinkingText(text: string): string {
  const parts: string[] = [];
  const pattern = /<(?:sf:thinking|sf:analysis)>([\s\S]*?)<\/(?:sf:thinking|sf:analysis)>/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) parts.push(match[1].trim());
  return parts.filter(Boolean).join('\n\n').slice(0, 12_000);
}

function parseJsonObject(text: string): Record<string, unknown> {
  const cleaned = String(text || '')
    .replace(/^\uFEFF/, '')
    // 部分模型把思考内容包在 <sf:thinking> 中；不能把思考里的示例 JSON 当成结果。
    .replace(/<sf:thinking>[\s\S]*?<\/sf:thinking>/gi, '')
    .replace(/<sf:analysis>[\s\S]*?<\/sf:analysis>/gi, '')
    .replace(/^```(?:json)?/i, '')
    .replace(/```$/i, '')
    .trim();
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) {
    const preview = cleaned.slice(0, 60).replace(/\s+/g, ' ');
    throw new LearningAgentError('INVALID_JSON', preview ? `模型没有返回 JSON 对象：${preview}` : '模型没有返回 JSON 对象');
  }
  try {
    const parsed = JSON.parse(cleaned.slice(start, end + 1));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('not object');
    }
    return parsed as Record<string, unknown>;
  } catch {
    throw new LearningAgentError('INVALID_JSON', '模型返回的 JSON 无法解析');
  }
}

async function callAgentJson(input: {
  systemPrompt: string;
  userMessage: string;
  config: AgentModelConfig;
  callImpl?: AgentCall;
  attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
  review?: boolean;
  maxTokens?: number;
  taskType?: string;
  inputBlockCount?: number;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<Record<string, unknown>> {
  const config = input.config;
  if (!config.apiKey && !PROVIDERS_NO_KEY.has(config.provider)) {
    throw new LearningAgentError('NO_API_KEY', '请先在设置中配置当前模型的 API Key');
  }
  const call = input.callImpl ?? callProvider;
  config.onCallStart?.(input.taskType || 'agent_json');
  const maxAttempts = Math.max(1, Math.min(config.maxAttempts ?? 2, 3));
  const maxTokens = input.maxTokens ?? config.maxTokens ?? 8192;
  let lastError: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    input.onAttempt?.(attempt);
    const startedAt = Date.now();
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    let outputChars = 0;
    let rawText = '';
    try {
      const chunks: string[] = [];
      const run = (async () => {
        for await (const chunk of call(config.provider, {
          systemPrompt: input.systemPrompt,
          userMessage: input.userMessage,
          attachments: input.attachments,
          model: config.model,
          api_key: config.apiKey,
          base_url: config.baseUrl,
          max_tokens: maxTokens,
          temperature: 0.1,
          signal: controller.signal,
          // v1.3.7 的稳定路线：默认显式关闭 thinking。
          // 不传该字段时，部分 DeepSeek 模型会默认输出很长的思考，耗尽预算后才开始 JSON。
          // 只有显式复核且部署方声明支持时才开启。
          ...(input.review && config.supportsThinking && config.reasoningMode !== 'off'
            ? { thinking: 'enabled' as const, reasoning_effort: 'high' as const }
            : { thinking: 'disabled' as const }),
        })) {
          if (chunk.type === 'text-delta') chunks.push(chunk.text);
        }
        rawText = chunks.join('');
        outputChars = rawText.length;
        return rawText;
      })();
      const timeoutMs = config.timeoutMs ?? 75_000;
      const timeoutPromise = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          controller.abort();
          reject(new LearningAgentError('TIMEOUT', '模型调用超时'));
        }, timeoutMs);
      });
      try {
        const text = await Promise.race([run, timeoutPromise]);
        return parseJsonObject(text);
      } finally {
        if (timer) clearTimeout(timer);
        controller.abort();
      }
    } catch (error) {
      lastError = error;
      input.onAttempt?.(attempt, error instanceof Error ? error.message : 'unknown');
      if (error instanceof LearningAgentError && error.code === 'NO_API_KEY') throw error;
    } finally {
      const truncated = lastError instanceof LearningAgentError
        && (lastError.code === 'INVALID_JSON' || lastError.code === 'TIMEOUT');
      config.onCall?.({
        task: input.taskType || 'agent_json',
        input_chars: input.systemPrompt.length + input.userMessage.length,
        input_blocks: input.inputBlockCount ?? 0,
        attachments: input.attachments?.length ?? 0,
        max_tokens: maxTokens,
        output_chars: outputChars,
        output_preview: rawText.slice(0, 4000),
        thinking_text: extractThinkingText(rawText),
        prompt_preview: input.userMessage.slice(0, 1200),
        duration_ms: Date.now() - startedAt,
        truncated,
        attempts: attempt,
        aborted: timedOut,
      });
    }
  }
  if (lastError instanceof LearningAgentError) {
    lastError.attempts = maxAttempts;
    throw lastError;
  }
  throw new LearningAgentError('AGENT_FAILED', lastError instanceof Error ? lastError.message : '模型调用失败', maxAttempts);
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter(Boolean);
}

function normalizeTitle(value: string): string {
  return value.replace(/\s+/g, '').replace(/[：:；;，,。.、]/g, '').toLowerCase();
}

function validateOutline(raw: Record<string, unknown>, knownBlocks: Set<string>): {
  chapters: AgentChapter[];
  errors: string[];
} {
  const errors: string[] = [];
  const chaptersRaw = Array.isArray(raw.chapters) ? raw.chapters : [];
  const chapters: AgentChapter[] = [];
  chaptersRaw.slice(0, 60).forEach((chapterValue) => {
    if (!chapterValue || typeof chapterValue !== 'object') return;
    const chapter = chapterValue as Record<string, unknown>;
    const title = typeof chapter.title === 'string' ? chapter.title.trim().slice(0, 120) : '';
    if (!title) return;
    const sectionsRaw = Array.isArray(chapter.sections) ? chapter.sections : [];
    const sections: AgentSection[] = [];
    sectionsRaw.slice(0, 80).forEach((sectionValue) => {
      if (!sectionValue || typeof sectionValue !== 'object') return;
      const section = sectionValue as Record<string, unknown>;
      const sectionTitle = typeof section.title === 'string' ? section.title.trim().slice(0, 120) : '';
      if (!sectionTitle) return;
      const blockIds = asStringArray(section.block_ids).filter((id) => knownBlocks.has(id));
      if (!blockIds.length) {
        errors.push(`小节「${sectionTitle}」没有有效原文块引用`);
        return;
      }
      sections.push({
        title: sectionTitle,
        summary: typeof section.summary === 'string' ? section.summary.slice(0, 500) : '',
        block_ids: [...new Set(blockIds)],
        objectives: [],
        knowledge_points: [],
        status: 'needs_review',
      });
    });
    if (sections.length) {
      chapters.push({
        title,
        summary: typeof chapter.summary === 'string' ? chapter.summary.slice(0, 500) : '',
        sections,
      });
    }
  });
  return { chapters, errors };
}

function mergeOutlineChapters(target: AgentChapter[], incoming: AgentChapter[]): void {
  incoming.forEach((chapter) => {
    let existingChapter = target.find((item) => normalizeTitle(item.title) === normalizeTitle(chapter.title));
    if (!existingChapter) {
      existingChapter = { ...chapter, sections: [] };
      target.push(existingChapter);
    }
    if (!existingChapter.summary && chapter.summary) existingChapter.summary = chapter.summary;
    chapter.sections.forEach((section) => {
      const existing = existingChapter!.sections.find((item) => normalizeTitle(item.title) === normalizeTitle(section.title));
      if (existing) {
        existing.block_ids = [...new Set([...existing.block_ids, ...section.block_ids])];
        if (!existing.summary && section.summary) existing.summary = section.summary;
      } else {
        existingChapter!.sections.push({ ...section });
      }
    });
  });
}

function normalizeProgramHeading(value: string): string {
  const digits = '０１２３４５６７８９';
  return value.replace(/[０-９]/g, (char) => String(digits.indexOf(char)))
    .replace(/．/g, '.')
    .replace(/\s+/g, ' ')
    .trim();
}

function programChapterTitle(value: string): string {
  return normalizeProgramHeading(value)
    .replace(/^#{1,6}\s*/, '')
    .replace(/\s+[０-９0-9]+\s*$/, '')
    .trim()
    .slice(0, 120);
}

function programTitle(value: string): string {
  return normalizeProgramHeading(value)
    .replace(/^#{1,6}\s*/, '')
    .replace(/^第[一二三四五六七八九十百零\d]+(?:章|单元)\s*[:：.、]?\s*/, '')
    .replace(/^第[一二三四五六七八九十百零\d]+(?:节|课|讲)\s*[:：.、]?\s*/, '')
    .replace(/^([一二三四五六七八九十]+)[、.．]\s*/, '')
    .replace(/^(\d{1,2}(?:\.\d{1,2})+)\s*/, '$1 ')
    .trim()
    .slice(0, 120);
}

/** 程序侧标题候选目录，作为 Agent 目录的结构校验和兜底，不替代语义判断。 */
function isProgramChapterIntro(blocks: LearningSourceBlock[]): boolean {
  return blocks.some((block) => /本章|在本章|本章将/.test(block.text));
}

export function buildProgramOutline(blocks: LearningSourceBlock[]): AgentChapter[] {
  const chapters: AgentChapter[] = [];
  let currentChapter: AgentChapter | null = null;
  let currentSection: AgentSection | null = null;
  let pending: LearningSourceBlock[] = [];
  const ensureChapter = (title: string) => {
    currentChapter = { title: title || '课程内容', summary: '程序按标题候选建立，待 Agent 语义确认', sections: [] };
    chapters.push(currentChapter);
    currentSection = null;
    return currentChapter;
  };
  const ensureSection = (title: string) => {
    if (!currentChapter) ensureChapter('课程内容');
    // 第一个节标题之前的章导学单独成节，避免 8.1 的错误跨页归属。
    if (pending.length && currentChapter!.sections.length === 0 && isProgramChapterIntro(pending)) {
      currentChapter!.sections.push({
        title: '本章导学', summary: '', block_ids: pending.map((block) => block.id),
        objectives: [], knowledge_points: [], status: 'needs_review',
      });
      pending = [];
    }
    currentSection = {
      title: title || '本节内容', summary: '', block_ids: pending.map((block) => block.id),
      objectives: [], knowledge_points: [], status: 'needs_review',
    };
    pending = [];
    currentChapter!.sections.push(currentSection);
    return currentSection;
  };
  blocks.forEach((block) => {
    if (block.heading_level === 'chapter') {
      ensureChapter(programChapterTitle(block.text));
      return;
    }
    if (block.heading_level === 'section') {
      ensureSection(programTitle(block.text));
      currentSection!.block_ids.push(block.id);
      return;
    }
    if (!currentSection) {
      pending.push(block);
      return;
    }
    currentSection.block_ids.push(block.id);
  });
  if (pending.length) {
    if (!currentSection) ensureSection('本节内容');
    currentSection!.block_ids.push(...pending.map((block) => block.id));
  }
  if (!chapters.length) {
    chapters.push({
      title: '课程内容', summary: '未识别到章节标题，按单节材料处理', sections: [{
        title: '本节内容', summary: '', block_ids: blocks.map((block) => block.id),
        objectives: [], knowledge_points: [], status: 'needs_review',
      }],
    });
  }
  chapters.forEach((chapter) => {
    if (!chapter.sections.length) {
      chapter.sections.push({
        title: chapter.title, summary: '', block_ids: [], objectives: [], knowledge_points: [], status: 'needs_review',
      });
    }
    chapter.sections.forEach((section) => {
      section.block_ids = [...new Set(section.block_ids)];
    });
  });
  return chapters;
}

function outlineCounts(chapters: AgentChapter[]): { chapters: number; sections: number; largestSection: number } {
  const sections = chapters.flatMap((chapter) => chapter.sections);
  return {
    chapters: chapters.length,
    sections: sections.length,
    largestSection: sections.reduce((max, section) => Math.max(max, section.block_ids.length), 0),
  };
}

export async function runAgentOutline(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  blocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  attachments?: Array<{ kind: 'image' | 'video'; data_url: string }>;
  onProgress?: (stage: string, batch: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<{ chapters: AgentChapter[]; course: AgentCourseInfo; errors: string[]; batchCount: number; coverage: CoverageStats }> {
  const known = new Set(input.blocks.map((block) => block.id));
  // 明确标题已经给出可靠章/节边界时，目录由程序确定；Agent 只做语义标题和知识点。
  // 这样避免把 600+ 个碎片块塞进目录模型导致截断、重试和 30 分钟级耗时。
  const explicitProgram = buildProgramOutline(input.blocks);
  const explicitCounts = outlineCounts(explicitProgram);
  if (!input.callImpl && explicitCounts.chapters >= 1 && explicitCounts.sections >= 2) {
    const allChunks = chunkSourceBlocks(input.blocks, MAX_PROMPT_CHARS);
    const processed = new Set(allChunks.flatMap((chunk) => chunk.segments.map(rangeKey)));
    const cited = new Set(explicitProgram.flatMap((chapter) => chapter.sections.flatMap((section) => section.block_ids)));
    input.onProgress?.('outline', 1, 1);
    return {
      chapters: explicitProgram,
      course: {
        title: input.courseTitle,
        subject: input.subject,
        grade: input.grade,
        book_info: '',
      },
      errors: [],
      batchCount: 0,
      coverage: coverageFrom(input.blocks, allChunks, processed, [], 0, cited),
    };
  }
  const chunks = chunkSourceBlocks(input.blocks, MAX_PROMPT_CHARS);
  const chapters: AgentChapter[] = [];
  const errors: string[] = [];
  const processed = new Set<string>();
  const failedRanges: CoverageRange[] = [];
  const allChunks: SourceChunk[] = [];
  let batchCount = 0;
  let pointCount = 0;
  const citedBlocks = new Set<string>();
  let courseInfo: AgentCourseInfo = {
    title: input.courseTitle,
    subject: input.subject,
    grade: input.grade,
    book_info: '',
  };
  let courseInfoSet = false;
  let chunkIndex = 0;
  let lastError: unknown;

  const extractChunk = async (chunk: SourceChunk, depth: number): Promise<void> => {
    chunkIndex += 1;
    batchCount += 1;
    input.onProgress?.('outline', chunkIndex, chunks.length);
    try {
      const raw = await callAgentJson({
        systemPrompt: AGENT_SYSTEM_PROMPT,
        userMessage: buildOutlinePrompt({
          courseTitle: input.courseTitle,
          subject: input.subject,
          grade: input.grade,
          blocks: promptBlocks(chunk),
          hasImages: Boolean(input.attachments?.length),
        }),
        config: { ...input.config, maxAttempts: 1 },
        callImpl: input.callImpl,
        attachments: input.attachments,
        taskType: 'outline',
        inputBlockCount: chunk.segments.length,
        onAttempt: input.onAttempt,
      });
      const rawCourse = raw.course && typeof raw.course === 'object' ? raw.course as Record<string, unknown> : {};
      if (!courseInfoSet) {
        courseInfo = {
          title: typeof rawCourse.title === 'string' && rawCourse.title.trim() ? rawCourse.title.trim().slice(0, 120) : input.courseTitle,
          subject: typeof rawCourse.subject === 'string' ? rawCourse.subject.trim().slice(0, 20) : input.subject,
          grade: typeof rawCourse.grade === 'string' ? rawCourse.grade.trim().slice(0, 20) : input.grade,
          book_info: typeof rawCourse.book_info === 'string' ? rawCourse.book_info.trim().slice(0, 300) : '',
        };
        courseInfoSet = true;
      }
      const validated = validateOutline(raw, known);
      validated.chapters.forEach((chapter) => {
        chapter.sections.forEach((section) => {
          section.block_ids.forEach((id) => citedBlocks.add(id));
          pointCount += section.objectives.length;
        });
      });
      chunk.segments.forEach((segment) => processed.add(rangeKey(segment)));
      errors.push(...validated.errors);
      mergeOutlineChapters(chapters, validated.chapters);
    } catch (error) {
      lastError = error;
      if (error instanceof LearningAgentError && error.code === 'NO_API_KEY') throw error;
      const canSplit = depth < MAX_SPLIT_DEPTH
        && (error instanceof LearningAgentError)
        && (error.code === 'INVALID_JSON' || error.code === 'TIMEOUT' || error.code === 'AGENT_FAILED');
      const halves = canSplit ? splitSourceChunk(chunk) : null;
      if (halves) {
        errors.push(`目录片段 ${chunk.segments[0].start}-${chunk.segments[chunk.segments.length - 1].end} 输出截断，已拆分重试`);
        await extractChunk(halves[0], depth + 1);
        await extractChunk(halves[1], depth + 1);
        return;
      }
      const reason = error instanceof Error ? error.message.slice(0, 160) : '未知错误';
      chunk.segments.forEach((segment) => failedRanges.push({
        block_id: segment.blockId, start_offset: segment.start, end_offset: segment.end, reason,
      }));
      errors.push(`目录片段 ${chunk.segments[0].blockId}:${chunk.segments[0].start}-${chunk.segments[chunk.segments.length - 1].end} 解析失败：${reason}`);
    }
  };

  for (const chunk of chunks) {
    allChunks.push(chunk);
    await extractChunk(chunk, 0);
  }
  // 程序已能从标题层级确定章节/小节边界时，原文块归属以程序结构为准。
  // Agent 仍负责语义标题/说明，但不能只引用部分块，导致前面章节内容缺失。
  const programForOwnership = buildProgramOutline(input.blocks);
  const programOwnershipCounts = outlineCounts(programForOwnership);
  const agentOwnershipCounts = outlineCounts(chapters);
  const sameShape = programOwnershipCounts.chapters === agentOwnershipCounts.chapters
    && programOwnershipCounts.sections === agentOwnershipCounts.sections;
  const sameTitles = sameShape
    && programForOwnership.every((chapter, ci) => chapter.sections.every((section, si) => (
      normalizeTitle(section.title) === normalizeTitle(chapters[ci]?.sections[si]?.title || '')
    )));
  if (sameShape && sameTitles && programOwnershipCounts.sections >= 2) {
    chapters.forEach((chapter, ci) => chapter.sections.forEach((section, si) => {
      section.block_ids = [...programForOwnership[ci].sections[si].block_ids];
    }));
  }

  // Agent 漏掉的原文块不能消失：归入行号最近的小节，并记录具体块类型和警告。
  const assigned = new Set(chapters.flatMap((chapter) => chapter.sections.flatMap((section) => section.block_ids)));
  const sections = chapters.flatMap((chapter) => chapter.sections.map((section) => ({ chapter, section })));
  input.blocks.filter((block) => !assigned.has(block.id)).forEach((block) => {
    if (!sections.length) return;
    const nearest = sections.reduce((best, item) => {
      const blockStart = block.line_start;
      const distance = Math.min(
        ...item.section.block_ids.map((id) => {
          const source = input.blocks.find((candidate) => candidate.id === id);
          return source ? Math.abs(source.line_start - blockStart) : Number.MAX_SAFE_INTEGER;
        }),
      );
      const bestDistance = Math.min(
        ...best.section.block_ids.map((id) => {
          const source = input.blocks.find((candidate) => candidate.id === id);
          return source ? Math.abs(source.line_start - blockStart) : Number.MAX_SAFE_INTEGER;
        }),
      );
      return distance < bestDistance ? item : best;
    }, sections[0]);
    nearest.section.block_ids.push(block.id);
    errors.push(`原文块 ${block.id}（${block.block_type}）未被 Agent 分组，已归入小节「${nearest.section.title}」并保留原文`);
  });

  const assignedOnce = new Set<string>();
  chapters.forEach((chapter) => chapter.sections.forEach((section) => {
    const kept: string[] = [];
    section.block_ids.forEach((id) => {
      if (assignedOnce.has(id)) {
        errors.push(`小节「${section.title}」重复归属原文块 ${id}，已移除重复引用`);
        return;
      }
      assignedOnce.add(id);
      kept.push(id);
    });
    section.block_ids = kept;
  }));

  const program = buildProgramOutline(input.blocks);
  const programCounts = outlineCounts(program);
  const agentCounts = outlineCounts(chapters);
  const programHasMoreStructure = programCounts.sections > agentCounts.sections
    || programCounts.chapters > agentCounts.chapters;
  const collapsed = programCounts.sections >= 2 && agentCounts.sections <= 1;
  const mergedChapters = programCounts.chapters >= 2 && agentCounts.chapters <= 1;
  const oversizedSingleSection = agentCounts.sections === 1 && agentCounts.largestSection >= 20
    && programCounts.sections >= 2;
  // 程序能从明确标题识别出至少两个小节时，多出来的“习题/阅读/小结”不能
  // 自动变成并列小节；否则目录会被页眉和练习编号污染。
  const explicitStructureMismatch = programCounts.sections >= 2
    && agentCounts.sections !== programCounts.sections;
  if (collapsed || mergedChapters || oversizedSingleSection || explicitStructureMismatch) {
    errors.push(
      `目录结构异常：程序识别到 ${programCounts.chapters} 个章候选、${programCounts.sections} 个小节候选，`
      + `Agent 返回 ${agentCounts.chapters} 章、${agentCounts.sections} 小节；`
      + `已以程序标题结构为准并标记待确认`,
    );
    chapters.splice(0, chapters.length, ...program);
    chapters.forEach((chapter) => chapter.sections.forEach((section) => { section.status = 'needs_review'; }));
    // Agent 漏块/重复块是采用程序结构前的过程信息；程序已完整接管归属后，
    // 不再把这些已修复的逐块警告重复计入最终结构质量。
    const kept = errors.filter((message) => !message.includes('未被 Agent 分组') && !message.includes('重复归属原文块'));
    errors.splice(0, errors.length, ...kept);
  } else if (programHasMoreStructure && agentCounts.sections >= 1) {
    errors.push(`目录结构提示：程序识别到 ${programCounts.sections} 个小节候选，Agent 返回 ${agentCounts.sections} 个；请核对层级`);
  }

  if (!chapters.length) {
    if (lastError instanceof LearningAgentError && (lastError.code === 'TIMEOUT' || lastError.code === 'NO_API_KEY')) {
      throw lastError;
    }
    throw new LearningAgentError('INVALID_STRUCTURE', '模型没有返回可用的章节/小节结构', 1);
  }
  return {
    chapters, course: courseInfo, errors, batchCount,
    coverage: coverageFrom(input.blocks, allChunks, processed, failedRanges, pointCount, citedBlocks),
  };
}

function validateSectionPoints(raw: Record<string, unknown>, sectionBlockIds: Set<string>): {
  objectives: string[];
  points: AgentKnowledgePoint[];
  errors: string[];
} {
  const errors: string[] = [];
  const objectives = asStringArray(raw.objectives);
  const pointsRaw = Array.isArray(raw.knowledge_points) ? raw.knowledge_points : [];
  const points: AgentKnowledgePoint[] = [];
  pointsRaw.forEach((pointValue) => {
    if (!pointValue || typeof pointValue !== 'object') return;
    const point = pointValue as Record<string, unknown>;
    const title = typeof point.title === 'string' ? point.title.trim().slice(0, 120) : '';
    const explanation = typeof point.explanation === 'string' ? point.explanation.trim().slice(0, 20000) : '';
    if (!title) return;
    const blockIds = asStringArray(point.block_ids).filter((id) => sectionBlockIds.has(id));
    if (!blockIds.length) {
      errors.push(`知识点「${title}」没有有效原文块引用，已丢弃`);
      return;
    }
    const origin = point.origin === 'agent_inferred' ? 'agent_inferred' : 'textbook';
    const confidenceRaw = typeof point.confidence === 'number' ? point.confidence : 0.5;
    const confidence = Math.max(0, Math.min(1, confidenceRaw));
    const needsReview = point.needs_review === true || confidence < 0.6;
    points.push({
      title, explanation,
      concepts: asStringArray(point.concepts),
      formulas: asStringArray(point.formulas),
      methods: asStringArray(point.methods),
      prerequisites: asStringArray(point.prerequisites),
      block_ids: [...new Set(blockIds)],
      origin,
      status: needsReview ? 'needs_review' : 'parsed',
      confidence,
    });
  });
  return { objectives, points, errors };
}

export async function runAgentSections(input: {
  courseTitle: string;
  chapters: AgentChapter[];
  allBlocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  imagesByBlock?: Map<string, Array<{ kind: 'image' | 'video'; data_url: string }>>;
  onProgress?: (stage: string, section: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<{ chapters: AgentChapter[]; errors: string[]; batchCount: number; coverage: CoverageStats }> {
  const blockMap = new Map(input.allBlocks.map((block) => [block.id, block]));
  const errors: string[] = [];
  const processed = new Set<string>();
  const failedRanges: CoverageRange[] = [];
  const allChunks: SourceChunk[] = [];
  const citedBlocks = new Set<string>();
  let batchCount = 0;
  let pointCount = 0;
  const totalSections = input.chapters.reduce((sum, chapter) => sum + chapter.sections.length, 0);
  const tasks: Array<{ chapter: AgentChapter; section: AgentSection }> = [];
  input.chapters.forEach((chapter) => chapter.sections.forEach((section) => tasks.push({ chapter, section })));
  let nextTask = 0;
  let done = 0;

  const runOne = async (chapter: AgentChapter, section: AgentSection): Promise<void> => {
    const sectionBlocks = section.block_ids
      .map((id) => blockMap.get(id))
      .filter((block): block is LearningSourceBlock => Boolean(block));
    if (!sectionBlocks.length) {
      errors.push(`小节「${section.title}」没有可读取原文，已标记待确认`);
      section.status = 'needs_review';
      return;
    }
    const sectionIds = new Set(sectionBlocks.map((block) => block.id));
    const sectionImages = [...new Set(section.block_ids.flatMap((id) => input.imagesByBlock?.get(id) || []))];
    const chunks = chunkSourceBlocks(sectionBlocks, MAX_SECTION_CHARS);
    allChunks.push(...chunks);
    const allPoints: AgentKnowledgePoint[] = [];
    const allObjectives: string[] = [];

    const extractChunk = async (chunk: SourceChunk, depth: number): Promise<void> => {
      batchCount += 1;
      input.onProgress?.('sections', done, totalSections);
      try {
        const raw = await callAgentJson({
          systemPrompt: AGENT_SYSTEM_PROMPT,
          userMessage: buildSectionPrompt({
            courseTitle: input.courseTitle,
            chapterTitle: chapter.title,
            sectionTitle: section.title,
            blocks: promptBlocks(chunk),
            previousTitles: allPoints.map((point) => point.title),
            hasImages: sectionImages.length > 0,
          }),
          config: { ...input.config, maxAttempts: 1 },
          callImpl: input.callImpl,
          attachments: sectionImages,
          taskType: 'knowledge_points',
          inputBlockCount: chunk.segments.length,
          onAttempt: input.onAttempt,
        });
        const validated = validateSectionPoints(raw, sectionIds);
        validated.points.forEach((point) => {
          point.source_ranges = chunk.segments.map((segment) => ({
            block_id: segment.blockId, start_offset: segment.start, end_offset: segment.end,
          }));
          point.block_ids.forEach((id) => citedBlocks.add(id));
        });
        chunk.segments.forEach((segment) => processed.add(rangeKey(segment)));
        allObjectives.push(...validated.objectives);
        allPoints.push(...validated.points);
        pointCount += validated.points.length;
        errors.push(...validated.errors);
      } catch (error) {
        const canSplit = depth < MAX_SPLIT_DEPTH
          && (error instanceof LearningAgentError)
          && (error.code === 'INVALID_JSON' || error.code === 'TIMEOUT' || error.code === 'AGENT_FAILED');
        const halves = canSplit ? splitSourceChunk(chunk) : null;
        if (halves) {
          errors.push(`小节「${section.title}」知识点片段 ${chunk.segments[0].start}-${chunk.segments[chunk.segments.length - 1].end} 输出截断，已拆成两个更小片段重试`);
          await extractChunk(halves[0], depth + 1);
          await extractChunk(halves[1], depth + 1);
          return;
        }
        const reason = error instanceof Error ? error.message.slice(0, 160) : '未知错误';
        chunk.segments.forEach((segment) => failedRanges.push({
          block_id: segment.blockId, start_offset: segment.start, end_offset: segment.end, reason,
        }));
        errors.push(`小节「${section.title}」原文片段 ${chunk.segments[0].blockId}:${chunk.segments[0].start}-${chunk.segments[chunk.segments.length - 1].end} 未能提取知识点：${reason}`);
        section.status = 'needs_review';
      }
    };

    for (const chunk of chunks) await extractChunk(chunk, 0);

    // 同一小节内去重：同名知识点合并来源块和片段范围。
    const merged = new Map<string, AgentKnowledgePoint>();
    allPoints.forEach((point) => {
      const key = normalizeTitle(point.title);
      const existing = merged.get(key);
      if (!existing) {
        merged.set(key, point);
        return;
      }
      existing.block_ids = [...new Set([...existing.block_ids, ...point.block_ids])];
      existing.source_ranges = [
        ...(existing.source_ranges || []), ...(point.source_ranges || []),
      ].filter((range, index, values) => values.findIndex((item) => (
        item.block_id === range.block_id && item.start_offset === range.start_offset && item.end_offset === range.end_offset
      )) === index);
      existing.concepts = [...new Set([...existing.concepts, ...point.concepts])];
      existing.formulas = [...new Set([...existing.formulas, ...point.formulas])];
      existing.methods = [...new Set([...existing.methods, ...point.methods])];
      existing.prerequisites = [...new Set([...existing.prerequisites, ...point.prerequisites])];
      existing.confidence = Math.max(existing.confidence, point.confidence);
      if (point.origin === 'textbook') existing.origin = 'textbook';
      if (point.status === 'parsed') existing.status = 'parsed';
    });
    section.objectives = [...new Set(allObjectives)];
    section.knowledge_points = [...merged.values()];
    section.status = section.knowledge_points.some((point) => point.status === 'parsed')
      ? 'parsed'
      : 'needs_review';
    if (!section.knowledge_points.length) {
      errors.push(`小节「${section.title}」没有提取出可验证的知识点，已标记待确认`);
    }
  };

  const worker = async (): Promise<void> => {
    while (true) {
      const index = nextTask;
      nextTask += 1;
      if (index >= tasks.length) return;
      await runOne(tasks[index].chapter, tasks[index].section);
      done += 1;
      input.onProgress?.('sections', done, totalSections);
    }
  };

  const concurrency = Math.min(Math.max(1, input.config.sectionConcurrency ?? 3), 4);
  await Promise.all(Array.from({ length: Math.min(concurrency, Math.max(tasks.length, 1)) }, () => worker()));
  const coverage = coverageFrom(input.allBlocks, allChunks, processed, failedRanges, pointCount, citedBlocks);
  return { chapters: input.chapters, errors, batchCount, coverage };
}

const LESSON_SOURCES: LessonSourceKind[] = ['textbook', 'derived', 'supplemental', 'needs_review'];

function sourceKind(value: unknown): LessonSourceKind {
  return typeof value === 'string' && (LESSON_SOURCES as string[]).includes(value)
    ? value as LessonSourceKind
    : 'needs_review';
}

function designBlockIds(value: unknown, allowed: Set<string>): string[] {
  return asStringArray(value).filter((id) => allowed.has(id));
}

function balancedFormula(value: string): boolean {
  const dollars = (value.match(/\$/g) || []).length;
  return dollars % 2 === 0;
}

function noteFrom(value: unknown, allowed: Set<string>, label: string, errors: string[]): LessonDesignNote | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const text = typeof raw.text === 'string' ? raw.text.trim().slice(0, 4000) : '';
  if (!text) return null;
  const source = sourceKind(raw.source);
  const blockIds = designBlockIds(raw.block_ids, allowed);
  if ((source === 'textbook' || source === 'derived') && !blockIds.length) {
    errors.push(`学案「${label}」标记为${source}，但没有有效原文块引用`);
  }
  if (!balancedFormula(text)) errors.push(`学案「${label}」的公式定界符不完整`);
  return { text, source, block_ids: blockIds };
}

export function validateLessonDesign(
  raw: Record<string, unknown>,
  sectionBlockIds: Set<string>,
): { design: AgentLessonDesign; errors: string[]; status: DesignStatus } {
  const errors: string[] = [];
  const objectives: LessonDesignObjective[] = [];
  (Array.isArray(raw.objectives) ? raw.objectives : []).slice(0, 12).forEach((item) => {
    const value = typeof item === 'string'
      ? { text: item, kind: 'knowledge' }
      : (item && typeof item === 'object' ? item as Record<string, unknown> : null);
    if (!value) return;
    const text = typeof value.text === 'string' ? value.text.trim().slice(0, 500) : '';
    if (!text) return;
    const kind = value.kind === 'method' || value.kind === 'application' ? value.kind : 'knowledge';
    objectives.push({ text, kind });
  });

  const preparation = (Array.isArray(raw.preparation) ? raw.preparation : [])
    .map((item) => noteFrom(item, sectionBlockIds, '学习准备', errors))
    .filter((item): item is LessonDesignNote => Boolean(item))
    .slice(0, 12);

  const introduction = noteFrom(raw.introduction, sectionBlockIds, '情境导入', errors);

  const teachingFlow: LessonDesignItem[] = [];
  (Array.isArray(raw.teaching_flow) ? raw.teaching_flow : []).slice(0, 20).forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const value = item as Record<string, unknown>;
    const title = typeof value.title === 'string' ? value.title.trim().slice(0, 160) : '';
    const body = typeof value.body === 'string' ? value.body.trim().slice(0, 6000) : '';
    if (!title || !body) return;
    if (body.length < 12) errors.push(`核心学习「${title}」内容过短，可能只是标题式罗列`);
    const source = sourceKind(value.source);
    const blockIds = designBlockIds(value.block_ids, sectionBlockIds);
    if ((source === 'textbook' || source === 'derived') && !blockIds.length) {
      errors.push(`核心学习「${title}」标记为${source}，但没有有效原文块引用`);
    }
    if (!balancedFormula(body)) errors.push(`核心学习「${title}」的公式定界符不完整`);
    const formulas = asStringArray(value.formulas).slice(0, 12);
    formulas.forEach((formula) => {
      if (!balancedFormula(formula)) errors.push(`核心学习「${title}」的公式「${formula.slice(0, 40)}」定界符不完整`);
    });
    teachingFlow.push({
      kind: typeof value.kind === 'string' ? value.kind.slice(0, 30) : 'concept',
      title, body, bullets: asStringArray(value.bullets).slice(0, 12), formulas,
      source, block_ids: blockIds,
    });
  });

  const examples: LessonDesignExample[] = [];
  (Array.isArray(raw.examples) ? raw.examples : []).slice(0, 12).forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const value = item as Record<string, unknown>;
    const title = typeof value.title === 'string' ? value.title.trim().slice(0, 160) : '';
    const problem = typeof value.problem === 'string' ? value.problem.trim().slice(0, 3000) : '';
    if (!title || !problem) return;
    const source = sourceKind(value.source);
    const blockIds = designBlockIds(value.block_ids, sectionBlockIds);
    if (source === 'needs_review') errors.push(`例题「${title}」没有明确来源`);
    if ((source === 'textbook' || source === 'derived') && !blockIds.length) {
      errors.push(`例题「${title}」标记为${source}，但没有有效原文块引用`);
    }
    examples.push({
      title, problem, steps: asStringArray(value.steps).slice(0, 12),
      answer: typeof value.answer === 'string' ? value.answer.trim().slice(0, 2000) : '',
      source, block_ids: blockIds,
    });
  });

  const misconceptions = (Array.isArray(raw.misconceptions) ? raw.misconceptions : [])
    .map((item) => noteFrom(item, sectionBlockIds, '易错提醒', errors))
    .filter((item): item is LessonDesignNote => Boolean(item))
    .slice(0, 12);

  const activities: LessonDesignActivity[] = [];
  (Array.isArray(raw.activities) ? raw.activities : []).slice(0, 12).forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const value = item as Record<string, unknown>;
    const prompt = typeof value.prompt === 'string' ? value.prompt.trim().slice(0, 1000) : '';
    if (!prompt) return;
    if (/^(认真|积极|仔细)?(阅读|听讲|思考|讨论|预习|复习)[。.!！]?$/.test(prompt) || prompt.length < 12) {
      errors.push('学习活动过于空泛，缺少具体任务');
    }
    activities.push({
      prompt,
      purpose: typeof value.purpose === 'string' ? value.purpose.trim().slice(0, 500) : '',
      source: sourceKind(value.source),
      block_ids: designBlockIds(value.block_ids, sectionBlockIds),
    });
  });

  const summary = noteFrom(raw.summary, sectionBlockIds, '本节小结', errors);
  const selfCheck = (Array.isArray(raw.self_check) ? raw.self_check : []).slice(0, 12).map((item) => {
    if (!item || typeof item !== 'object') return null;
    const value = item as Record<string, unknown>;
    const question = typeof value.question === 'string' ? value.question.trim().slice(0, 1000) : '';
    if (!question) return null;
    return {
      question,
      answer_hint: typeof value.answer_hint === 'string' ? value.answer_hint.trim().slice(0, 1000) : '',
      source: sourceKind(value.source),
      block_ids: designBlockIds(value.block_ids, sectionBlockIds),
    };
  }).filter((item): item is { question: string; answer_hint: string; source: LessonSourceKind; block_ids: string[] } => Boolean(item));

  const sourceItems = [
    ...preparation, ...(introduction ? [introduction] : []), ...teachingFlow,
    ...examples, ...misconceptions, ...activities, ...(summary ? [summary] : []), ...selfCheck,
  ];
  const citedBlocks = new Set(sourceItems.flatMap((item) => item.block_ids));
  if (!citedBlocks.size) errors.push('整份学案没有引用任何有效原文块');
  if (!objectives.length) errors.push('学案缺少学习目标');
  if (!teachingFlow.length) errors.push('学案缺少核心知识学习内容');
  if (!summary) errors.push('学案缺少本节小结');
  const supplementalCount = sourceItems.filter((item) => item.source === 'supplemental').length;
  if (sourceItems.length && supplementalCount / sourceItems.length > 0.5) {
    errors.push('学案中教学补充内容占比过高，需核对与教材的一致性');
  }

  let status: DesignStatus = raw.design_status === 'complete' || raw.design_status === 'partial' || raw.design_status === 'needs_review'
    ? raw.design_status
    : 'complete';
  if (!objectives.length || !teachingFlow.length || !citedBlocks.size) status = 'needs_review';
  else if (errors.length >= 5) status = 'needs_review';
  else if (errors.length > 0) status = status === 'complete' ? 'partial' : status;

  return {
    design: {
      objectives, preparation, introduction, teaching_flow: teachingFlow, examples,
      misconceptions, activities, summary, self_check: selfCheck, status,
    },
    errors,
    status,
  };
}

export async function runAgentLessonDesigns(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  chapters: AgentChapter[];
  allBlocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  imagesByBlock?: Map<string, Array<{ kind: 'image' | 'video'; data_url: string }>>;
  onProgress?: (stage: string, current: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<{ errors: string[]; stats: LessonDesignStats }> {
  const blockMap = new Map(input.allBlocks.map((block) => [block.id, block]));
  const tasks: Array<{ chapter: AgentChapter; section: AgentSection }> = [];
  input.chapters.forEach((chapter) => chapter.sections.forEach((section) => tasks.push({ chapter, section })));
  const errors: string[] = [];
  const stats: LessonDesignStats = { total: tasks.length, complete: 0, partial: 0, needs_review: 0, failed: 0, part_failures: 0 };
  let nextTask = 0;
  let done = 0;

  const parts: Array<{ part: LessonDesignPart; maxTokens: number }> = [
    { part: 'A', maxTokens: 4000 },
    { part: 'B', maxTokens: 4000 },
    { part: 'C1', maxTokens: 2200 },
    { part: 'C2', maxTokens: 1500 },
    { part: 'D1', maxTokens: 3000 },
    { part: 'D2', maxTokens: 1800 },
  ];

  const runOne = async (chapter: AgentChapter, section: AgentSection): Promise<void> => {
    if (!section.knowledge_points.length) {
      section.design_status = 'needs_review';
      stats.needs_review += 1;
      return;
    }
    const allSectionBlocks = section.block_ids
      .map((id) => blockMap.get(id))
      .filter((block): block is LearningSourceBlock => Boolean(block));
    const sectionIds = new Set(allSectionBlocks.map((block) => block.id));
    const sectionImages = [...new Set(section.block_ids.flatMap((id) => input.imagesByBlock?.get(id) || []))];
    const chunks = chunkSourceBlocks(allSectionBlocks, 12_000);
    const promptFor = (part: LessonDesignPart, chunk: SourceChunk) => buildLessonDesignPartPrompt({
      courseTitle: input.courseTitle,
      subject: input.subject,
      grade: input.grade,
      chapterTitle: chapter.title,
      sectionTitle: section.title,
      objectives: section.objectives,
      knowledgePoints: section.knowledge_points.map((point) => ({
        title: point.title, explanation: point.explanation, concepts: point.concepts,
        formulas: point.formulas, methods: point.methods, origin: point.origin, status: point.status,
      })),
      blocks: promptBlocks(chunk),
      hasImages: sectionImages.length > 0,
    }, part);

    const partResults: Partial<Record<LessonDesignPart, Record<string, unknown>>> = {};
    let partFailures = 0;
    let reviewFirst = input.config.reasoningMode === 'always' && input.config.supportsThinking === true;
    const runPart = async (part: LessonDesignPart, maxTokens: number): Promise<void> => {
      // 同一部分如果内容跨多个片段，每个片段独立生成，避免整份学案一次性返回。
      for (const chunk of chunks) {
        try {
          const raw = await callAgentJson({
            systemPrompt: AGENT_SYSTEM_PROMPT,
            userMessage: promptFor(part, chunk),
            config: input.config,
            callImpl: input.callImpl,
            attachments: sectionImages,
            review: reviewFirst,
            maxTokens,
            taskType: `lesson_design_${part}`,
            inputBlockCount: chunk.segments.length,
            onAttempt: input.onAttempt,
          });
          const existing = partResults[part] || {};
          // 多片段结果按数组字段合并，后面的片段不会覆盖前面的成功内容。
          Object.keys(raw).forEach((key) => {
            const value = raw[key];
            if (Array.isArray(value)) {
              const merged = Array.isArray(existing[key]) ? existing[key] as unknown[] : [];
              existing[key] = [...merged, ...value];
            } else if (existing[key] === undefined) {
              existing[key] = value;
            }
          });
          partResults[part] = existing;
        } catch (error) {
          partFailures += 1;
          stats.part_failures = (stats.part_failures || 0) + 1;
          const message = error instanceof Error ? error.message.slice(0, 160) : '未知错误';
          errors.push(`小节「${section.title}」学案部分 ${part} 生成失败：${message}`);
          if (input.config.reasoningMode === 'auto' && input.config.supportsThinking === true && reviewFirst === false) {
            try {
              const raw = await callAgentJson({
                systemPrompt: AGENT_SYSTEM_PROMPT,
                userMessage: promptFor(part, chunk),
                config: input.config,
                callImpl: input.callImpl,
                attachments: sectionImages,
                review: true,
                maxTokens,
                taskType: `lesson_design_${part}_review`,
                inputBlockCount: chunk.segments.length,
                onAttempt: input.onAttempt,
              });
              partResults[part] = raw;
              reviewFirst = true;
            } catch (reviewError) {
              errors.push(`小节「${section.title}」学案部分 ${part} 复核仍失败：${reviewError instanceof Error ? reviewError.message.slice(0, 120) : '未知错误'}`);
            }
          }
        }
      }
    };

    // 每个 section 最多两个 part 并行，避免把 provider 并发打满。
    let nextPart = 0;
    const partWorker = async (): Promise<void> => {
      while (true) {
        const index = nextPart;
        nextPart += 1;
        if (index >= parts.length) return;
        await runPart(parts[index].part, parts[index].maxTokens);
      }
    };
    await Promise.all(Array.from({ length: Math.min(2, parts.length) }, () => partWorker()));

    const mergedRaw: Record<string, unknown> = {};
    parts.forEach(({ part }) => {
      const value = partResults[part];
      if (value) Object.assign(mergedRaw, value);
    });
    const validated = validateLessonDesign(mergedRaw, sectionIds);
    const finalStatus: DesignStatus = partFailures > 0 ? 'needs_review' : validated.status;
    section.lesson_design = validated.design;
    section.design_status = finalStatus;
    if (finalStatus === 'complete') stats.complete += 1;
    else if (finalStatus === 'partial') stats.partial += 1;
    else stats.needs_review += 1;
    validated.errors.forEach((error) => errors.push(`小节「${section.title}」学案：${error}`));
  };

  const worker = async (): Promise<void> => {
    while (true) {
      const index = nextTask;
      nextTask += 1;
      if (index >= tasks.length) return;
      await runOne(tasks[index].chapter, tasks[index].section);
      done += 1;
      input.onProgress?.('lesson_design', done, tasks.length);
    }
  };
  const concurrency = Math.min(Math.max(1, input.config.designConcurrency ?? 2), 4);
  await Promise.all(Array.from({ length: Math.min(concurrency, Math.max(tasks.length, 1)) }, () => worker()));
  return { errors, stats };
}

/** 全局校验：重复知识点、未知前置、循环依赖。 */
export function validateAgentGraph(chapters: AgentChapter[]): {
  errors: string[];
  duplicates: string[];
  cycles: string[];
} {
  const errors: string[] = [];
  const duplicates: string[] = [];
  const points = chapters.flatMap((chapter) => chapter.sections.flatMap((section) => section.knowledge_points));
  const titleMap = new Map<string, AgentKnowledgePoint[]>();
  points.forEach((point) => {
    const key = normalizeTitle(point.title);
    titleMap.set(key, [...(titleMap.get(key) || []), point]);
  });
  titleMap.forEach((items, key) => {
    if (items.length > 1) {
      duplicates.push(items[0].title);
      items.forEach((point) => { point.status = 'needs_review'; });
      void key;
    }
  });

  const byTitle = new Map<string, AgentKnowledgePoint>();
  points.forEach((point) => {
    const key = normalizeTitle(point.title);
    if (!byTitle.has(key)) byTitle.set(key, point);
  });
  points.forEach((point) => {
    point.prerequisites = point.prerequisites.filter((name) => {
      const key = normalizeTitle(name);
      if (!byTitle.has(key) || key === normalizeTitle(point.title)) {
        errors.push(`知识点「${point.title}」的前置「${name}」不存在，已移除`);
        return false;
      }
      return true;
    });
  });

  // 循环检测：DFS；检测到环时清除相关前置并标记待确认。
  const cycles: string[] = [];
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (key: string, path: string[]): void => {
    if (visiting.has(key)) {
      const cycle = [...path.slice(path.indexOf(key)), key];
      cycles.push(cycle.map((item) => byTitle.get(item)?.title || item).join(' → '));
      byTitle.get(key)!.prerequisites = [];
      byTitle.get(key)!.status = 'needs_review';
      return;
    }
    if (visited.has(key)) return;
    visiting.add(key);
    const point = byTitle.get(key);
    point?.prerequisites.forEach((name) => visit(normalizeTitle(name), [...path, key]));
    visiting.delete(key);
    visited.add(key);
  };
  [...byTitle.keys()].forEach((key) => visit(key, []));

  const needsReview = points.filter((point) => point.status === 'needs_review').length;
  if (points.length > 0 && needsReview / points.length > 0.5) {
    errors.push(`待确认知识点占比过高：${needsReview}/${points.length}`);
  }
  return { errors, duplicates, cycles };
}

export async function repairFallbackPages(
  input: {
    text: string;
    images?: Array<{ kind: 'image' | 'video'; data_url: string; page?: number | null; is_fallback?: boolean }>;
    audit?: { page_records?: Array<Record<string, unknown>>; fallback_pages?: number[]; unreadable_pages?: number[] } | undefined;
  },
  config: AgentModelConfig,
  callImpl?: AgentCall,
): Promise<{ text: string; errors: string[]; repairedPages: number[]; failedPages: number[] }> {
  const errors: string[] = [];
  const repairedPages: number[] = [];
  const failedPages: number[] = [];
  const candidates = (input.images || []).filter((image) => image.is_fallback && image.page && image.data_url);
  if (!candidates.length) return { text: input.text, errors, repairedPages, failedPages };

  const runOne = async (image: { kind: 'image' | 'video'; data_url: string; page?: number | null }) => {
    const page = Number(image.page);
    try {
      const raw = await callAgentJson({
        systemPrompt: '你是教材页面校对员。只转录图片中的可见文字、公式和表格；不要补写图片没有的内容。无法辨认的字符用 [无法辨认] 标记。只输出 JSON：{"text":"...","notes":"..."}。',
        userMessage: `请转录第 ${page} 页的教材内容。保留 LaTeX 公式、上下标、希腊字母和数学符号；如果页面含有无法辨认的字符，保留 [无法辨认] 位置，不要编造。`,
        config,
        callImpl,
        attachments: [{ kind: 'image', data_url: image.data_url }],
        maxTokens: 3000,
        taskType: 'vision_page_repair',
        inputBlockCount: 0,
      });
      const text = typeof raw.text === 'string' ? raw.text.trim() : '';
      if (text.length < 4) throw new Error('视觉转录没有返回有效文字');
      const record = input.audit?.page_records?.find((item) => Number(item.page) === page);
      if (record) {
        record.original_status = record.status;
        record.status = 'vision_repaired';
        record.vision_repair = { status: 'accepted', chars: text.length, notes: typeof raw.notes === 'string' ? raw.notes.slice(0, 300) : '' };
      }
      repairedPages.push(page);
      return { page, text };
    } catch (error) {
      failedPages.push(page);
      const record = input.audit?.page_records?.find((item) => Number(item.page) === page);
      if (record) {
        record.status = 'vision_repair_failed';
        record.vision_repair = { status: 'failed', reason: error instanceof Error ? error.message.slice(0, 200) : '视觉转录失败' };
      }
      errors.push(`第 ${page} 页页面图像视觉转录失败：${error instanceof Error ? error.message.slice(0, 120) : '未知错误'}`);
      return null;
    }
  };

  const results: Array<{ page: number; text: string }> = [];
  let next = 0;
  const worker = async () => {
    while (true) {
      const index = next;
      next += 1;
      if (index >= candidates.length) return;
      const result = await runOne(candidates[index]);
      if (result) results.push(result);
    }
  };
  await Promise.all(Array.from({ length: Math.min(Math.max(1, config.designConcurrency ?? 2), 3) }, () => worker()));

  let text = input.text;
  results.forEach(({ page, text: repaired }) => {
    const pageMarker = new RegExp(`!\\[[^\\]]*\\]\\([^)]*page=${page}[^)]*\\)`, 'g');
    if (pageMarker.test(text)) {
      text = text.replace(pageMarker, `页码：${page}\n${repaired}`);
    } else {
      text += `\n\n页码：${page}\n${repaired}`;
    }
  });
  return { text, errors, repairedPages, failedPages };
}

export async function parseTextbookWithAgent(input: {
  courseTitle: string;
  subject: string;
  grade: string;
  blocks: LearningSourceBlock[];
  config: AgentModelConfig;
  callImpl?: AgentCall;
  imagesByBlock?: Map<string, Array<{ kind: 'image' | 'video'; data_url: string }>>;
  generateLessonDesigns?: boolean;
  onProgress?: (stage: string, current: number, total: number) => void;
  onAttempt?: (attempt: number, error?: string) => void;
}): Promise<AgentParseResult> {
  const modelCalls: AgentCallStat[] = [];
  const config: AgentModelConfig = {
    ...input.config,
    onCall: (stat) => {
      modelCalls.push(stat);
      input.config.onCall?.(stat);
    },
  };
  const outline = await runAgentOutline({
    courseTitle: input.courseTitle,
    subject: input.subject,
    grade: input.grade,
    blocks: input.blocks,
    config,
    callImpl: input.callImpl,
    onProgress: input.onProgress,
    onAttempt: input.onAttempt,
  });
  const sections = await runAgentSections({
    courseTitle: input.courseTitle,
    chapters: outline.chapters,
    allBlocks: input.blocks,
    config,
    callImpl: input.callImpl,
    imagesByBlock: input.imagesByBlock,
    onProgress: input.onProgress,
    onAttempt: input.onAttempt,
  });
  const graph = validateAgentGraph(sections.chapters);
  const validationErrors = [...outline.errors, ...sections.errors, ...graph.errors];
  graph.duplicates.forEach((title) => validationErrors.push(`重复知识点：${title}`));
  graph.cycles.forEach((cycle) => validationErrors.push(`前置知识点存在循环：${cycle}`));
  let lessonDesigns: LessonDesignStats | undefined;
  if (input.generateLessonDesigns) {
    const designed = await runAgentLessonDesigns({
      courseTitle: input.courseTitle,
      subject: input.subject,
      grade: input.grade,
      chapters: sections.chapters,
      allBlocks: input.blocks,
      config,
      callImpl: input.callImpl,
      imagesByBlock: input.imagesByBlock,
      onProgress: input.onProgress,
      onAttempt: input.onAttempt,
    });
    lessonDesigns = designed.stats;
    validationErrors.push(...designed.errors);
  }
  return {
    course: outline.course,
    chapters: sections.chapters,
    validation_errors: validationErrors,
    batch_count: outline.batchCount + sections.batchCount,
    lesson_designs: lessonDesigns,
    coverage: sections.coverage,
    model_calls: modelCalls,
  };
}

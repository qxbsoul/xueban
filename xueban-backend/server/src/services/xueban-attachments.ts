import { PYTHON_BACKEND_URL } from '../python-backend';
import { XuebanChatError } from './xueban-chat';

export interface XuebanChatAttachment {
  kind: 'image' | 'video' | 'text';
  name: string;
  mime: string;
  size: number;
  data_url?: string;
  text?: string;
}

interface UploadLike { originalname: string; mimetype: string; size: number; buffer: Buffer }

const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const VIDEO_MIMES = new Set(['video/mp4', 'video/webm']);
const TEXT_EXTENSIONS = new Set(['txt', 'md', 'markdown', 'log', 'csv', 'json', 'jsonl', 'html', 'htm']);
const DOCUMENT_EXTENSIONS = new Set(['pdf', 'docx', 'pptx', 'xlsx', 'xlsm']);
const MAX_FILES = 4;
const MAX_TOTAL_BYTES = 25 * 1024 * 1024;
const PARSE_TIMEOUT_MS = 240_000;

function extension(name: string): string {
  return name.toLowerCase().split('.').pop() || '';
}

export function normalizeUploadFilename(name: string): string {
  const cleaned = String(name || '').replace(/[\u0000-\u001f]/g, '').trim();
  if (!cleaned) return '未命名文件';
  try {
    const decoded = Buffer.from(cleaned, 'latin1').toString('utf8');
    if (decoded && decoded !== cleaned && !decoded.includes('\ufffd')) return decoded;
  } catch { /* keep cleaned */ }
  return cleaned;
}

function safeName(name: string): string {
  return name.replace(/[\r\n\0]/g, '').slice(0, 180) || '未命名文件';
}

function checkLimits(files: UploadLike[]): void {
  if (files.length > MAX_FILES) throw new XuebanChatError(413, 'TOO_MANY_FILES', '每条消息最多上传 4 个文件');
  if (files.reduce((sum, file) => sum + file.size, 0) > MAX_TOTAL_BYTES) {
    throw new XuebanChatError(413, 'FILES_TOO_LARGE', '单条消息附件总大小不能超过 25 MB');
  }
}

function isDocument(file: UploadLike): boolean {
  return DOCUMENT_EXTENSIONS.has(extension(file.originalname));
}

function toDirectAttachment(file: UploadLike): XuebanChatAttachment {
  const ext = extension(file.originalname);
  const name = safeName(file.originalname);
  if (IMAGE_MIMES.has(file.mimetype) && ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(ext)) {
    if (file.size > 10 * 1024 * 1024) throw new XuebanChatError(413, 'IMAGE_TOO_LARGE', `${name} 超过图片 10 MB 限制`);
    return { kind: 'image', name, mime: file.mimetype, size: file.size, data_url: `data:${file.mimetype};base64,${file.buffer.toString('base64')}` };
  }
  if (VIDEO_MIMES.has(file.mimetype) && ['mp4', 'webm'].includes(ext)) {
    if (file.size > 25 * 1024 * 1024) throw new XuebanChatError(413, 'VIDEO_TOO_LARGE', `${name} 超过视频 25 MB 限制`);
    return { kind: 'video', name, mime: file.mimetype, size: file.size, data_url: `data:${file.mimetype};base64,${file.buffer.toString('base64')}` };
  }
  if ((file.mimetype === 'text/plain' || file.mimetype === 'application/octet-stream' || file.mimetype === 'text/markdown' || file.mimetype === 'text/csv' || file.mimetype === 'application/json' || file.mimetype === 'text/html') && TEXT_EXTENSIONS.has(ext)) {
    if (file.size > 2 * 1024 * 1024) throw new XuebanChatError(413, 'TEXT_TOO_LARGE', `${name} 超过文本附件 2 MB 限制`);
    try {
      const text = new TextDecoder('utf-8', { fatal: true }).decode(file.buffer);
      return { kind: 'text', name, mime: 'text/plain', size: file.size, text };
    } catch {
      throw new XuebanChatError(422, 'TEXT_ENCODING_INVALID', `${name} 不是有效的 UTF-8 文本文件`);
    }
  }
  throw new XuebanChatError(
    415,
    'UNSUPPORTED_FILE_TYPE',
    `${name} 格式不支持，仅支持图片、MP4/WebM、文本（TXT/MD/CSV/JSON/HTML）以及 PDF/Word/PPT/Excel`,
  );
}

/** 同步校验：只处理图片 / 视频 / 文本，文档由 buildChatAttachments 解析。 */
export function validateChatAttachments(files: UploadLike[]): XuebanChatAttachment[] {
  checkLimits(files);
  return files.map(toDirectAttachment);
}

const PARSE_BASE_URLS = [...new Set([
  PYTHON_BACKEND_URL,
  'http://127.0.0.1:8010',
  'http://127.0.0.1:8000',
])];

async function requestDocumentParse(
  file: UploadLike,
  maxChars: number,
  includeImages: boolean,
  timeoutMs = PARSE_TIMEOUT_MS,
): Promise<{ text: string; images: LearningDocumentImage[]; audit?: LearningDocumentAudit }> {
  const form = new FormData();
  form.append('upload', new Blob([new Uint8Array(file.buffer)], { type: file.mimetype || 'application/octet-stream' }), file.originalname);
  form.append('max_chars', String(Math.max(1_000, Math.min(maxChars, 500_000))));
  if (includeImages) {
    form.append('include_images', 'true');
    // 学习中心把原图直接交给 DeepSeek 原生视觉；跳过 Python OCR，避免复杂 PDF 超时。
    form.append('describe_images', 'false');
  }
  let lastError: unknown;
  let timedOut = false;
  const parseStarted = Date.now();
  for (const base of PARSE_BASE_URLS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${base}/api/memory-bank/parse`, {
        method: 'POST', body: form, signal: controller.signal,
      });
      const raw = await response.text();
      // 该端口不是文档解析服务时继续尝试其他已知端口。
      if (response.status === 404) {
        lastError = new Error(`parse endpoint not found at ${base}`);
        continue;
      }
      if (!response.ok) {
        let message = raw;
        try {
          const body = JSON.parse(raw) as { detail?: { error?: { message?: string }; message?: string }; error?: { message?: string } };
          message = body?.detail?.error?.message || body?.detail?.message || body?.error?.message || raw;
        } catch { /* keep raw */ }
        throw new XuebanChatError(422, 'DOCUMENT_PARSE_FAILED', `${safeName(file.originalname)} 解析失败：${String(message).slice(0, 200)}`);
      }
      const body = JSON.parse(raw) as { data?: { text?: string; images?: LearningDocumentImage[]; audit?: LearningDocumentAudit } };
      const text = body?.data?.text ?? '';
      if (!text.trim()) throw new XuebanChatError(422, 'DOCUMENT_EMPTY', `${safeName(file.originalname)} 里没有可读取的文字内容`);
      return {
        text,
        images: includeImages && Array.isArray(body?.data?.images) ? body.data.images : [],
        audit: body?.data?.audit,
      };
    } catch (error) {
      if (error instanceof XuebanChatError) throw error;
      lastError = error;
      if ((error as { name?: string })?.name === 'AbortError') {
        timedOut = true;
        break;
      }
    } finally {
      clearTimeout(timer);
    }
  }
  if (timedOut) {
    throw new XuebanChatError(
      504, 'DOCUMENT_PARSE_TIMEOUT',
      `文档解析超时：文件 ${safeName(file.originalname)}，${Math.max(1, Math.round(file.size / 1024 / 1024))}MB，${file.mimetype || '未知类型'}，等待 ${Math.round((Date.now() - parseStarted) / 1000)} 秒。`, 
    );
  }
  throw new XuebanChatError(
    503, 'DOCUMENT_PARSE_UNAVAILABLE',
    `文档解析服务暂时不可用，请确认 Python 服务已启动后重试${lastError instanceof Error ? `（${lastError.message.slice(0, 100)}）` : ''}`,
  );
}

/** 调 Python 知识库解析器，把文档转成文本（不落库）。 */
export async function parseDocument(file: UploadLike, maxChars = 24_000, timeoutMs = PARSE_TIMEOUT_MS): Promise<string> {
  const result = await requestDocumentParse(file, maxChars, false, timeoutMs);
  return result.text;
}

async function requestDocumentPageRange(
  file: UploadLike,
  pageStart: number,
  pageEnd: number,
  timeoutMs: number,
): Promise<{ text: string; totalPages: number; pageStart: number; pageEnd: number; pages: Array<{ page: number; chars: number; ok: boolean }>; page_texts?: Array<{ page: number; text: string }> }> {
  const form = new FormData();
  form.append('upload', new Blob([new Uint8Array(file.buffer)], { type: file.mimetype || 'application/octet-stream' }), file.originalname);
  form.append('page_start', String(pageStart));
  form.append('page_end', String(pageEnd));
  form.append('max_chars', '500000');
  let lastError: unknown;
  for (const base of PARSE_BASE_URLS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${base}/api/memory-bank/parse-pages`, { method: 'POST', body: form, signal: controller.signal });
      const raw = await response.text();
      if (response.status === 404) { lastError = new Error(`parse-pages not found at ${base}`); continue; }
      if (!response.ok) throw new Error(raw.slice(0, 200));
      const body = JSON.parse(raw) as { data?: { text?: string; total_pages?: number; page_start?: number; page_end?: number; pages?: Array<{ page: number; chars: number; ok: boolean }> } };
      const data = body?.data;
      if (!data) throw new Error('empty page parse response');
      return {
        text: data.text || '',
        totalPages: data.total_pages || pageEnd,
        pageStart: data.page_start || pageStart,
        pageEnd: data.page_end || pageEnd,
        pages: Array.isArray(data.pages) ? data.pages : [],
        page_texts: Array.isArray((data as { page_texts?: Array<{ page: number; text: string }> }).page_texts) ? (data as { page_texts?: Array<{ page: number; text: string }> }).page_texts! : [],
      };
    } catch (error) {
      lastError = error;
      if ((error as { name?: string })?.name === 'AbortError') break;
    } finally {
      clearTimeout(timer);
    }
  }
  throw new XuebanChatError(504, 'DOCUMENT_PAGE_TIMEOUT', `课纲页段 ${pageStart}-${pageEnd} 解析失败${lastError instanceof Error ? `：${lastError.message.slice(0, 120)}` : ''}`);
}

async function requestDocumentOcrRange(
  file: UploadLike,
  pageStart: number,
  pageEnd: number,
  timeoutMs = 900_000,
): Promise<{ text: string; totalPages: number; pageTexts: Array<{ page: number; text: string; chars: number; ocr: boolean; ok: boolean }>; errors: Array<{ page: number; error: string }> }> {
  const form = new FormData();
  form.append('upload', new Blob([new Uint8Array(file.buffer)], { type: file.mimetype || 'application/octet-stream' }), file.originalname);
  form.append('page_start', String(pageStart));
  form.append('page_end', String(pageEnd));
  let lastError: unknown;
  for (const base of PARSE_BASE_URLS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${base}/api/memory-bank/parse-pages-ocr`, { method: 'POST', body: form, signal: controller.signal });
      const raw = await response.text();
      if (response.status === 404) { lastError = new Error('OCR endpoint not found'); continue; }
      if (!response.ok) throw new Error(raw.slice(0, 300));
      const body = JSON.parse(raw) as { data?: { text?: string; total_pages?: number; page_texts?: Array<{ page: number; text: string; chars: number; ocr: boolean; ok: boolean }>; errors?: Array<{ page: number; error: string }> } };
      const data = body?.data;
      if (!data) throw new Error('empty OCR response');
      return {
        text: data.text || '', totalPages: data.total_pages || pageEnd,
        pageTexts: Array.isArray(data.page_texts) ? data.page_texts : [],
        errors: Array.isArray(data.errors) ? data.errors : [],
      };
    } catch (error) {
      lastError = error;
      if ((error as { name?: string })?.name === 'AbortError') break;
    } finally {
      clearTimeout(timer);
    }
  }
  throw new XuebanChatError(504, 'OCR_TIMEOUT', `课纲 OCR 失败：${lastError instanceof Error ? lastError.message.slice(0, 160) : '未知错误'}`);
}

async function requestPdfInfo(file: UploadLike, timeoutMs = 30_000): Promise<number | null> {
  const form = new FormData();
  form.append('upload', new Blob([new Uint8Array(file.buffer)], { type: file.mimetype || 'application/octet-stream' }), file.originalname);
  for (const base of PARSE_BASE_URLS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${base}/api/memory-bank/pdf-info`, { method: 'POST', body: form, signal: controller.signal });
      if (!response.ok) continue;
      const body = await response.json() as { data?: { pages?: number } };
      if (body?.data?.pages) return body.data.pages;
    } catch {
      // 尝试下一个地址；连接失败不重复提交耗时解析。
    } finally {
      clearTimeout(timer);
    }
  }
  return null;
}

/** 课纲优先轻量文本；超时后按页段降级，并记录缺失页。 */
export async function parseCurriculumDocument(file: UploadLike, maxChars = 500_000): Promise<{
  text: string;
  audit?: LearningDocumentAudit;
  partial: boolean;
  failedPages: number[];
  scannedPages: number[];
  totalPages: number | null;
  pageRanges: Array<{ start: number; end: number; ok: boolean; chars: number }>;
  pages?: Array<{ page: number; text: string }>;
}> {
  try {
    const text = await parseDocument(file, maxChars, 90_000);
    return { text, partial: false, failedPages: [], scannedPages: [], totalPages: null, pageRanges: [{ start: 1, end: 0, ok: true, chars: text.length }] };
  } catch (error) {
    if (!(error instanceof XuebanChatError) || error.code !== 'DOCUMENT_PARSE_TIMEOUT') throw error;
  }
  const totalPages = await requestPdfInfo(file);
  if (!totalPages) {
    throw new XuebanChatError(504, 'DOCUMENT_PARSE_TIMEOUT', '课纲全文文本提取超时，且无法获取 PDF 页数；未继续生成。');
  }
  // 只上传一次 PDF、只在 Python 进程内打开一次。不要每 20 页重新上传整份文件。
  try {
    const result = await requestDocumentPageRange(file, 1, totalPages, 120_000);
    const pages = result.pages || [];
    const scannedPages = pages.filter((page) => !page.ok).map((page) => page.page);
    const text = result.text || '';
    if (!text.trim()) {
      const ocr = await requestDocumentOcrRange(file, 1, totalPages);
      if (!ocr.text.trim()) {
        throw new XuebanChatError(
          504, 'DOCUMENT_SCANNED',
          `课纲 PDF 共 ${totalPages} 页，文本层和 OCR 均未提取到内容；失败页 ${ocr.errors.length} 页。`,
        );
      }
      return {
        text: ocr.text,
        audit: { format: 'pdf', pages: totalPages },
        partial: ocr.pageTexts.some((page) => !page.ok),
        failedPages: ocr.pageTexts.filter((page) => !page.ok).map((page) => page.page),
        scannedPages,
        totalPages,
        pageRanges: [{ start: 1, end: totalPages, ok: true, chars: ocr.text.length }],
        pages: ocr.pageTexts.map((page) => ({ page: page.page, text: page.text })),
      };
    }
    return {
      text,
      audit: {
        format: 'pdf',
        pages: totalPages,
        readability: {
          level: scannedPages.length ? 'partial' : 'complete', pages: totalPages,
          text_pages: totalPages - scannedPages.length, minor_issue_pages: 0, fallback_pages: 0,
          garbled_pages: 0, empty_pages: scannedPages.length, unreadable_pages: scannedPages.length,
          unreliable_pages: 0, skipped_visual_pages: 0, images_exported: 0, images_omitted: 0,
        },
        page_records: [],
      },
      partial: scannedPages.length > 0,
      failedPages: scannedPages,
      scannedPages,
      totalPages,
      pageRanges: [{ start: 1, end: totalPages, ok: true, chars: text.length }],
      pages: result.page_texts,
    };
  } catch (error) {
    if (error instanceof XuebanChatError) throw error;
    throw new XuebanChatError(504, 'DOCUMENT_PAGE_TIMEOUT', `课纲 PDF 共 ${totalPages} 页，整份页段文本提取失败：${error instanceof Error ? error.message.slice(0, 160) : '未知错误'}`);
  }
}

export interface LearningDocumentImage {
  image_id?: string;
  mime: string;
  data_url: string;
  page?: number | null;
  page_status?: string;
  is_fallback?: boolean;
  context?: string;
}

export interface LearningReadabilityAudit {
  level: 'complete' | 'partial' | 'needs_review';
  pages: number;
  text_pages: number;
  minor_issue_pages: number;
  fallback_pages: number;
  garbled_pages: number;
  empty_pages: number;
  unreadable_pages: number;
  unreliable_pages: number;
  skipped_visual_pages: number;
  images_exported: number;
  images_omitted: number;
  images_oversize?: number;
}

export interface LearningDocumentAudit {
  format?: string;
  pages?: number;
  readability?: LearningReadabilityAudit;
  page_records?: Array<{ page: number; status: string; reason: string; raw_text?: string; extraction_method?: string; metrics?: Record<string, number>; font_repair?: { count: number; glyphs: Record<string, string> } }>;
  garbled_pages?: number[];
  empty_pages?: number[];
  fallback_pages?: number[];
  unreadable_pages?: number[];
  skipped_visual_pages?: number[];
  images_omitted?: Array<{ page: number | null; kind: string; image_id?: string }>;
  vision_repair_errors?: string[];
}

/** 学习中心：解析文档文本，同时取回文档内图片供原生视觉模型使用。 */
export async function parseDocumentWithImages(
  file: UploadLike,
  maxChars = 500_000,
): Promise<{ text: string; images: LearningDocumentImage[]; audit?: LearningDocumentAudit }> {
  return requestDocumentParse(file, maxChars, true);
}

/**
 * 聊天附件统一入口：图片/视频原样上送；文本直接读；PDF/Word/PPT/Excel
 * 先用知识库解析器转成文本再交给模型。
 */
export async function buildChatAttachments(
  files: UploadLike[],
  parse: (file: UploadLike) => Promise<string> = parseDocument,
): Promise<XuebanChatAttachment[]> {
  checkLimits(files);
  const result: XuebanChatAttachment[] = [];
  for (const file of files) {
    if (!isDocument(file)) {
      result.push(toDirectAttachment(file));
      continue;
    }
    const text = await parse(file);
    result.push({ kind: 'text', name: safeName(file.originalname), mime: 'text/plain', size: Buffer.byteLength(text), text });
  }
  return result;
}

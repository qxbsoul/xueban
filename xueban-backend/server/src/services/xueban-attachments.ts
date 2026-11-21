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
): Promise<{ text: string; images: LearningDocumentImage[] }> {
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
  for (const base of PARSE_BASE_URLS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PARSE_TIMEOUT_MS);
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
      const body = JSON.parse(raw) as { data?: { text?: string; images?: LearningDocumentImage[] } };
      const text = body?.data?.text ?? '';
      if (!text.trim()) throw new XuebanChatError(422, 'DOCUMENT_EMPTY', `${safeName(file.originalname)} 里没有可读取的文字内容`);
      return {
        text,
        images: includeImages && Array.isArray(body?.data?.images) ? body.data.images.slice(0, 12) : [],
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
      '文档解析超时。文件可能过大、页数过多或包含复杂扫描页，请拆分后再上传。',
    );
  }
  throw new XuebanChatError(
    503, 'DOCUMENT_PARSE_UNAVAILABLE',
    `文档解析服务暂时不可用，请确认 Python 服务已启动后重试${lastError instanceof Error ? `（${lastError.message.slice(0, 100)}）` : ''}`,
  );
}

/** 调 Python 知识库解析器，把文档转成文本（不落库）。 */
export async function parseDocument(file: UploadLike, maxChars = 24_000): Promise<string> {
  const result = await requestDocumentParse(file, maxChars, false);
  return result.text;
}

export interface LearningDocumentImage {
  image_id?: string;
  mime: string;
  data_url: string;
  page?: number | null;
  context?: string;
}

/** 学习中心：解析文档文本，同时取回文档内图片供原生视觉模型使用。 */
export async function parseDocumentWithImages(
  file: UploadLike,
  maxChars = 500_000,
): Promise<{ text: string; images: LearningDocumentImage[] }> {
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

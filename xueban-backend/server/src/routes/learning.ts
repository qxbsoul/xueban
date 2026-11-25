/**
 * 学习中心路由（2026-11-15，框架阶段）。
 *
 * 账号隔离：owner_id 只来自 Bearer token，请求体不接收 owner_id。
 * 解析/生成能力后续替换 services/learning.ts 内部实现，HTTP 契约保持不变。
 */
import { createHash } from 'node:crypto';
import { Router, type Request, type Response } from 'express';
import multer from 'multer';
import { requireAccount } from '../lib/memory-account';
import { PYTHON_BACKEND_URL } from '../python-backend';
import { buildCurriculumIndex, buildCurriculumProfile, curriculumContext } from '../services/curriculum-index';
import { normalizeUploadFilename, parseDocumentWithImages, parseCurriculumDocument, type LearningDocumentAudit, type LearningDocumentImage } from '../services/xueban-attachments';
import { modelConfig } from './xueban-chat';
import { runCourseGeneration } from '../services/learning';
import {
  COURSE_SOURCE_TYPES,
  createCourse,
  createGenerationJob,
  updateCourseStatus,
  updateJob,
  createMaterial,
  createUnit,
  createLesson,
  createAttempt,
  deleteCourse,
  getCourse,
  getCourseDetail,
  getMaterialById,
  updateMaterialParsed,
  getExercise,
  getLatestJob,
  appendGenerationEvent,
  getLatestDiagnosis,
  listGenerationEvents,
  getMaterialSource,
  getLesson,
  isLessonCompleted,
  listAttempts,
  listCourses,
  listSharedCourses,
  shareCourse,
  canAccessSharedCourse,
  courseOwner,
  listExercises,
  listKnowledgePoints,
  listMaterials,
  listSourceBlocks,
  listUnits,
  setLessonProgress,
  type CourseSourceType,
  type ExercisePhase,
} from '../storage/learning';

const router = Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 50 * 1024 * 1024, files: 20 } });

router.use(async (req: Request, res: Response, next) => {
  try {
    res.locals.ownerId = await requireAccount(req);
    next();
  } catch {
    res.status(401).json({ error: { code: 'UNAUTHORIZED', message: '请先登录后再访问学习中心' } });
  }
});

function owner(res: Response): string {
  return String(res.locals.ownerId ?? '');
}

function fail(res: Response, status: number, code: string, message: string): void {
  res.status(status).json({ error: { code, message } });
}

const STALE_GENERATION_MS = 30 * 60 * 1000;

function latestJobOrMarkStalled(account: string, courseId: string) {
  const job = getLatestJob(account, courseId);
  if (job && job.status === 'running' && Date.now() - job.updated_at > STALE_GENERATION_MS) {
    const message = '生成任务超过 5 分钟没有进度更新，已标记中断；可以重新生成。';
    appendGenerationEvent(account, courseId, job.id, 'stalled', message);
    updateJob(account, job.id, { status: 'failed', stage: 'failed', error: message });
    updateCourseStatus(account, courseId, { status: 'failed', stage: 'failed', progress: 0, error: message });
    return getLatestJob(account, courseId);
  }
  return job;
}

const TEXT_EXTENSIONS = ['.txt', '.md', '.markdown', '.json', '.csv'];
const DOCUMENT_EXTENSIONS = ['.pdf', '.docx', '.pptx', '.xlsx', '.xlsm'];

function isTextFile(file: Express.Multer.File): boolean {
  const name = (file.originalname || '').toLowerCase();
  const mime = (file.mimetype || '').toLowerCase();
  return mime.startsWith('text/')
    || mime === 'application/json'
    || mime === 'application/csv'
    || TEXT_EXTENSIONS.some((extension) => name.endsWith(extension));
}

function isDocumentFile(file: Express.Multer.File): boolean {
  const name = (file.originalname || '').toLowerCase();
  return DOCUMENT_EXTENSIONS.some((extension) => name.endsWith(extension));
}

/** 复用课程资源库 / 聊天附件已有的 Python 文档解析能力（PDF、DOCX、PPTX、XLSX）。 */
const BOILERPLATE_RE = /(仅供个人|未经授权|不得另做他用|版权所有|翻印|侵权|内部资料)/;
function cleanCourseText(value: string): string {
  return value
    .split(/\r?\n/)
    .filter((line) => !BOILERPLATE_RE.test(line))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

async function extractDocumentText(file: Express.Multer.File): Promise<{ text: string; format: string; images: LearningDocumentImage[]; audit?: LearningDocumentAudit }> {
  if (isTextFile(file)) {
    const text = file.buffer.toString('utf8').replace(/\u0000/g, '').trim();
    if (!text) throw Object.assign(new Error('上传文件没有可读取的文本内容。'), { status: 422 });
    return { text: cleanCourseText(text).slice(0, 500_000), format: 'text', images: [] };
  }
  if (!isDocumentFile(file)) {
    throw Object.assign(new Error('这个文件格式暂时无法读取，请上传 PDF、Word、PPT、Excel 或文本文件。'), { status: 422 });
  }
  const parsed = await parseDocumentWithImages(file as never, 500_000);
  if (!parsed.text.trim()) throw Object.assign(new Error('文件里没有可读取的文字内容。'), { status: 422 });
  const format = (file.originalname || '').toLowerCase().split('.').pop() || 'document';
  return { text: cleanCourseText(parsed.text).slice(0, 500_000), format, images: parsed.images, audit: parsed.audit };
}

function scheduleGeneration(ownerId: string, courseId: string, source: { source_type: CourseSourceType; source_name?: string; material_id?: string; text: string; images?: LearningDocumentImage[]; audit?: LearningDocumentAudit; curriculum_text?: string; materials?: Array<{ id: string; filename: string; material_role: 'curriculum_standard' | 'textbook' }> }, config: ReturnType<typeof modelConfig>): void {
  setImmediate(() => {
    void runCourseGeneration(ownerId, courseId, source, { config }).catch((error) => {
      console.error('[learning] generation failed', error);
    });
  });
}

function agentModelConfig(req: Request, res: Response) {
  try {
    const config = modelConfig(req);
    const reasoningMode = process.env.XUEBAN_LEARNING_REASONING_MODE;
    return {
      ...config,
      reasoningMode: reasoningMode === 'auto' || reasoningMode === 'always' ? reasoningMode : 'off',
      // 只有部署方明确声明模型支持 thinking 时才允许开启；默认快速模式。
      supportsThinking: process.env.XUEBAN_LEARNING_SUPPORTS_THINKING === '1',
      sectionConcurrency: Math.max(1, Math.min(4, Number(process.env.XUEBAN_LEARNING_SECTION_CONCURRENCY || 3))),
      designConcurrency: Math.max(1, Math.min(4, Number(process.env.XUEBAN_LEARNING_DESIGN_CONCURRENCY || 2))),
    } as ReturnType<typeof modelConfig>;
  } catch (error) {
    fail(res, 401, 'NO_MODEL', error instanceof Error ? error.message : '请先在设置中选择并配置模型');
    return null;
  }
}

async function runJointPreparation(input: {
  ownerId: string;
  courseId: string;
  jobId: string;
  materials: Array<{ id: string; filename: string; role: 'curriculum_standard' | 'textbook' }>;
  config: ReturnType<typeof modelConfig>;
}): Promise<void> {
  const { ownerId, courseId, jobId, materials, config } = input;
  const log = (kind: string, message: string, payload?: unknown) => appendGenerationEvent(ownerId, courseId, jobId, kind, message, payload);
  updateJob(ownerId, jobId, { status: 'running', stage: 'parsing' });
  updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'parsing', progress: 3, error: '' });
  const warnings: string[] = [];
  const curriculumParts: string[] = [];
  const curriculumPages: Array<{ page: number; text: string }> = [];
  let curriculumMaterialId = '';
  let cachedCurriculumIndex: ReturnType<typeof buildCurriculumIndex> | undefined;
  let cachedCurriculumProfile: ReturnType<typeof buildCurriculumProfile> | undefined;
  const textbookParts: Array<{ id: string; filename: string; text: string; images: LearningDocumentImage[]; audit?: LearningDocumentAudit }> = [];
  const total = materials.length;
  for (let index = 0; index < materials.length; index += 1) {
    const material = materials[index];
    const record = getMaterialById(ownerId, courseId, material.id);
    if (!record?.original_blob) {
      warnings.push(`${material.filename}：原始文件缺失`);
      continue;
    }
    const progress = 4 + Math.round((index / Math.max(total, 1)) * 18);
    updateJob(ownerId, jobId, { status: 'running', stage: `parse ${index + 1}/${total}` });
    updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'parsing', progress });
    log('material_parse', `正在解析 ${material.filename}`, { material_id: material.id, role: material.role, index: index + 1, total });
    try {
      const uploadFile = {
        originalname: material.filename,
        mimetype: record.mime || 'application/octet-stream',
        size: record.size || record.original_blob.length,
        buffer: record.original_blob,
      };
      const parsed = material.role === 'curriculum_standard'
        ? await parseCurriculumDocument(uploadFile)
        : await parseDocumentWithImages(uploadFile);
      const text = cleanCourseText(parsed.text);
      if (!text.trim()) throw new Error('文件没有可读取的文本内容');
      if (material.role === 'curriculum_standard') {
        const curriculum = parsed as Awaited<ReturnType<typeof parseCurriculumDocument>>;
        if (curriculum.partial) {
          warnings.push(`${material.filename}：课纲仅部分页段成功，失败页 ${curriculum.failedPages.join('、') || '未知'}`);
          log('material_partial', `${material.filename} 课纲部分解析，失败页 ${curriculum.failedPages.join('、') || '未知'}`, {
            material_id: material.id, failed_pages: curriculum.failedPages, page_ranges: curriculum.pageRanges,
          });
        }
        log('material_pages', `${material.filename} 课纲页段完成`, {
          material_id: material.id, total_pages: curriculum.totalPages, failed_pages: curriculum.failedPages, page_ranges: curriculum.pageRanges,
        });
      }
      updateMaterialParsed({
        owner_id: ownerId, course_id: courseId, material_id: material.id,
        text_content: text, extraction_status: 'indexed', format: record.format || 'text',
        page_count: null, parse_audit_json: JSON.stringify(parsed.audit || {}),
      });
      if (material.role === 'curriculum_standard') {
        curriculumMaterialId = material.id;
        try {
          const auditCache = JSON.parse(record.parse_audit_json || '{}');
          if (auditCache.curriculum_index) cachedCurriculumIndex = auditCache.curriculum_index;
          if (auditCache.curriculum_profile) cachedCurriculumProfile = auditCache.curriculum_profile;
        } catch { /* ignore invalid cache */ }
        curriculumParts.push(`\n\n===== 课程标准：${material.filename}（material_id=${material.id}）=====\n${text}`);
        if (parsed && 'pages' in parsed && Array.isArray(parsed.pages) && parsed.pages.length) {
          curriculumPages.push(...parsed.pages);
        } else {
          curriculumPages.push({ page: 1, text });
        }
      } else {
        textbookParts.push({ id: material.id, filename: material.filename, text, images: 'images' in parsed ? parsed.images : [], audit: parsed.audit });
      }
      log('material_parsed', `${material.filename} 解析完成`, { material_id: material.id, role: material.role, chars: text.length });
    } catch (error) {
      const message = error instanceof Error ? error.message : '解析失败';
      warnings.push(`${material.filename}：${message}`);
      updateMaterialParsed({
        owner_id: ownerId, course_id: courseId, material_id: material.id,
        text_content: '', extraction_status: 'failed', format: record.format || 'text',
        page_count: record.page_count ?? null, parse_audit_json: JSON.stringify({ error: message }),
      });
      log('material_failed', `${material.filename} 解析失败：${message}`, { material_id: material.id, role: material.role });
    }
  }
  const curriculumIndex = cachedCurriculumIndex || buildCurriculumIndex(
    `curriculum:${courseId}:${curriculumPages.length}:${curriculumPages.reduce((sum, page) => sum + page.text.length, 0)}`,
    curriculumPages,
  );
  const curriculumProfile = cachedCurriculumProfile || buildCurriculumProfile(curriculumIndex);
  if (curriculumMaterialId && !cachedCurriculumIndex) {
    const materialRecord = getMaterialById(ownerId, courseId, curriculumMaterialId);
    if (materialRecord) {
      updateMaterialParsed({
        owner_id: ownerId, course_id: courseId, material_id: curriculumMaterialId,
        text_content: materialRecord.text_content, extraction_status: materialRecord.extraction_status,
        format: materialRecord.format || 'text', page_count: materialRecord.page_count,
        parse_audit_json: JSON.stringify({ ...(JSON.parse(materialRecord.parse_audit_json || '{}') || {}), curriculum_index: curriculumIndex, curriculum_profile: curriculumProfile }),
      });
    }
  }
  const curriculumOverview = curriculumContext(curriculumIndex, '本课程教材', 2000);

  if (!textbookParts.length) {
    const message = `教材全部解析失败：${warnings.join('；') || '没有可用教材'}`;
    updateJob(ownerId, jobId, { status: 'failed', stage: 'failed', error: message });
    updateCourseStatus(ownerId, courseId, { status: 'failed', stage: 'failed', progress: 0, error: message });
    return;
  }
  if (!curriculumParts.length) {
    const message = `课程标准全部解析失败：${warnings.join('；') || '没有有效课纲'}`;
    updateJob(ownerId, jobId, { status: 'failed', stage: 'failed', error: message });
    updateCourseStatus(ownerId, courseId, { status: 'failed', stage: 'failed', progress: 0, error: message });
    return;
  }
  updateJob(ownerId, jobId, { status: 'running', stage: 'generating' });
  updateCourseStatus(ownerId, courseId, { status: 'generating', stage: 'generating', progress: 24, error: '' });
  log('materials_ready', `材料解析完成：课纲 ${curriculumParts.length} 份，教材 ${textbookParts.length} 份`, { warnings });
  try {
    await runCourseGeneration(ownerId, courseId, {
      source_type: 'comprehensive',
      source_name: textbookParts.map((item) => item.filename).join('、'),
      text: '',
      materials: textbookParts.map((item) => ({ ...item, material_role: 'textbook' as const })),
      curriculum_text: curriculumOverview,
      curriculum_index: curriculumIndex,
      curriculum_profile: curriculumProfile,
    }, { config });
  } catch (error) {
    const message = error instanceof Error ? error.message : '综合生成失败';
    updateJob(ownerId, jobId, { status: 'failed', stage: 'failed', error: message });
    updateCourseStatus(ownerId, courseId, { status: 'failed', stage: 'failed', progress: 0, error: message });
    log('error', message);
  }
}

router.get('/courses', (_req: Request, res: Response) => {
  const account = owner(res);
  const own = listCourses(account);
  const shared = listSharedCourses(account);
  const ids = new Set(own.map((course) => course.id));
  res.json([...own, ...shared.filter((course) => !ids.has(course.id)).map((course) => ({ ...course, shared: true }))]);
});

router.post('/courses/joint', upload.fields([
  { name: 'curriculumFiles', maxCount: 10 },
  { name: 'textbookFiles', maxCount: 10 },
]), async (req: Request, res: Response) => {
  try {
    const account = owner(res);
    const title = String(req.body.title || '').trim().slice(0, 100);
    if (!title) return fail(res, 422, 'INVALID_BODY', '请填写课程名称');
    const files = (req.files || {}) as Record<string, Express.Multer.File[]>;
    const curriculumFiles = (files.curriculumFiles || []).map((file) => ({ ...file, originalname: normalizeUploadFilename(file.originalname) }));
    const textbookFiles = (files.textbookFiles || []).map((file) => ({ ...file, originalname: normalizeUploadFilename(file.originalname) }));
    if (!curriculumFiles.length) return fail(res, 422, 'MISSING_CURRICULUM', '请至少上传一份课程标准或教学大纲');
    if (!textbookFiles.length) return fail(res, 422, 'MISSING_TEXTBOOK', '请至少上传一份教材');
    const allFiles = [...curriculumFiles, ...textbookFiles];
    if (allFiles.length > 20) return fail(res, 422, 'TOO_MANY_FILES', '一次最多上传 20 份文件');
    if (allFiles.some((file) => !file.size)) return fail(res, 422, 'EMPTY_FILE', '不能上传空文件');
    const totalBytes = allFiles.reduce((sum, file) => sum + file.size, 0);
    if (totalBytes > 150 * 1024 * 1024) return fail(res, 413, 'PAYLOAD_TOO_LARGE', '本次上传总大小不能超过 150MB');
    const config = agentModelConfig(req, res);
    if (!config) return;
    const course = createCourse({
      owner_id: account,
      title,
      subject: String(req.body.subject || '').trim().slice(0, 20),
      grade: String(req.body.grade || '').trim().slice(0, 20),
      source_type: 'comprehensive',
      source_name: textbookFiles.map((file) => file.originalname).join('、'),
    });
    const materials: Array<{ id: string; filename: string; role: 'curriculum_standard' | 'textbook' }> = [];
    // 上传请求只持久化原始文件，不在这里解析 PDF。
    for (const file of curriculumFiles) {
      const material = createMaterial({
        owner_id: account, course_id: course.id, filename: file.originalname,
        mime: file.mimetype, size: file.size, text_content: '',
        original: file.buffer, format: 'pending', page_count: null,
        parse_audit_json: '{}', material_role: 'curriculum_standard',
      });
      materials.push({ id: material.id, filename: file.originalname, role: 'curriculum_standard' });
    }
    for (const file of textbookFiles) {
      const material = createMaterial({
        owner_id: account, course_id: course.id, filename: file.originalname,
        mime: file.mimetype, size: file.size, text_content: '',
        original: file.buffer, format: 'pending', page_count: null,
        parse_audit_json: '{}', material_role: 'textbook',
      });
      materials.push({ id: material.id, filename: file.originalname, role: 'textbook' });
    }
    const job = createGenerationJob(account, course.id);
    updateCourseStatus(account, course.id, { status: 'generating', stage: 'queued', progress: 1, error: '' });
    updateJob(account, job.id, { status: 'queued', stage: 'queued' });
    res.status(202).json({ course: getCourse(account, course.id), job, materials });
    setImmediate(() => {
      void runJointPreparation({ ownerId: account, courseId: course.id, jobId: job.id, materials, config }).catch((error) => {
        const message = error instanceof Error ? error.message : '综合后台任务失败';
        updateJob(account, job.id, { status: 'failed', stage: 'failed', error: message });
        updateCourseStatus(account, course.id, { status: 'failed', stage: 'failed', progress: 0, error: message });
      });
    });
  } catch (error) {
    const status = (error as { status?: number }).status || 500;
    const message = error instanceof Error ? error.message : '创建综合课程失败';
    fail(res, status, status === 422 ? 'INVALID_MATERIAL' : 'INTERNAL', message);
  }
});

router.post('/courses', upload.single('file'), async (req: Request, res: Response) => {
  try {
    const account = owner(res);
    const title = String(req.body.title || '').trim().slice(0, 100);
    const sourceType = String(req.body.source_type || 'textbook') as CourseSourceType;
    if (!title) return fail(res, 422, 'INVALID_BODY', '请填写课程/教材名称');
    if (!COURSE_SOURCE_TYPES.includes(sourceType)) return fail(res, 422, 'INVALID_BODY', '来源类型不合法');
    const config = agentModelConfig(req, res);
    if (!config) return;
    const file = req.file;
    if (file) file.originalname = normalizeUploadFilename(file.originalname);
    if (file && sourceType === 'description') {
      return fail(res, 422, 'INVALID_BODY', '自主描述不需要上传文件，写下学习目标即可');
    }
    let text = String(req.body.source_text || '').trim();
    let format = 'text';
    let images: LearningDocumentImage[] = [];
    let audit: LearningDocumentAudit | undefined;
    if (file) {
      const extracted = await extractDocumentText(file);
      text = extracted.text;
      format = extracted.format;
      images = extracted.images;
      audit = extracted.audit;
    }
    if (!text) return fail(res, 422, 'INVALID_BODY', '请上传一份材料，或粘贴目录 / 学习目标');
    if (sourceType === 'description' && text.length < 10) {
      return fail(res, 422, 'INVALID_BODY', '自主描述至少需要 10 个字，便于生成目录');
    }

    const course = createCourse({
      owner_id: account,
      title,
      subject: String(req.body.subject || '').trim().slice(0, 20),
      grade: String(req.body.grade || '').trim().slice(0, 20),
      source_type: sourceType,
      source_name: file?.originalname || String(req.body.source_name || '').trim().slice(0, 120) || title,
    });
    const material = createMaterial({
      owner_id: account,
      course_id: course.id,
      filename: file?.originalname || `${title}.txt`,
      mime: file?.mimetype || 'text/plain',
      size: file?.size || Buffer.byteLength(text, 'utf8'),
      extraction_status: 'indexed',
      text_content: text,
      original: file?.buffer,
      sha256: createHash('sha256').update(file?.buffer || text).digest('hex'),
      format,
      page_count: format === 'pdf' ? (text.match(/页码[：:]\s*\d+/g) || []).length : null,
    });
    const job = createGenerationJob(account, course.id);
    scheduleGeneration(account, course.id, {
      source_type: sourceType,
      source_name: file?.originalname || course.source_name,
      material_id: material.id,
      text,
      images,
      audit,
    }, config);
    res.status(201).json({ course: getCourse(account, course.id), job });
  } catch (error) {
    const status = (error as { status?: number }).status || 500;
    const message = error instanceof Error ? error.message : '创建课程失败';
    if (status >= 500) console.error('[learning] create course', error);
    fail(res, status, status === 422 ? 'INVALID_MATERIAL' : 'INTERNAL', message);
  }
});

router.post('/courses/:id/share', async (req: Request, res: Response) => {
  const account = owner(res);
  const authorization = String(req.headers.authorization || '');
  let bound: Array<{ user_id?: string }> = [];
  try {
    const response = await fetch(`${PYTHON_BACKEND_URL}/api/teacher/students`, { headers: { Authorization: authorization } });
    if (!response.ok) return fail(res, response.status === 403 ? 403 : 503, 'BINDING_UNAVAILABLE', '无法校验教师绑定的学生');
    const body = await response.json() as { items?: Array<{ user_id?: string }> };
    bound = body.items || [];
  } catch {
    return fail(res, 503, 'BINDING_UNAVAILABLE', '无法连接教师学生绑定服务');
  }
  const allowed = new Set(bound.map((item) => String(item.user_id || '')).filter(Boolean));
  const requested = Array.isArray(req.body?.student_owner_ids) ? req.body.student_owner_ids.map((id: unknown) => String(id)) : [];
  if (requested.some((id: string) => !allowed.has(id))) return fail(res, 403, 'NOT_BOUND', '只能共享给当前教师实际绑定的学生');
  const ids = requested.length ? requested : [...allowed];
  const shared = shareCourse(req.params.id, account, ids);
  if (!shared) return fail(res, 404, 'NOT_FOUND', '没有可共享的教材或当前账号不是教材所有者');
  res.json({ shared });
});





router.post('/courses/import-markdown', upload.single('file'), (req: Request, res: Response) => {
  const account = owner(res);
  const file = req.file;
  if (!file) return fail(res, 422, 'INVALID_MATERIAL', '请上传导出的学案 Markdown 文件');
  const text = file.buffer.toString('utf8').replace(/^\uFEFF/, '');
  const lines = text.split(/\r?\n/);
  const title = (text.match(/^#\s+(.+)$/m)?.[1] || file.originalname.replace(/\.(md|markdown)$/i, '')).trim();
  const meta = text.match(/^>\s*(.+)$/m)?.[1] || '';
  const metaParts = meta.split('·').map((part) => part.trim());
  const subject = metaParts.find((part) => ['数学', '语文', '英语', '物理', '化学', '生物', '历史', '地理'].includes(part)) || '';
  const grade = metaParts.find((part) => /年级|七年级|八年级|九年级/.test(part)) || '';
  const bodyStart = lines.findIndex((line) => /^##\s+正文/.test(line));
  const body = bodyStart >= 0 ? lines.slice(bodyStart + 1) : lines;
  if (!body.some((line) => /^##\s+/.test(line))) return fail(res, 422, 'INVALID_MARKDOWN', '没有找到章节标题');
  const course = createCourse({ owner_id: account, title: title.slice(0, 100), subject, grade, source_type: 'textbook', source_name: file.originalname });
  createMaterial({ owner_id: account, course_id: course.id, filename: file.originalname, mime: 'text/markdown', size: file.size, extraction_status: 'indexed', text_content: text, format: 'text', material_role: 'textbook' });
  let chapterId = ''; let sectionId = ''; let chapterOrdinal = 0; let sectionOrdinal = 0; let lessonOrdinal = 0;
  let chapterTitle = ''; let sectionTitle = '';
  let chapterBuffer: string[] = []; let sectionBuffer: string[] = [];
  let current: { unitId: string; title: string; body: string[] } | null = null;
  let importedChars = 0;
  const flushLesson = () => {
    if (current) {
      const content = current.body.join('\n').trim();
      if (content) {
        createLesson({ owner_id: account, course_id: course.id, unit_id: current.unitId, ordinal: lessonOrdinal++, title: current.title, objective: '', content, key_points: [], source_refs: [], status: 'ready' });
        importedChars += content.length;
      }
      current = null;
    }
  };
  const flushSection = () => {
    if (sectionBuffer.join('\n').trim() && sectionId) {
      const content = sectionBuffer.join('\n').trim();
      createLesson({ owner_id: account, course_id: course.id, unit_id: sectionId, ordinal: lessonOrdinal++, title: sectionTitle || '正文', objective: '', content, key_points: [], source_refs: [], status: sectionTitle ? 'ready' : 'draft' });
      importedChars += content.length;
    }
    sectionBuffer = [];
  };
  const flushChapter = () => {
    if (chapterBuffer.join('\n').trim() && chapterId) {
      const content = chapterBuffer.join('\n').trim();
      createLesson({ owner_id: account, course_id: course.id, unit_id: chapterId, ordinal: lessonOrdinal++, title: chapterTitle || '正文', objective: '', content, key_points: [], source_refs: [], status: 'draft' });
      importedChars += content.length;
    }
    chapterBuffer = [];
  };
  for (const line of body) {
    if (/^##\s+/.test(line)) {
      flushLesson(); flushSection(); flushChapter();
      chapterOrdinal += 1; sectionOrdinal = 0; lessonOrdinal = 0;
      chapterTitle = line.replace(/^##\s+/, '').trim(); sectionTitle = '';
      chapterId = createUnit({ owner_id: account, course_id: course.id, level: 'chapter', ordinal: chapterOrdinal, title: chapterTitle, status: 'parsed' }).id;
      sectionId = ''; continue;
    }
    if (/^###\s+/.test(line)) {
      flushLesson(); flushSection();
      sectionOrdinal += 1; lessonOrdinal = 0;
      sectionTitle = line.replace(/^###\s+/, '').trim();
      sectionId = createUnit({ owner_id: account, course_id: course.id, parent_id: chapterId, level: 'section', ordinal: sectionOrdinal, title: sectionTitle, status: 'parsed' }).id;
      continue;
    }
    if (/^####\s+/.test(line)) {
      flushLesson(); flushSection();
      current = { unitId: sectionId || chapterId, title: line.replace(/^####\s+/, '').trim(), body: [] };
      continue;
    }
    if (current) current.body.push(line);
    else if (sectionId) sectionBuffer.push(line);
    else chapterBuffer.push(line);
  }
  flushLesson(); flushSection(); flushChapter();
  const bodyChars = body.join('\n').replace(/\s+/g, '').length;
  if (importedChars < Math.max(40, Math.floor(bodyChars * 0.65))) {
    updateCourseStatus(account, course.id, { status: 'failed', stage: 'failed', progress: 0, error: '导入正文不完整，未标记成功' });
    return res.status(422).json({ error: { code: 'INCOMPLETE_MARKDOWN', message: `导入正文不完整：原文 ${bodyChars} 字，导入 ${importedChars} 字` } });
  }
  updateCourseStatus(account, course.id, { status: 'ready', stage: 'done', progress: 100, error: '', parse_quality: 'complete' });
  res.status(201).json({ course: getCourse(account, course.id), imported_chars: importedChars, source_chars: bodyChars });
});

router.get('/courses/:id', (req: Request, res: Response) => {
  const account = owner(res);
  const courseOwnerId = courseOwner(req.params.id);
  const readOwner = courseOwnerId && (courseOwnerId === account || canAccessSharedCourse(account, req.params.id)) ? courseOwnerId : '';
  let detail = readOwner ? getCourseDetail(readOwner, req.params.id) : undefined;
  if (!detail) return fail(res, 404, 'NOT_FOUND', 'course not found');
  const job = latestJobOrMarkStalled(readOwner, req.params.id);
  if (job && job.status === 'failed') detail = getCourseDetail(readOwner, req.params.id) || detail;
  res.json({
    ...detail,
    job: job || null,
    events: listGenerationEvents(readOwner, req.params.id).filter((event) => !job || event.job_id === job.id),
  });
});

router.post('/courses/:id/retry', async (req: Request, res: Response) => {
  const account = owner(res);
  const course = getCourse(account, req.params.id);
  if (!course) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  const config = agentModelConfig(req, res);
  if (!config) return;
  const material = getMaterialSource(account, course.id);
  if (!material) return fail(res, 409, 'NO_SOURCE', '没有可用的原始材料，无法重新生成');
  let retryAudit: LearningDocumentAudit | undefined;
  if (material.audit) {
    try { retryAudit = JSON.parse(material.audit) as LearningDocumentAudit; } catch { retryAudit = undefined; }
  }
  const job = createGenerationJob(account, course.id);
  updateCourseStatus(account, course.id, { status: 'generating', stage: 'extracting', progress: 3, error: '' });
  updateJob(account, job.id, { status: 'running', stage: 'extracting' });
  // 立即返回，避免 2MB 扫描 PDF 重新解析时按钮看起来“没反应”。
  res.status(202).json({ course, job });

  setImmediate(() => {
    void (async () => {
      try {
        let retryText = material.text;
        let retryImages: LearningDocumentImage[] = [];
        if (material.blob) {
          const materialName = normalizeUploadFilename(material.filename);
          const parsed = await parseDocumentWithImages({
            originalname: materialName,
            mimetype: material.mime,
            size: material.blob.length,
            buffer: material.blob,
          }, 500_000);
          retryText = cleanCourseText(parsed.text);
          retryImages = parsed.images;
          retryAudit = parsed.audit;
        }
        scheduleGeneration(account, course.id, {
          source_type: course.source_type,
          source_name: normalizeUploadFilename(course.source_name),
          material_id: '',
          text: retryText,
          images: retryImages,
          audit: retryAudit,
        }, config);
      } catch (error) {
        const message = error instanceof Error ? error.message.slice(0, 500) : '重新解析失败';
        updateJob(account, job.id, { status: 'failed', stage: 'failed', error: message });
        updateCourseStatus(account, course.id, { status: 'failed', stage: 'failed', progress: 0, error: message });
      }
    })();
  });
});

router.get('/courses/:id/original', (req: Request, res: Response) => {
  const account = owner(res);
  const material = getMaterialSource(account, req.params.id);
  if (!material?.blob) return fail(res, 404, 'NO_ORIGINAL', '没有可用的原始教材文件');
  const contentType = /\.pdf$/i.test(material.filename || '') ? 'application/pdf' : (material.mime || 'application/pdf');
  res.setHeader('Content-Type', contentType);
  res.setHeader('Content-Length', String(material.blob.length));
  res.setHeader('Content-Disposition', `inline; filename*=UTF-8''${encodeURIComponent(material.filename || 'textbook.pdf')}`);
  res.setHeader('Cache-Control', 'private, max-age=300');
  res.send(material.blob);
});

router.delete('/courses/:id', (req: Request, res: Response) => {
  if (!deleteCourse(owner(res), req.params.id)) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  res.json({ ok: true });
});

router.get('/courses/:id/lessons/:lessonId', (req: Request, res: Response) => {
  const account = owner(res);
  const readOwner = courseOwner(req.params.id) && (courseOwner(req.params.id) === account || canAccessSharedCourse(account, req.params.id)) ? courseOwner(req.params.id)! : '';
  const lesson = readOwner ? getLesson(readOwner, req.params.lessonId) : undefined;
  if (!lesson || lesson.course_id !== req.params.id) return fail(res, 404, 'NOT_FOUND', 'lesson not found');
  const units = listUnits(readOwner, lesson.course_id);
  const points = listKnowledgePoints(readOwner, lesson.course_id, lesson.id).map((point) => ({
    ...point,
    concepts: JSON.parse(point.concepts_json || '[]'),
    formulas: JSON.parse(point.formulas_json || '[]'),
    methods: JSON.parse(point.methods_json || '[]'),
    source_block_ids: JSON.parse(point.source_block_ids_json || '[]'),
  }));
  const referenced = new Set(points.flatMap((point) => point.source_block_ids as string[]));
  const lessonRefs = JSON.parse(lesson.source_refs_json || '[]') as Array<{ line_start?: number; line_end?: number }>;
  const sourceBlocks = listSourceBlocks(readOwner, lesson.course_id).filter((block) => (
    referenced.has(block.id)
    || lessonRefs.some((ref) => typeof ref.line_start === 'number'
      && typeof ref.line_end === 'number'
      && block.line_start <= ref.line_end
      && block.line_end >= ref.line_start)
  ));
  res.json({
    lesson,
    unit: units.find((unit) => unit.id === lesson.unit_id) || null,
    course: getCourse(readOwner, lesson.course_id),
    knowledge_points: points,
    source_blocks: sourceBlocks,
  });
});

router.post('/lessons/:lessonId/complete', (req: Request, res: Response) => {
  const account = owner(res);
  const lesson = getLesson(account, req.params.lessonId);
  if (!lesson) return fail(res, 404, 'NOT_FOUND', '教案不存在');
  const progress = setLessonProgress(account, lesson.course_id, lesson.id, 'completed');
  res.json({
    progress,
    unlocked: { post_class: true },
    message: '本节已完成，课后练习已解锁。',
  });
});

router.get('/courses/:id/diagnosis', (req: Request, res: Response) => {
  const account = owner(res);
  const course = getCourse(account, req.params.id);
  if (!course) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  const latest = getLatestDiagnosis(account, course.id) || null;
  res.json({
    available: Boolean(latest && latest.status === 'ready'),
    status: latest?.status || 'not_started',
    summary: latest?.summary || '',
    mastery: latest ? JSON.parse(latest.mastery_json || '{}') : null,
    message: latest
      ? '学习分析暂时不可用，请稍后再试。'
      : '学习分析暂时不可用，请稍后再试。',
    latest,
  });
});

router.post('/courses/:id/diagnosis', (_req: Request, res: Response) => {
  res.status(501).json({
    error: {
      code: 'NOT_IMPLEMENTED',
      message: '学习分析暂时不可用，请稍后再试。',
    },
  });
});

router.get('/exercises', (req: Request, res: Response) => {
  const account = owner(res);
  const phase = String(req.query.phase || '');
  const validPhase = phase === 'in_class' || phase === 'post_class' ? phase as ExercisePhase : undefined;
  const exercises = listExercises(account, {
    course_id: String(req.query.course_id || '') || undefined,
    lesson_id: String(req.query.lesson_id || '') || undefined,
    phase: validPhase,
  });
  res.json(exercises.map((exercise) => ({
    ...exercise,
    answer: undefined,
    locked: exercise.phase === 'post_class' ? !isLessonCompleted(account, exercise.lesson_id) : false,
  })));
});

router.get('/exercises/:id', (req: Request, res: Response) => {
  const account = owner(res);
  const exercise = getExercise(account, req.params.id);
  if (!exercise) return fail(res, 404, 'NOT_FOUND', '练习不存在');
  res.json({
    ...exercise,
    answer: undefined,
    locked: exercise.phase === 'post_class' ? !isLessonCompleted(account, exercise.lesson_id) : false,
  });
});

router.post('/exercises/:id/attempts', (req: Request, res: Response) => {
  const account = owner(res);
  const exercise = getExercise(account, req.params.id);
  if (!exercise) return fail(res, 404, 'NOT_FOUND', '练习不存在');
  if (exercise.phase === 'post_class' && !isLessonCompleted(account, exercise.lesson_id)) {
    return fail(res, 409, 'LESSON_LOCKED', '完成本节学习后才能提交课后练习');
  }
  const answer = String(req.body.answer || '').trim();
  if (!answer) return fail(res, 422, 'INVALID_BODY', '请先填写答案');
  if (answer.length > 8000) return fail(res, 422, 'INVALID_BODY', '答案过长');
  const attempt = createAttempt({
    owner_id: account,
    exercise_id: exercise.id,
    course_id: exercise.course_id,
    lesson_id: exercise.lesson_id,
    answer,
    status: 'submitted',
    feedback: '答案已保存。',
  });
  res.status(201).json({
    attempt,
    grading: { available: false, status: 'submitted', message: '已提交，答案已保存。' },
  });
});

router.get('/attempts', (req: Request, res: Response) => {
  res.json(listAttempts(owner(res), {
    course_id: String(req.query.course_id || '') || undefined,
    exercise_id: String(req.query.exercise_id || '') || undefined,
  }));
});

export default router;

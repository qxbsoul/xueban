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
import { normalizeUploadFilename, parseDocumentWithImages, type LearningDocumentAudit, type LearningDocumentImage } from '../services/xueban-attachments';
import { modelConfig } from './xueban-chat';
import { runCourseGeneration } from '../services/learning';
import {
  COURSE_SOURCE_TYPES,
  createCourse,
  createGenerationJob,
  updateCourseStatus,
  updateJob,
  createMaterial,
  createAttempt,
  deleteCourse,
  getCourse,
  getCourseDetail,
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
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 50 * 1024 * 1024, files: 1 } });

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

const STALE_GENERATION_MS = 5 * 60 * 1000;

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

function scheduleGeneration(ownerId: string, courseId: string, source: { source_type: CourseSourceType; source_name?: string; material_id?: string; text: string; images?: LearningDocumentImage[]; audit?: LearningDocumentAudit }, config: ReturnType<typeof modelConfig>): void {
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

router.get('/courses', (_req: Request, res: Response) => {
  const account = owner(res);
  const courses = listCourses(account).map((course) => {
    latestJobOrMarkStalled(account, course.id);
    return { ...(getCourse(account, course.id) || course), job: getLatestJob(account, course.id) || null };
  });
  res.json(courses);
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

router.get('/courses/:id', (req: Request, res: Response) => {
  const account = owner(res);
  let detail = getCourseDetail(account, req.params.id);
  if (!detail) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  const job = latestJobOrMarkStalled(account, req.params.id);
  if (job && job.status === 'failed') detail = getCourseDetail(account, req.params.id) || detail;
  res.json({
    ...detail,
    job: job || null,
    events: listGenerationEvents(account, req.params.id).filter((event) => !job || event.job_id === job.id),
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

router.delete('/courses/:id', (req: Request, res: Response) => {
  if (!deleteCourse(owner(res), req.params.id)) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  res.json({ ok: true });
});

router.get('/courses/:id/lessons/:lessonId', (req: Request, res: Response) => {
  const account = owner(res);
  const lesson = getLesson(account, req.params.lessonId);
  if (!lesson || lesson.course_id !== req.params.id) return fail(res, 404, 'NOT_FOUND', '教案不存在');
  const units = listUnits(account, lesson.course_id);
  const points = listKnowledgePoints(account, lesson.course_id, lesson.id).map((point) => ({
    ...point,
    concepts: JSON.parse(point.concepts_json || '[]'),
    formulas: JSON.parse(point.formulas_json || '[]'),
    methods: JSON.parse(point.methods_json || '[]'),
    source_block_ids: JSON.parse(point.source_block_ids_json || '[]'),
  }));
  const referenced = new Set(points.flatMap((point) => point.source_block_ids as string[]));
  const lessonRefs = JSON.parse(lesson.source_refs_json || '[]') as Array<{ line_start?: number; line_end?: number }>;
  const sourceBlocks = listSourceBlocks(account, lesson.course_id).filter((block) => (
    referenced.has(block.id)
    || lessonRefs.some((ref) => typeof ref.line_start === 'number'
      && typeof ref.line_end === 'number'
      && block.line_start <= ref.line_end
      && block.line_end >= ref.line_start)
  ));
  res.json({
    lesson,
    unit: units.find((unit) => unit.id === lesson.unit_id) || null,
    course: getCourse(account, lesson.course_id),
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

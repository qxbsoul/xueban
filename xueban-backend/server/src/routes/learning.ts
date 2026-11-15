/**
 * 学习中心路由（2026-11-15，框架阶段）。
 *
 * 账号隔离：owner_id 只来自 Bearer token，请求体不接收 owner_id。
 * 解析/生成能力后续替换 services/learning.ts 内部实现，HTTP 契约保持不变。
 */
import { Router, type Request, type Response } from 'express';
import multer from 'multer';
import { requireAccount } from '../lib/memory-account';
import { runCourseGeneration } from '../services/learning';
import {
  COURSE_SOURCE_TYPES,
  createCourse,
  createGenerationJob,
  createMaterial,
  createAttempt,
  deleteCourse,
  getCourse,
  getCourseDetail,
  getExercise,
  getLatestJob,
  getLatestDiagnosis,
  getLesson,
  isLessonCompleted,
  listAttempts,
  listCourses,
  listExercises,
  listMaterials,
  listUnits,
  setLessonProgress,
  type CourseSourceType,
  type ExercisePhase,
} from '../storage/learning';

const router = Router();
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 15 * 1024 * 1024, files: 1 } });

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

const TEXT_EXTENSIONS = ['.txt', '.md', '.markdown', '.json', '.csv'];

function extractText(file: Express.Multer.File): string {
  const name = (file.originalname || '').toLowerCase();
  const mime = (file.mimetype || '').toLowerCase();
  const supported = mime.startsWith('text/')
    || mime === 'application/json'
    || mime === 'application/csv'
    || TEXT_EXTENSIONS.some((extension) => name.endsWith(extension));
  if (!supported) {
    throw Object.assign(new Error('暂时无法读取这个文件，请上传文本文件，或直接粘贴内容。'), { status: 422 });
  }
  const text = file.buffer.toString('utf8').replace(/\u0000/g, '').trim();
  if (!text) throw Object.assign(new Error('上传文件没有可读取的文本内容。'), { status: 422 });
  return text.slice(0, 200_000);
}

function scheduleGeneration(ownerId: string, courseId: string, source: { source_type: CourseSourceType; source_name?: string; material_id?: string; text: string }): void {
  // 框架阶段用进程内异步任务模拟生成；后续替换为队列/agent 编排。
  setImmediate(() => {
    try {
      runCourseGeneration(ownerId, courseId, source);
    } catch (error) {
      console.error('[learning] generation failed', error);
    }
  });
}

router.get('/courses', (_req: Request, res: Response) => {
  const courses = listCourses(owner(res)).map((course) => ({
    ...course,
    job: getLatestJob(owner(res), course.id) || null,
  }));
  res.json(courses);
});

router.post('/courses', upload.single('file'), (req: Request, res: Response) => {
  try {
    const account = owner(res);
    const title = String(req.body.title || '').trim().slice(0, 100);
    const sourceType = String(req.body.source_type || 'textbook') as CourseSourceType;
    if (!title) return fail(res, 422, 'INVALID_BODY', '请填写课程/教材名称');
    if (!COURSE_SOURCE_TYPES.includes(sourceType)) return fail(res, 422, 'INVALID_BODY', '来源类型不合法');
    const file = req.file;
    if (file && sourceType === 'description') {
      return fail(res, 422, 'INVALID_BODY', '自主描述不需要上传文件，写下学习目标即可');
    }
    let text = String(req.body.source_text || '').trim();
    if (file) text = extractText(file);
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
    });
    const job = createGenerationJob(account, course.id);
    scheduleGeneration(account, course.id, {
      source_type: sourceType,
      source_name: file?.originalname || course.source_name,
      material_id: material.id,
      text,
    });
    res.status(201).json({ course: getCourse(account, course.id), job });
  } catch (error) {
    const status = (error as { status?: number }).status || 500;
    if (status >= 500) console.error('[learning] create course', error);
    fail(res, status, status === 422 ? 'INVALID_MATERIAL' : 'INTERNAL', error instanceof Error ? error.message : '创建课程失败');
  }
});

router.get('/courses/:id', (req: Request, res: Response) => {
  const detail = getCourseDetail(owner(res), req.params.id);
  if (!detail) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  res.json({ ...detail, job: getLatestJob(owner(res), req.params.id) || null });
});

router.post('/courses/:id/retry', (req: Request, res: Response) => {
  const account = owner(res);
  const course = getCourse(account, req.params.id);
  if (!course) return fail(res, 404, 'NOT_FOUND', '课程不存在');
  const material = listMaterials(account, course.id).slice(-1)[0];
  if (!material) return fail(res, 409, 'NO_SOURCE', '没有可用的原始材料，无法重新生成');
  const job = createGenerationJob(account, course.id);
  scheduleGeneration(account, course.id, {
    source_type: course.source_type,
    source_name: course.source_name,
    material_id: material.id,
    text: material.text_content,
  });
  res.status(202).json({ course, job });
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
  res.json({
    lesson,
    unit: units.find((unit) => unit.id === lesson.unit_id) || null,
    course: getCourse(account, lesson.course_id),
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

import { useEffect, useMemo, useState, type FormEvent } from 'react';
import {
  ArrowRight,
  BookOpen,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleCheck,
  Clock,
  FileText,
  GraduationCap,
  Loader2,
  ListChecks,
  Lock,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
} from 'lucide-react';
import ChatRichContent from './ChatRichContent';
import { useWorkbench } from '../store';
import { findModelPreset, loadApiSettings, loadCustomModels } from '../settings-storage';
import {
  SOURCE_TYPE_LABEL,
  QUALITY_LABEL,
  completeLearningLesson,
  createLearningCourse,
  deleteLearningCourse,
  getLearningCourse,
  getLearningLesson,
  listLearningCourses,
  listLearningExercises,
  retryLearningCourse,
  submitLearningAttempt,
  type ExercisePhase,
  type LearningCourse,
  type LearningCourseDetail,
  type LearningExercise,
  type LearningLessonDetail,
  type LearningSourceType,
  type LearningSection,
} from '../learning-api';
import './learning-center.css';

type Tab = 'courses' | 'materials' | 'practice';

const TABS: Array<{ key: Tab; label: string; hint: string; icon: typeof BookOpen }> = [
  { key: 'courses', label: '我的课程', hint: '继续你的课程，按自己的节奏完成每一节。', icon: BookOpen },
  { key: 'materials', label: '教材管理', hint: '上传教材目录，或直接描述你想学习的内容。', icon: Upload },
  { key: 'practice', label: '练习中心', hint: '先完成课上练习，再用课后练习巩固。', icon: ListChecks },
];

const STATUS_LABEL: Record<string, string> = {
  draft: '准备中',
  generating: '生成中',
  ready: '可学习',
  failed: '生成失败',
};

function formatTime(value: number | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

function studentText(value: string): string {
  return value
    .replace(/[\ufffd\u25a1\u25a0\u2612\ue000-\uf8ff]/g, '')
    .split(/\r?\n/)
    .filter((line) => !/(仅供个人|未经授权|不得另做他用|版权所有|翻印|侵权|内部资料)/.test(line))
    .join('\n')
    .replace(/^>\s*当前教案为框架生成稿[^\n]*\n?/gm, '')
    .replace(/【待教研生成】/g, '')
    .replace(/当前为框架占位题[^\n]*\n?/g, '')
    .trim();
}

function cleanSupplementSource(source: string): string {
  if (!source) return '';
  // 图片引用保留，用占位标签展示；不渲染 pdf:// 这类不可访问地址。
  if (/!\[[^\]]*\]\((?:pdf|docx|image):\/\//i.test(source)) return source;
  const meaningful = source
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !/^#{1,3}\s/.test(line) && !/^[-*]\s/.test(line))
    .filter((line) => ((line.match(/[\u4e00-\u9fa5]/g) || []).length >= 4 || line.length >= 12));
  // 扫描版 PDF 的文字层经常只剩题号、页码和孤立字符；宁可折叠隐藏，也不展示碎片。
  return meaningful.length >= 2 ? source : '';
}

function splitLessonContent(content: string): { main: string; source: string } {
  const marker = '## 补充原文';
  const index = content.indexOf(marker);
  if (index < 0) return { main: content, source: '' };
  return { main: content.slice(0, index).trim(), source: cleanSupplementSource(content.slice(index).trim()) };
}

function parseJson<T>(value: string, fallback: T): T {
  try { return JSON.parse(value) as T; } catch { return fallback; }
}

function courseLessonQuality(course: LearningCourse): string {
  try {
    const value = JSON.parse(course.quality_json || '{}') as { lesson?: { level?: string } };
    return value.lesson?.level || '';
  } catch {
    return '';
  }
}

function generationStageLabel(stage: string): string {
  if (!stage) return '正在准备';
  if (stage === 'extracting') return '正在读取教材原文';
  if (stage === 'outline' || stage.startsWith('outline ')) return '正在整理章节目录';
  if (stage.startsWith('sections')) {
    const progress = stage.replace('sections', '').trim();
    return progress ? `正在解析知识点 ${progress}` : '正在解析知识点';
  }
  if (stage === 'persisting') return '正在保存课程';
  if (stage === 'done') return '课程已生成';
  if (stage === 'failed') return '生成失败';
  return stage;
}

export default function LearningCenterView({ active = true }: { active?: boolean }) {
  const { conversations, activeConversationId } = useWorkbench();
  const [tab, setTab] = useState<Tab>('courses');
  const [courses, setCourses] = useState<LearningCourse[]>([]);
  const [coursesLoading, setCoursesLoading] = useState(true);
  const [coursesError, setCoursesError] = useState('');
  const [notice, setNotice] = useState('');

  const [selectedCourseId, setSelectedCourseId] = useState('');
  const [detail, setDetail] = useState<LearningCourseDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [selectedLessonId, setSelectedLessonId] = useState('');
  const [lessonDetail, setLessonDetail] = useState<LearningLessonDetail | null>(null);
  const [lessonLoading, setLessonLoading] = useState(false);
  const [lessonError, setLessonError] = useState('');
  const [completing, setCompleting] = useState(false);

  const [form, setForm] = useState({
    title: '',
    subject: '数学',
    grade: '七年级',
    source_type: 'textbook' as LearningSourceType,
    source_text: '',
  });
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');

  const [practiceCourseId, setPracticeCourseId] = useState('');
  const [practiceDetail, setPracticeDetail] = useState<LearningCourseDetail | null>(null);
  const [practiceLessonId, setPracticeLessonId] = useState('');
  const [practicePhase, setPracticePhase] = useState<ExercisePhase>('in_class');
  const [exercises, setExercises] = useState<LearningExercise[]>([]);
  const [practiceLoading, setPracticeLoading] = useState(false);
  const [practiceError, setPracticeError] = useState('');
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittingExercise, setSubmittingExercise] = useState('');
  const [attemptMessages, setAttemptMessages] = useState<Record<string, string>>({});

  function learningModel() {
    const activeMeta = conversations.find((item) => item.id === activeConversationId);
    const selected = activeMeta?.model;
    if (selected?.startsWith('custom:')) {
      const entry = loadCustomModels().find((item) => item.id === selected.slice('custom:'.length));
      if (entry) {
        return { provider: 'openai', model: entry.model, base_url: entry.baseUrl, customKey: entry.key };
      }
    }
    if (selected) {
      const preset = findModelPreset(selected);
      if (preset) {
        if (preset.preset === 'custom') {
          const entry = loadCustomModels().find((item) => item.id === preset.customId);
          return { provider: 'openai', model: entry?.model || preset.model, base_url: entry?.baseUrl || preset.baseUrl, customKey: entry?.key };
        }
        return { provider: preset.preset, model: preset.model, base_url: preset.baseUrl };
      }
    }
    const settings = loadApiSettings();
    return { provider: settings.provider, model: settings.model, base_url: settings.baseUrl };
  }

  async function loadCourses(silent = false) {
    if (!silent) setCoursesLoading(true);
    if (!silent) setCoursesError('');
    try {
      setCourses(await listLearningCourses());
    } catch (error) {
      if (!silent) setCoursesError(error instanceof Error ? error.message : '加载课程失败');
    } finally {
      if (!silent) setCoursesLoading(false);
    }
  }

  useEffect(() => {
    if (!active) return;
    void loadCourses();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!active) return;
    if (!courses.some((course) => course.status === 'generating' || course.status === 'draft')) return;
    const timer = window.setTimeout(() => { void loadCourses(true); }, 2500);
    return () => window.clearTimeout(timer);
  }, [active, courses]);

  useEffect(() => {
    if (!active || !selectedCourseId || !detail) return;
    if (detail.course.status !== 'generating' && detail.course.status !== 'draft') return;
    const timer = window.setTimeout(async () => {
      try {
        setDetail(await getLearningCourse(selectedCourseId));
      } catch {
        // 下一轮继续轮询；错误由页面现有提示处理。
      }
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [active, selectedCourseId, detail]);

  useEffect(() => {
    if (!active) return;
    if (tab === 'practice' && courses.length === 0) void loadCourses();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, tab]);

  async function openCourse(courseId: string) {
    setSelectedCourseId(courseId);
    setDetail(null);
    setLessonDetail(null);
    setSelectedLessonId('');
    setDetailLoading(true);
    setDetailError('');
    try {
      setDetail(await getLearningCourse(courseId));
    } catch (error) {
      setDetailError(error instanceof Error ? error.message : '加载课程失败');
    } finally {
      setDetailLoading(false);
    }
  }

  async function openLesson(courseId: string, lessonId: string) {
    setSelectedLessonId(lessonId);
    setLessonDetail(null);
    setLessonLoading(true);
    setLessonError('');
    try {
      setLessonDetail(await getLearningLesson(courseId, lessonId));
    } catch (error) {
      setLessonError(error instanceof Error ? error.message : '加载教案失败');
    } finally {
      setLessonLoading(false);
    }
  }

  async function markCompleted() {
    if (!lessonDetail || completing) return;
    setCompleting(true);
    setLessonError('');
    try {
      const result = await completeLearningLesson(lessonDetail.lesson.id);
      setNotice(result.message);
      await openCourse(lessonDetail.lesson.course_id);
      await openLesson(lessonDetail.lesson.course_id, lessonDetail.lesson.id);
    } catch (error) {
      setLessonError(error instanceof Error ? error.message : '保存失败');
    } finally {
      setCompleting(false);
    }
  }

  async function submitCourse(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setFormError('');
    if (!form.title.trim()) { setFormError('请填写课程名称'); return; }
    if (form.source_type === 'textbook' && !file && !form.source_text.trim()) {
      setFormError('上传教材文件，或粘贴教材目录'); return;
    }
    if (form.source_type === 'curriculum' && form.source_text.trim().length < 10) {
      setFormError('请粘贴课标或教学大纲内容，至少 10 个字'); return;
    }
    if (form.source_type === 'description' && form.source_text.trim().length < 10) {
      setFormError('多写几句你想学习的内容，至少 10 个字'); return;
    }
    setSubmitting(true);
    try {
      const result = await createLearningCourse({ ...form, file, model: learningModel() });
      setNotice('课程已创建，正在为你整理目录与教案');
      setForm({ ...form, title: '', source_text: '' });
      setFile(null);
      await loadCourses();
      setTab('courses');
      await openCourse(result.course.id);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '创建课程失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function retryCourse(courseId: string) {
    try {
      await retryLearningCourse(courseId, learningModel());
      setNotice('已重新开始整理课程');
      await loadCourses();
      if (selectedCourseId === courseId) await openCourse(courseId);
    } catch (error) {
      const message = error instanceof Error ? error.message : '重试失败';
      setCoursesError(message);
      setNotice(`重新生成失败：${message}`);
    }
  }

  async function removeCourse(courseId: string) {
    if (!window.confirm('删除后，这门课程的目录、教案与练习都会移除。确认删除？')) return;
    try {
      await deleteLearningCourse(courseId);
      if (selectedCourseId === courseId) { setSelectedCourseId(''); setDetail(null); setLessonDetail(null); }
      setNotice('课程已删除');
      await loadCourses();
    } catch (error) {
      setCoursesError(error instanceof Error ? error.message : '删除失败');
    }
  }

  async function openPractice(courseId: string, lessonId = '', phase: ExercisePhase = 'in_class') {
    setTab('practice');
    setPracticeCourseId(courseId);
    setPracticeLessonId(lessonId);
    setPracticePhase(phase);
    setPracticeDetail(null);
    setExercises([]);
    setPracticeLoading(true);
    setPracticeError('');
    try {
      setPracticeDetail(await getLearningCourse(courseId));
    } catch (error) {
      setPracticeError(error instanceof Error ? error.message : '加载练习失败');
      setPracticeLoading(false);
    }
  }

  useEffect(() => {
    if (!active || tab !== 'practice' || !practiceCourseId) return;
    let cancelled = false;
    setPracticeLoading(true);
    setPracticeError('');
    void listLearningExercises({
      course_id: practiceCourseId,
      lesson_id: practiceLessonId || undefined,
      phase: practicePhase,
    }).then((items) => {
      if (!cancelled) setExercises(items);
    }).catch((error) => {
      if (!cancelled) setPracticeError(error instanceof Error ? error.message : '加载练习失败');
    }).finally(() => {
      if (!cancelled) setPracticeLoading(false);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, tab, practiceCourseId, practiceLessonId, practicePhase]);

  const readyCourses = useMemo(() => courses.filter((course) => course.status === 'ready'), [courses]);
  const sections: Array<{ chapterTitle: string; section: LearningSection }> = useMemo(() => {
    const source = practiceDetail || detail;
    if (!source) return [];
    return source.chapters.flatMap((chapter) => chapter.sections.map((section) => ({ chapterTitle: chapter.title, section })));
  }, [practiceDetail, detail]);

  async function submitExercise(exercise: LearningExercise) {
    const answer = (answers[exercise.id] || '').trim();
    if (!answer || submittingExercise) return;
    setSubmittingExercise(exercise.id);
    try {
      const result = await submitLearningAttempt(exercise.id, answer);
      setAttemptMessages((previous) => ({ ...previous, [exercise.id]: result.grading.message }));
    } catch (error) {
      setAttemptMessages((previous) => ({ ...previous, [exercise.id]: error instanceof Error ? error.message : '提交失败' }));
    } finally {
      setSubmittingExercise('');
    }
  }

  function completedInCourse(course: LearningCourseDetail): number {
    return course.chapters.reduce(
      (sum, chapter) => sum + chapter.sections.filter((section) => section.progress?.status === 'completed').length,
      0,
    );
  }

  function renderCourseList() {
    return (
      <>
        <header className="learning-section-head">
          <div>
            <span className="learning-eyebrow">你的课程</span>
            <h3>我的课程</h3>
            <p>从目录进入每一节，先读教案，再完成练习。</p>
          </div>
          <button type="button" className="learning-btn primary" onClick={() => setTab('materials')}>
            <Plus size={15} /> 创建课程
          </button>
        </header>

        {coursesLoading ? (
          <div className="learning-course-grid">
            {[0, 1, 2].map((item) => <div className="learning-skeleton-card" key={item} />)}
          </div>
        ) : coursesError ? (
          <div className="learning-error">{coursesError}<button type="button" onClick={() => void loadCourses()}>重试</button></div>
        ) : courses.length === 0 ? (
          <div className="learning-empty-hero">
            <div className="learning-empty-icon"><GraduationCap size={34} /></div>
            <h3>从第一门课程开始</h3>
            <p>上传教材目录，或直接告诉我们你想学什么。</p>
            <button type="button" className="learning-btn primary" onClick={() => setTab('materials')}>去创建课程 <ArrowRight size={15} /></button>
          </div>
        ) : (
          <div className="learning-course-grid">
            {courses.map((course) => (
              <article className="learning-course-card" key={course.id}>
                <div className={`learning-course-cover subject-${(course.subject || '').length % 4}`}>
                  <span className="learning-course-mark">{course.title.slice(0, 1)}</span>
                  <span className={`learning-status-pill status-${course.status}`}>{STATUS_LABEL[course.status]}</span>
                </div>
                <div className="learning-course-body">
                  <div className="learning-course-kicker">{SOURCE_TYPE_LABEL[course.source_type]} · {course.grade || '通用'}
                    {course.parse_quality && course.parse_quality !== 'complete' && <span className={`learning-quality quality-${course.parse_quality}`}>{QUALITY_LABEL[course.parse_quality]}</span>}
                    {courseLessonQuality(course) && courseLessonQuality(course) !== 'complete' && <span className={`learning-quality quality-${courseLessonQuality(course)}`}>学案待确认</span>}
                  </div>
                  <h3>{course.title}</h3>
                  <p>{course.subject || '未设置学科'}</p>
                  {course.status === 'generating' && (
                    <div className="learning-progress-row">
                      <div className="learning-progress"><span style={{ width: `${course.progress}%` }} /></div>
                      <small>{course.progress}%</small>
                    </div>
                  )}
                  {course.status === 'failed' && <p className="learning-card-error">{course.error || '生成没有完成，可以重新试一次。'}</p>}
                  <div className="learning-course-foot">
                    <span className="learning-updated"><Clock size={12} /> {formatTime(course.updated_at)}</span>
                    <div className="learning-card-actions">
                      {course.status === 'ready' && (
                        <button type="button" className="learning-btn primary small" onClick={() => void openCourse(course.id)}>进入课程</button>
                      )}
                      {course.status === 'failed' && (
                        <button type="button" className="learning-btn small" onClick={() => void retryCourse(course.id)}>重新生成</button>
                      )}
                      {course.status !== 'ready' && course.status !== 'failed' && (
                        <span className="learning-muted">请稍候…</span>
                      )}
                      <button type="button" className="learning-icon-btn" title="删除课程" onClick={() => void removeCourse(course.id)}><Trash2 size={14} /></button>
                    </div>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </>
    );
  }

  function renderCourseDetail() {
    if (!detail) return null;
    const completed = completedInCourse(detail);
    const total = detail.stats.lessons || 0;
    return (
      <div className="learning-detail">
        <button type="button" className="learning-back" onClick={() => { setDetail(null); setLessonDetail(null); setSelectedCourseId(''); }}>
          <ChevronLeft size={16} /> 全部课程
        </button>

        <div className="learning-course-hero">
          <div className="learning-hero-main">
            <div className="learning-hero-mark">{detail.course.title.slice(0, 1)}</div>
            <div>
              <div className="learning-hero-meta">
                <span>{SOURCE_TYPE_LABEL[detail.course.source_type]}</span>
                <span>{detail.course.subject || '通用'}</span>
                <span>{detail.course.grade || '未设置年级'}</span>
                {detail.course.parse_quality && detail.course.parse_quality !== 'complete' && (
                  <span className={`learning-quality quality-${detail.course.parse_quality}`}>{QUALITY_LABEL[detail.course.parse_quality]}</span>
                )}
              </div>
              <h2>{detail.course.title}</h2>
              <p>{detail.stats.chapters} 个章节 · {detail.stats.sections} 个小节 · {detail.stats.exercises} 道练习</p>
            </div>
          </div>
          <div className="learning-hero-progress">
            <div><strong>{completed}</strong><span> / {total} 节已完成</span></div>
            <div className="learning-progress"><span style={{ width: `${total ? Math.round((completed / total) * 100) : 0}%` }} /></div>
          </div>
        </div>

        {detail.course.status === 'ready' && detail.course.parse_quality && detail.course.parse_quality !== 'complete' && (() => {
          const quality = parseJson<{
            readability?: { pages?: number; text_pages?: number; fallback_pages?: number; unreadable_pages?: number; skipped_visual_pages?: number };
            lesson?: { level?: string; complete?: number; partial?: number; needs_review?: number; failed?: number };
          }>(detail.course.quality_json || '{}', {});
          const readability = quality.readability;
          const lessonQuality = quality.lesson;
          return (
            <div className="learning-inline-note quality-note">
              本次解析有部分内容需要确认：部分知识点或原文归属尚未完全确定。课程可以继续学习，标记为“待确认”的内容请以教材原文为准。
              {readability && typeof readability.pages === 'number' && readability.pages > 0 && (
                <span className="learning-readability">
                  原文可读性：正常文本 {readability.text_pages || 0}/{readability.pages} 页
                  {readability.fallback_pages ? `；${readability.fallback_pages} 页使用页面图像识别` : ''}
                  {readability.unreadable_pages ? `；${readability.unreadable_pages} 页未能可靠识别` : ''}
                  {readability.skipped_visual_pages ? `；${readability.skipped_visual_pages} 页因视觉预算未识别` : ''}
                </span>
              )}
              {lessonQuality && lessonQuality.level && lessonQuality.level !== 'complete' && (
                <span className="learning-readability">
                  教学设计：{lessonQuality.complete || 0} 节完整
                  {lessonQuality.partial ? `；${lessonQuality.partial} 节部分完成` : ''}
                  {lessonQuality.needs_review ? `；${lessonQuality.needs_review} 节待确认` : ''}
                  {lessonQuality.failed ? `；${lessonQuality.failed} 节生成失败` : ''}
                </span>
              )}
            </div>
          );
        })()}
        {detail.course.status === 'ready' && (() => {
          try {
            const quality = JSON.parse(detail.course.quality_json || '{}') as { lesson?: { level?: string; complete?: number; partial?: number; needs_review?: number; failed?: number } };
            const lesson = quality.lesson;
            if (!lesson || !lesson.level || lesson.level === 'complete') return null;
            return (
              <div className="learning-inline-note quality-note">
                学案教学设计仍有待确认的部分：{lesson.complete || 0} 节完整
                {lesson.partial ? `，${lesson.partial} 节部分完成` : ''}
                {lesson.needs_review ? `，${lesson.needs_review} 节待确认` : ''}
                {lesson.failed ? `，${lesson.failed} 节生成失败` : ''}。
              </div>
            );
          } catch {
            return null;
          }
        })()}

        {detail.course.status === 'ready' && detail.course.error && (
          <div className="learning-inline-note quality-note">
            上次重新解析没有完成，当前保留的是上一次成功生成的课程版本。
          </div>
        )}

        {detail.course.status === 'generating' && (
          <div className="learning-inline-note">
            <Loader2 size={15} className="spin" /> 正在整理目录与教案，完成后即可进入学习。
            <span className="learning-generation-stage">当前：{generationStageLabel(detail.job?.stage || detail.course.stage)}</span>
          </div>
        )}
        {detail.course.status === 'failed' && (
          <div className="learning-error">
            {detail.course.error || '生成没有完成，可以重新试一次。'}
            <button type="button" onClick={() => void retryCourse(detail.course.id)}>重新生成</button>
          </div>
        )}

        <div className="learning-detail-layout">
          <aside className="learning-outline-card">
            <header><h4>课程目录</h4><span>{detail.stats.sections} 节</span></header>
            {detail.chapters.map((chapter, chapterIndex) => (
              <section className="learning-chapter" key={chapter.id}>
                <h5><span>{String(chapterIndex + 1).padStart(2, '0')}</span>{chapter.title}</h5>
                {chapter.sections.map((section, sectionIndex) => (
                  <div className="learning-section-unit" key={section.id}>
                    <button
                      type="button"
                      className={selectedLessonId === section.lesson?.id ? 'learning-section active' : 'learning-section'}
                      disabled={!section.lesson || detail.course.status !== 'ready'}
                      onClick={() => section.lesson && void openLesson(detail.course.id, section.lesson.id)}
                    >
                      <span className="learning-section-index">
                        {section.progress?.status === 'completed' ? <CircleCheck size={15} /> : String(sectionIndex + 1).padStart(2, '0')}
                      </span>
                      <span className="learning-section-text">
                        <strong>
                          {section.title}
                          {section.status === 'needs_review' && <em className="learning-section-status">待确认</em>}
                        </strong>
                        <small>{section.exercise_counts.in_class} 道课上 · {section.exercise_counts.post_class} 道课后</small>
                      </span>
                      <ChevronRight size={15} className="learning-section-arrow" />
                    </button>
                    {section.knowledge_points.length > 0 && (
                      <div className="learning-point-list">
                        {section.knowledge_points.map((point) => (
                          <button
                            type="button"
                            key={point.id}
                            className={selectedLessonId === section.lesson?.id ? 'learning-point active' : 'learning-point'}
                            onClick={() => section.lesson && void openLesson(detail.course.id, section.lesson.id)}
                          >
                            <span className="learning-point-dot" />
                            <span>{point.title}</span>
                            {point.origin === 'textbook' && point.status === 'parsed' && <em className="source">原文</em>}
                            {point.origin === 'agent_inferred' && <em className="agent">Agent 归纳</em>}
                            {point.status === 'needs_review' && <em>待确认</em>}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </section>
            ))}
          </aside>

          <article className="learning-lesson-card">
            {lessonLoading && <div className="learning-lesson-placeholder"><Loader2 className="spin" /> 正在打开教案…</div>}
            {lessonError && <div className="learning-error">{lessonError}</div>}
            {!lessonLoading && !lessonDetail && (
              <div className="learning-lesson-placeholder">
                <BookOpen size={28} />
                <strong>选择一节开始学习</strong>
                <span>左侧目录会显示每一节的内容与练习。</span>
              </div>
            )}
            {lessonDetail && (
              <>
                <header className="learning-lesson-head">
                  <div className="learning-lesson-kicker">本节教案{lessonDetail.lesson.status === 'draft' ? ' · 试学版' : ''}</div>
                  <h2>{lessonDetail.lesson.title}</h2>
                  <p>{lessonDetail.lesson.objective}</p>
                </header>
                <div className="learning-lesson-body">
                  {(() => {
                    const content = studentText(lessonDetail.lesson.content).replace(/^#\s+[^\n]*\n+/, '');
                    const parts = splitLessonContent(content);
                    return (
                      <>
                        <ChatRichContent content={parts.main} />
                        {parts.source && (
                          <details className="learning-supplement">
                            <summary>查看教材原文</summary>
                            <ChatRichContent content={parts.source} />
                          </details>
                        )}
                      </>
                    );
                  })()}
                </div>
                {(() => {
                  const refs = parseJson<Array<{ source_name?: string; source_type?: string; excerpt?: string; material_id?: string; line_start?: number; line_end?: number }>>(
                    lessonDetail.lesson.source_refs_json, [],
                  );
                  if (!refs.length) return null;
                  return (
                    <details className="learning-source">
                      <summary><FileText size={14} /> 内容来源</summary>
                      {refs.map((ref, index) => (
                        <div key={`${ref.material_id || ref.source_name}-${index}`} className="learning-source-item">
                          <span>
                            {SOURCE_TYPE_LABEL[ref.source_type || ''] || '课程材料'}
                            {ref.source_name ? ` · ${ref.source_name}` : ''}
                            {typeof ref.line_start === 'number' ? ` · 第 ${ref.line_start}-${ref.line_end ?? ref.line_start} 行` : ''}
                          </span>
                          {ref.excerpt && <blockquote>{studentText(ref.excerpt)}</blockquote>}
                        </div>
                      ))}
                      {lessonDetail.source_blocks.length > 0 && (
                        <div className="learning-source-blocks">
                          {lessonDetail.source_blocks.slice(0, 6).map((block) => (
                            <div key={block.id}>
                              <span>{block.block_type === 'table' ? '表格' : block.block_type === 'formula' ? '公式' : block.block_type === 'image' ? '图片' : '原文'}</span>
                              <small>第 {block.line_start}-{block.line_end} 行{block.page ? ` · 第 ${block.page} 页` : ''}</small>
                              <p>{studentText(block.text).slice(0, 80)}</p>
                            </div>
                          ))}
                        </div>
                      )}
                    </details>
                  );
                })()}
                <footer className="learning-lesson-footer">
                  <div className="learning-lesson-progress">
                    {detail.chapters.some((chapter) => chapter.sections.some((section) => section.lesson?.id === lessonDetail.lesson.id && section.progress?.status === 'completed'))
                      ? <span className="learning-complete"><Check size={14} /> 本节已完成</span>
                      : <span>读完教案后，标记完成并去做练习。</span>}
                  </div>
                  <div className="learning-lesson-actions">
                    <button type="button" className="learning-btn" onClick={() => void openPractice(lessonDetail.lesson.course_id, lessonDetail.lesson.id, 'in_class')}>
                      <Play size={14} /> 课上练习
                    </button>
                    <button type="button" className="learning-btn primary" disabled={completing} onClick={() => void markCompleted()}>
                      {completing ? <Loader2 size={14} className="spin" /> : <Check size={14} />} 完成本节
                    </button>
                    <button
                      type="button"
                      className="learning-btn ghost"
                      disabled={!detail.chapters.some((chapter) => chapter.sections.some((section) => section.lesson?.id === lessonDetail.lesson.id && section.progress?.status === 'completed'))}
                      onClick={() => void openPractice(lessonDetail.lesson.course_id, lessonDetail.lesson.id, 'post_class')}
                    >
                      <Lock size={13} /> 课后巩固
                    </button>
                  </div>
                </footer>
              </>
            )}
          </article>
        </div>
      </div>
    );
  }

  function renderCourses() {
    if (detailLoading) {
      return <div className="learning-detail-loading"><Loader2 className="spin" /> 正在加载课程…</div>;
    }
    if (detailError) {
      return <div className="learning-error">{detailError}<button type="button" onClick={() => void openCourse(selectedCourseId)}>重试</button></div>;
    }
    return detail ? renderCourseDetail() : renderCourseList();
  }

  function renderMaterials() {
    return (
      <>
        <header className="learning-section-head">
          <div>
            <span className="learning-eyebrow">添加材料</span>
            <h3>教材管理</h3>
            <p>上传教材目录，或直接描述你想学习的内容。</p>
          </div>
        </header>

        <div className="learning-materials-layout">
          <form className="learning-material-form" onSubmit={(event) => void submitCourse(event)}>
            <div className="learning-form-block">
              <label className="learning-field">
                <span>课程名称</span>
                <input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder={form.source_type === 'textbook' ? '例如：七年级数学上册' : form.source_type === 'curriculum' ? '例如：义务教育数学课程标准' : '例如：我的函数补强计划'} />
              </label>
              <div className="learning-field-row">
                <label className="learning-field">
                  <span>学科</span>
                  <input value={form.subject} onChange={(event) => setForm({ ...form, subject: event.target.value })} />
                </label>
                <label className="learning-field">
                  <span>年级</span>
                  <input value={form.grade} onChange={(event) => setForm({ ...form, grade: event.target.value })} />
                </label>
              </div>
            </div>

            <div className="learning-form-block">
              <span className="learning-block-label">材料类型</span>
              <div className="learning-source-types">
                <button type="button" className={form.source_type === 'textbook' ? 'learning-source-type active' : 'learning-source-type'} onClick={() => { setFile(null); setForm({ ...form, source_type: 'textbook', source_text: '' }); }}>
                  <strong>教材</strong><small>上传教材，按章整理</small>
                </button>
                <button type="button" className={form.source_type === 'curriculum' ? 'learning-source-type active' : 'learning-source-type'} onClick={() => { setFile(null); setForm({ ...form, source_type: 'curriculum', source_text: '' }); }}>
                  <strong>课标大纲</strong><small>粘贴大纲，按纲规划</small>
                </button>
                <button type="button" className={form.source_type === 'description' ? 'learning-source-type active' : 'learning-source-type'} onClick={() => { setFile(null); setForm({ ...form, source_type: 'description', source_text: '' }); }}>
                  <strong>自主描述</strong><small>只写目标，不用文件</small>
                </button>
              </div>
            </div>

            <div className="learning-form-block">
              {form.source_type === 'textbook' && (
                <>
                  <label className="learning-dropzone">
                    <input
                      type="file"
                      accept=".txt,.md,.markdown,.json,.csv,.pdf,.docx,.pptx,.xlsx,.xlsm,text/plain,text/markdown,application/json,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                      onChange={(event) => setFile(event.target.files?.[0] || null)}
                    />
                    <Upload size={18} />
                    <span>{file ? file.name : '上传教材文件'}</span>
                    <small>{file ? `${Math.max(1, Math.round(file.size / 1024))} KB · 点击更换` : '按教材章节目录整理课程'}</small>
                  </label>
                  <label className="learning-field">
                    <span>教材目录 <em>没有电子文件时填写</em></span>
                    <textarea
                      rows={5}
                      value={form.source_text}
                      onChange={(event) => setForm({ ...form, source_text: event.target.value })}
                      placeholder={'第一章 有理数\n1.1 正数和负数\n1.2 有理数的加减法\n……'}
                    />
                  </label>
                </>
              )}

              {form.source_type === 'curriculum' && (
                <>
                  <label className="learning-field">
                    <span>课标 / 教学大纲内容</span>
                    <textarea
                      rows={9}
                      value={form.source_text}
                      onChange={(event) => setForm({ ...form, source_text: event.target.value })}
                      placeholder={'粘贴课标或教学大纲中的章节、主题与学习要求，例如：\n第一章 数与代数\n1. 理解有理数的意义\n2. 掌握有理数的运算\n……'}
                    />
                  </label>
                  <p className="learning-field-hint">课程目录会按你粘贴的大纲整理，不需要上传文件。</p>
                </>
              )}

              {form.source_type === 'description' && (
                <>
                  <label className="learning-field">
                    <span>描述你想学习的内容</span>
                    <textarea
                      rows={8}
                      value={form.source_text}
                      onChange={(event) => setForm({ ...form, source_text: event.target.value })}
                      placeholder={'例如：我数学函数部分比较薄弱，想先理解函数的概念和图像，再练习定义域和值域，最后做几道综合题。'}
                    />
                  </label>
                  <div className="learning-description-hint">
                    <strong>可以这样写</strong>
                    <span>想学什么 · 哪里薄弱 · 希望先学哪一部分 · 想达到什么程度</span>
                  </div>
                </>
              )}
            </div>

            {formError && <div className="learning-error">{formError}</div>}
            <button type="submit" className="learning-btn primary learning-submit" disabled={submitting}>
              {submitting ? <Loader2 size={15} className="spin" /> : <Plus size={15} />}
              {form.source_type === 'textbook' ? '生成教材课程' : form.source_type === 'curriculum' ? '按大纲生成课程' : '生成学习计划'}
            </button>
          </form>

          <aside className="learning-material-side">
            <header><h4>我的材料</h4><span>{courses.length}</span></header>
            {coursesLoading ? <div className="learning-side-loading"><Loader2 className="spin" /> 加载中</div> : courses.length === 0 ? (
              <div className="learning-side-empty"><FileText size={22} /><p>还没有上传材料</p></div>
            ) : (
              <ul className="learning-material-list">
                {courses.map((course) => (
                  <li key={course.id}>
                    <div className={`learning-material-icon status-${course.status}`}><FileText size={16} /></div>
                    <div className="learning-material-info">
                      <strong>{course.title}</strong>
                      <span>{SOURCE_TYPE_LABEL[course.source_type]} · {formatTime(course.created_at)}</span>
                      <span className={`learning-status-pill status-${course.status}`}>{STATUS_LABEL[course.status]}</span>
                    </div>
                    <div className="learning-material-actions">
                      {course.status === 'failed' && <button type="button" className="learning-link" onClick={() => void retryCourse(course.id)}>重试</button>}
                      {course.status === 'ready' && <button type="button" className="learning-icon-btn" title="进入课程" onClick={() => { setTab('courses'); void openCourse(course.id); }}><ArrowRight size={15} /></button>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </aside>
        </div>
      </>
    );
  }

  function renderPractice() {
    return (
      <>
        <header className="learning-section-head">
          <div>
            <span className="learning-eyebrow">巩固练习</span>
            <h3>练习中心</h3>
            <p>先完成课上练习，再用课后练习巩固。</p>
          </div>
        </header>

        <div className="learning-practice-toolbar">
          <label className="learning-select">
            <span>课程</span>
            <select
              value={practiceCourseId}
              onChange={(event) => {
                const id = event.target.value;
                setPracticeCourseId(id);
                setPracticeLessonId('');
                setPracticeDetail(null);
                if (id) void openPractice(id);
              }}
            >
              <option value="">选择课程</option>
              {readyCourses.map((course) => <option key={course.id} value={course.id}>{course.title}</option>)}
            </select>
          </label>
          <label className="learning-select">
            <span>小节</span>
            <select value={practiceLessonId} onChange={(event) => setPracticeLessonId(event.target.value)} disabled={!practiceDetail}>
              <option value="">全部小节</option>
              {sections.map(({ chapterTitle, section }) => (
                <option key={section.id} value={section.lesson?.id || ''}>{chapterTitle} · {section.title}</option>
              ))}
            </select>
          </label>
          <div className="learning-phase-switch">
            <button type="button" className={practicePhase === 'in_class' ? 'active' : ''} onClick={() => setPracticePhase('in_class')}>课上</button>
            <button type="button" className={practicePhase === 'post_class' ? 'active' : ''} onClick={() => setPracticePhase('post_class')}>课后</button>
          </div>
        </div>

        {!practiceCourseId && (
          <div className="learning-practice-empty">
            <ListChecks size={28} />
            <h4>选择一门课程开始练习</h4>
            <p>课上练习随学随做，完成小节后开启课后巩固。</p>
          </div>
        )}
        {practiceError && <div className="learning-error">{practiceError}<button type="button" onClick={() => void openPractice(practiceCourseId)}>重试</button></div>}
        {practiceLoading && <div className="learning-side-loading"><Loader2 className="spin" /> 正在准备练习…</div>}
        {!practiceLoading && practiceCourseId && exercises.length === 0 && !practiceError && (
          <div className="learning-practice-empty"><BookOpen size={24} /><p>这个范围还没有练习。</p></div>
        )}
        <div className="learning-exercise-list">
          {exercises.map((exercise, index) => {
            const message = attemptMessages[exercise.id];
            return (
              <article className={exercise.locked ? 'learning-exercise locked' : 'learning-exercise'} key={exercise.id}>
                <header>
                  <span className="learning-exercise-no">{String(index + 1).padStart(2, '0')}</span>
                  <div className="learning-exercise-badges">
                    <span className={`learning-tag ${exercise.phase}`}>{exercise.phase === 'in_class' ? '课上' : '课后'}</span>
                    <span className="learning-tag">{exercise.source_type === 'textbook' ? '教材' : '拓展'}</span>
                    {exercise.validation_status === 'placeholder' && <span className="learning-tag example">示例</span>}
                  </div>
                </header>
                <p className="learning-exercise-stem">{studentText(exercise.stem)}</p>
                {exercise.locked ? (
                  <div className="learning-locked"><Lock size={14} /> 完成这一节后解锁课后练习</div>
                ) : (
                  <>
                    <textarea
                      rows={3}
                      value={answers[exercise.id] || ''}
                      onChange={(event) => setAnswers((previous) => ({ ...previous, [exercise.id]: event.target.value }))}
                      placeholder="写下你的答案或解题过程"
                    />
                    <div className="learning-exercise-foot">
                      <button type="button" className="learning-btn primary" disabled={submittingExercise === exercise.id || !(answers[exercise.id] || '').trim()} onClick={() => void submitExercise(exercise)}>
                        {submittingExercise === exercise.id ? <Loader2 size={14} className="spin" /> : <Check size={14} />} 提交答案
                      </button>
                      {message && <span className="learning-attempt-message">{message}</span>}
                    </div>
                  </>
                )}
              </article>
            );
          })}
        </div>
      </>
    );
  }

  const currentTab = TABS.find((item) => item.key === tab) || TABS[0];
  const CurrentIcon = currentTab.icon;

  return (
    <div className="view-inner learning-page">
      <div className="learning-title-row">
        <div>
          <span className="learning-eyebrow">学习空间</span>
          <h2>学习中心</h2>
        </div>
        <button type="button" className="learning-icon-btn" title="刷新" onClick={() => void loadCourses()} disabled={coursesLoading}>
          <RefreshCw size={16} className={coursesLoading ? 'spin' : ''} />
        </button>
      </div>

      <div className="settings-shell">
        <nav className="panel settings-nav learning-nav" aria-label="学习中心目录">
          {TABS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                type="button"
                key={item.key}
                className={tab === item.key ? 'settings-nav-item learning-nav-item active' : 'settings-nav-item learning-nav-item'}
                onClick={() => setTab(item.key)}
              >
                <Icon size={16} />
                <span>{item.label}</span>
                {item.key === 'practice' && exercises.length > 0 && tab === 'practice' && <em>{exercises.length}</em>}
              </button>
            );
          })}
          <div className="learning-nav-foot">
            <CurrentIcon size={15} />
            <p>{currentTab.hint}</p>
          </div>
        </nav>

        <div className="settings-content">
          <section className="panel learning-panel">
            {notice && <div className="learning-notice"><Check size={14} /> {notice}</div>}
            {tab === 'courses' && renderCourses()}
            {tab === 'materials' && renderMaterials()}
            {tab === 'practice' && renderPractice()}
          </section>
        </div>
      </div>
    </div>
  );
}

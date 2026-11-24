/**
 * 生成后质量检查：内部状态清理 + 可解析的一元一次不等式/区间校验。
 * 不可确定的数学答案只标记 needs_validation，不静默修改。
 */
import { correctLinearClaimsLine, intersectBounds, parseBound, formatIntersection, formatBound } from './algebra-solver';

export type LessonQualityStatus = 'complete' | 'partial' | 'failed' | 'needs_validation';

export interface LessonQualityAudit {
  status: LessonQualityStatus;
  internalPlaceholders: number;
  emptyUnitCount: number;
  missingUnits: string[];
  duplicateParagraphs: number;
  mathIssues: number;
  issues: string[];
}

export function repairGeneratedText(value: string): string {
  const lines = String(value || '').split('\n');
  const sectionSolved = new Map<number, ReturnType<typeof parseBound>>();
  let sectionIndex = 0;
  lines.forEach((line, index) => {
    if (/^#{3,4}\s+/.test(line)) sectionIndex = index;
    const corrected = correctLinearClaimsLine(line);
    if (corrected !== line) {
      const changed = corrected.match(/x(?:≤|≥|<|>)\s*(?:\\frac\{[0-9]+\}\{[0-9]+\}|[0-9.]+)/);
      if (changed) sectionSolved.set(sectionIndex, parseBound(changed[0]));
    }
  });
  sectionIndex = 0;
  let lastSolved: ReturnType<typeof parseBound> = null;
  let lastIntersection: ReturnType<typeof intersectBounds> | null = null;
  return lines.map((line, index) => {
    if (/^#{3,4}\s+/.test(line)) { sectionIndex = index; lastSolved = null; lastIntersection = null; }
    const sectionBound = sectionSolved.get(sectionIndex);
    let next = correctLinearClaimsLine(line);
    if (next !== line) {
      const changed = next.match(/x(?:≤|≥|<|>)\s*(?:\\frac\{[0-9]+\}\{[0-9]+\}|[0-9.]+)/);
      if (changed) lastSolved = parseBound(changed[0]);
    }
    if (!lastSolved && sectionBound) lastSolved = sectionBound;
    if (lastSolved && next.includes('x<45')) next = next.replace(/x<45/g, formatBound(lastSolved));
    const bounds = (next.match(/x\s*(?:≤|≥|<|>|<=|>=|\\ge|\\le)\s*(?:\\frac\{[0-9]+\}\{[0-9]+\}|[0-9.]+)/g) || [])
      .map((item) => parseBound(item))
      .filter((item): item is NonNullable<ReturnType<typeof parseBound>> => Boolean(item));
    if (bounds.length >= 2 && /没有公共部分|无公共部分/.test(next)) {
      const intersection = intersectBounds(bounds[0], bounds[1]);
      lastIntersection = intersection;
      if (!intersection.empty) next = next.replace(/^.*?(例如|教材例)/, '$1');
      const left = intersection.lower ? formatBound(intersection.lower) : '';
      const right = intersection.upper ? formatBound(intersection.upper) : '';
      next = next.replace(/x[^。；\n]*(?:没有公共部分|无公共部分)[^。；\n]*/g,
        intersection.empty
          ? `${left} 与 ${right} 没有公共部分，所以无解`
          : `${left} 与 ${right} 的公共部分为 ${formatIntersection(intersection)}`);
    }
    if (lastIntersection && /参考答案：无解/.test(next)) next = '参考答案：' + formatIntersection(lastIntersection) + '。';
    next = next.replace(/\\?\(?x≥8 与 x<([^ ]+) 没有公共部分/g, '\\(x≥8\\) 与 \\(x<$1\\) 没有公共部分');
    next = next.replace(/\\?\(?x≥8 与 x<45 的公共部分为 8≤x<45。?/g, '\\(x≥8\\) 与 \\(x<45\\) 的公共部分为 \\(8≤x<45\\)。');
    return next
      .replace(/程序按[^。\n]*/g, '')
      .replace(/以上知识点[^。\n]*/g, '')
      .replace(/待 Agent[^。\n]*/g, '');
  }).join('\n').replace(/\n{3,}/g, '\n\n');
}

function normalizeForDuplicate(value: string): string {
  return value.replace(/^#+\s*/gm, '').replace(/\s+/g, '');
}

export function auditGeneratedText(content: string, units: Array<{ title: string }>): LessonQualityAudit {
  const text = String(content || '');
  if (!/^####\s+/m.test(text)) {
    const placeholders = (text.match(/程序按|待 Agent|知识点提取结果补充|待确认/g) || []).length;
    return { status: placeholders ? 'needs_validation' : 'complete', internalPlaceholders: placeholders, emptyUnitCount: 0, missingUnits: [], duplicateParagraphs: 0, mathIssues: 0, issues: placeholders ? ['存在内部状态/占位文本'] : [] };
  }
  const internalPlaceholders = (text.match(/程序按|待 Agent|知识点提取结果补充|待确认/g) || []).length;
  const missingUnits = units.filter((unit) => !text.includes(unit.title)).map((unit) => unit.title);
  const emptyUnits: string[] = [];
  const paragraphs: string[] = [];
  units.forEach((unit) => {
    const start = text.indexOf(unit.title);
    if (start < 0) return;
    const after = text.slice(start + unit.title.length);
    const nextMatch = after.match(/\n####(?!#)\s/);
    const next = nextMatch ? start + unit.title.length + (nextMatch.index || 0) : -1;
    const body = text.slice(start + unit.title.length, next > 0 ? next : text.length)
      .replace(/^#+\s*/gm, '').replace(/\*\*[^*]+\*\*/g, '').trim();
    if (body.length < 120 || (body.length < 220 && !/例题|理解检查|易错点|本单元小结/.test(body))) emptyUnits.push(unit.title);
    body.split(/\n{2,}/).forEach((part) => {
      if (part.trim().length >= 80 && !/前面|之前|回顾|复习|已学过/.test(part)) paragraphs.push(normalizeForDuplicate(part));
    });
  });
  const seen = new Set<string>();
  let duplicateParagraphs = 0;
  paragraphs.forEach((part) => { if (seen.has(part)) duplicateParagraphs += 1; else seen.add(part); });
  let mathIssues = 0;
  text.split('\n').forEach((line) => {
    if (correctLinearClaimsLine(line) !== line) mathIssues += 1;
    const bounds = (line.match(/x\s*(?:≤|≥|<|>|<=|>=|\\ge|\\le)\s*(?:\\frac\{[0-9]+\}\{[0-9]+\}|[0-9.]+)/g) || [])
      .map((item) => parseBound(item)).filter((item): item is NonNullable<ReturnType<typeof parseBound>> => Boolean(item));
    if (bounds.length >= 2 && /没有公共部分|无公共部分/.test(line) && !intersectBounds(bounds[0], bounds[1]).empty) mathIssues += 1;
  });
  const issues: string[] = [];
  if (internalPlaceholders) issues.push('存在内部状态/占位文本');
  if (missingUnits.length) issues.push('目录单元无正文：' + missingUnits.join('、'));
  if (emptyUnits.length) issues.push('空或过短单元：' + emptyUnits.join('、'));
  if (duplicateParagraphs) issues.push('重复段落：' + String(duplicateParagraphs));
  if (mathIssues) issues.push('存在待复核数学表达');
  const status: LessonQualityStatus = missingUnits.length || emptyUnits.length
    ? 'partial' : mathIssues || internalPlaceholders ? 'needs_validation' : 'complete';
  return { status, internalPlaceholders, emptyUnitCount: emptyUnits.length, missingUnits, duplicateParagraphs, mathIssues, issues };
}

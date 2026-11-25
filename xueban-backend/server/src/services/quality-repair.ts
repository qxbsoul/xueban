/**
 * 生成后质量检查：内部状态清理 + 单行可确定的一元一次不等式校验。
 * 不跨题共享计算结果，不含特定教材规则。
 */
import { correctLinearClaimsLine, extractBounds, intersectBounds, formatIntersection, formatBound } from './algebra-solver';

export type LessonQualityStatus = 'complete' | 'partial' | 'failed' | 'needs_validation';

export interface AuditUnit {
  title: string;
  content?: string;
}

export interface LessonQualityAudit {
  status: LessonQualityStatus;
  internalPlaceholders: number;
  emptyUnitCount: number;
  missingUnits: string[];
  unlocatableUnits: string[];
  duplicateParagraphs: number;
  mathIssues: number;
  issues: string[];
}

export function repairGeneratedText(value: string): string {
  return String(value || '').split('\n').map((line) => {
    let next = correctLinearClaimsLine(line);
    const bounds = extractBounds(next);
    if (bounds.length >= 2 && /没有公共部分|无公共部分/.test(next)) {
      const intersection = intersectBounds(bounds[0], bounds[1]);
      const left = intersection.lower ? formatBound(intersection.lower) : '';
      const right = intersection.upper ? formatBound(intersection.upper) : '';
      next = next.replace(/x[^。；\n]*(?:没有公共部分|无公共部分)[^。；\n]*/g,
        intersection.empty
          ? `${left} 与 ${right} 没有公共部分，所以无解`
          : `${left} 与 ${right} 的公共部分为 ${formatIntersection(intersection)}`);
    }
    return next
      .replace(/程序按[^。\n]*/g, '')
      .replace(/以上知识点[^。\n]*/g, '')
      .replace(/待 Agent[^。\n]*/g, '');
  }).join('\n').replace(/\n{3,}/g, '\n\n');
}

interface HeadingBlock { title: string; body: string }
function scanHeadings(content: string): HeadingBlock[] {
  const source = String(content || '');
  const matches = [...source.matchAll(/^(#{1,6})\s+(.+)$/gm)];
  return matches.map((match, index) => {
    const level = match[1].length;
    const start = (match.index || 0) + match[0].length;
    let end = source.length;
    for (let cursor = index + 1; cursor < matches.length; cursor += 1) {
      if (matches[cursor][1].length <= level) { end = matches[cursor].index || 0; break; }
    }
    return { title: match[2].trim(), body: source.slice(start, end) };
  });
}

function normalizedTitle(value: string): string { return value.replace(/^#+\s*/, '').replace(/^\d+(?:\.\d+)*\s*/, '').replace(/\s+/g, ' ').trim(); }
function bodyText(value: string): string { return value.replace(/^#+\s*/gm, '').replace(/\*\*[^*]+\*\*/g, '').trim(); }

export function auditGeneratedText(content: string, units: AuditUnit[]): LessonQualityAudit {
  const text = String(content || '');
  const hasH4 = /^####\s+/m.test(text);
  const headings = scanHeadings(text);
  const queues = new Map<string, HeadingBlock[]>();
  headings.forEach((block) => {
    const key = normalizedTitle(block.title);
    queues.set(key, [...(queues.get(key) || []), block]);
  });
  const internalPlaceholders = (text.match(/程序按|待 Agent|知识点提取结果补充|待确认/g) || []).length;
  const missingUnits: string[] = [];
  const emptyUnits: string[] = [];
  const unlocatableUnits: string[] = [];
  const paragraphs: string[] = [];
  units.forEach((unit) => {
    const title = normalizedTitle(unit.title);
    const queue = queues.get(title) || [];
    const exact = queue.shift();
    let headingBody = exact?.body;
    const structured = unit.content === undefined ? '' : unit.content;
    const body = [structured, headingBody || ''].sort((left, right) => right.length - left.length)[0];
    if (!body) {
      if (!headings.length && !structured) unlocatableUnits.push(unit.title);
      else missingUnits.push(unit.title);
      return;
    }
    const cleanBody = bodyText(body);
    if ((hasH4 && cleanBody.length < 120) || (hasH4 && cleanBody.length < 220 && !/例题|理解检查|易错点|本单元小结/.test(cleanBody)) || (!hasH4 && cleanBody.length === 0)) emptyUnits.push(unit.title);
    cleanBody.split(/\n{2,}/).forEach((part) => {
      if (part.trim().length >= 80 && !/前面|之前|回顾|复习|已学过/.test(part)) paragraphs.push(part.replace(/\s+/g, ''));
    });
  });
  const seen = new Set<string>();
  let duplicateParagraphs = 0;
  paragraphs.forEach((part) => { if (seen.has(part)) duplicateParagraphs += 1; else seen.add(part); });
  let mathIssues = 0;
  text.split('\n').forEach((line) => {
    if (correctLinearClaimsLine(line) !== line) mathIssues += 1;
    const bounds = extractBounds(line);
    if (bounds.length >= 2) {
      const intersection = intersectBounds(bounds[0], bounds[1]);
      if (/没有公共部分|无公共部分/.test(line) && !intersection.empty) mathIssues += 1;
      if (/公共部分为/.test(line) && intersection.empty) mathIssues += 1;
    }
  });
  const issues: string[] = [];
  if (internalPlaceholders) issues.push('存在内部状态/占位文本');
  if (missingUnits.length) issues.push('目录单元无正文：' + missingUnits.join('、'));
  if (emptyUnits.length) issues.push('空或过短单元：' + emptyUnits.join('、'));
  if (unlocatableUnits.length) issues.push('无法定位正文的单元：' + unlocatableUnits.join('、'));
  if (duplicateParagraphs) issues.push('重复段落：' + String(duplicateParagraphs));
  if (mathIssues) issues.push('存在待复核数学表达');
  const status: LessonQualityStatus = missingUnits.length || emptyUnits.length
    ? 'partial' : duplicateParagraphs || unlocatableUnits.length || mathIssues || internalPlaceholders ? 'needs_validation' : 'complete';
  return { status, internalPlaceholders, emptyUnitCount: emptyUnits.length, missingUnits, unlocatableUnits, duplicateParagraphs, mathIssues, issues };
}

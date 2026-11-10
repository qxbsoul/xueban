/**
 * intent-router.test.ts — standalone smoke test for classifyTS() + classifyIntent().
 *
 * Run with:  npx tsx src/lib/__tests__/intent-router.test.ts   (from server/)
 *
 * Mirrors the classify-error.test.ts / assembler.test.ts no-framework pattern
 * (vitest not installed in the server package yet). Covers at minimum:
 *   - 3× chat (answer-keyword)
 *   - 3× clarify (empty / too short)
 *   - 3× task (workflow / review / report)
 * Plus the priority-order invariant from the Python source
 * (workflow > review > report > answer) and the default fallback.
 */

import {
  classifyTS,
  classifyIntent,
  detectExplicitSingleAgent,
  ANSWER_KW,
  REPORT_KW,
  REVIEW_KW,
  WORKFLOW_KW,
} from '../intent-router';

let pass = 0;
let fail = 0;

function check(label: string, expected: unknown, actual: unknown) {
  const eq = JSON.stringify(expected) === JSON.stringify(actual);
  if (eq) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`);
  }
}

function checkKind(label: string, expectedKind: string, goal: string) {
  const r = classifyTS(goal);
  check(label, expectedKind, r.kind);
}

// ── chat (answer-keyword → kind=chat) ──────────────────────────────────────
checkKind('chat #1 "什么是 RAG" → chat',          'chat', '什么是 RAG');
checkKind('chat #2 "how to deploy ACP" → chat',  'chat', 'how to deploy ACP');
checkKind('chat #3 "解释一下注意力机制" → chat',   'chat', '解释一下注意力机制');

// ── clarify (empty / too short → kind=clarify) ────────────────────────────
checkKind('clarify #1 empty string → clarify',   'clarify', '');
checkKind('clarify #2 whitespace only → clarify','clarify', '   ');
checkKind('clarify #3 too short "hi" → clarify', 'clarify', 'hi');

// ── task (workflow / review / report → kind=task) ─────────────────────────
checkKind('task #1 "搭一个数据同步工作流" → task',          'task', '搭一个数据同步工作流');
checkKind('task #2 "帮我 code review 这段代码" → task',    'task', '帮我 code review 这段代码');
checkKind('task #3 "调研一下竞品分析" → task',             'task', '调研一下竞品分析');

// ── Priority invariant: workflow beats review beats report beats answer ──
{
  // A goal containing BOTH "工作流" (workflow) and "什么是" (answer):
  //   Python loops in order workflow → review → report → answer,
  //   so workflow must win.
  const r = classifyIntent('什么是工作流');
  check('priority: workflow beats answer → outputType=workflow', 'workflow', r.outputType);
  check('priority: workflow beats answer → confidence=0.88',     0.88,        r.confidence);
}
{
  // "审查代码 report" — review keyword comes before report in the loop,
  // so review must win even when report keyword is also present.
  const r = classifyIntent('请审查这份 report');
  check('priority: review beats report → outputType=review', 'review', r.outputType);
}

// ── outputHint override (mirrors Python: hint forces confidence=1.0) ──────
{
  const r = classifyIntent('随便说点什么', 'workflow');
  check('hint=workflow forces outputType', 'workflow', r.outputType);
  check('hint=workflow forces mode=team',  'team',     r.mode);
  check('hint=workflow forces confidence=1.0', 1.0,    r.confidence);
}
{
  const r = classifyIntent('随便说点什么', 'answer');
  check('hint=answer → mode=single', 'single', r.mode);
}
{
  // Invalid hint must be ignored and we fall back to keyword/default
  const r = classifyIntent('完全没有关键词的句子内容', 'garbage');
  check('invalid hint ignored → default report fallback', 'report', r.outputType);
  check('invalid hint ignored → default confidence=0.75', 0.75,     r.confidence);
}

// ── Default fallback (no keyword hits) ────────────────────────────────────
{
  const r = classifyIntent('完全没有任何匹配词的随机句子内容');
  check('no-keyword default → outputType=report', 'report', r.outputType);
  check('no-keyword default → mode=team',         'team',   r.mode);
  check('no-keyword default → confidence=0.75',   0.75,     r.confidence);
  check('no-keyword default → complexity=3',      3,        r.complexity);
}

// ── classifyTS reasons surface useful info ────────────────────────────────
{
  const r = classifyTS('搭一个 pipeline');
  check('classifyTS task confidence preserved (0.88)', 0.88, r.confidence);
  if (!r.reasons.some((s) => s.startsWith('keyword:'))) {
    fail++;
    console.log(`  FAIL  classifyTS reasons should include a "keyword:..." entry, got ${JSON.stringify(r.reasons)}`);
  } else {
    pass++;
    console.log('  PASS  classifyTS task reasons include "keyword:..."');
  }
}
{
  const r = classifyTS('完全没有任何匹配词的随机句子内容');
  if (!r.reasons.includes('fallback:default_report_team')) {
    fail++;
    console.log(`  FAIL  classifyTS fallback reason missing, got ${JSON.stringify(r.reasons)}`);
  } else {
    pass++;
    console.log('  PASS  classifyTS fallback reason "fallback:default_report_team" present');
  }
}

// ── Keyword tables are exactly the Python lists (count + order spot-check) ─
check('ANSWER_KW length matches Python',   9, ANSWER_KW.length);
check('REPORT_KW length matches Python',   8, REPORT_KW.length);
check('REVIEW_KW length matches Python',   8, REVIEW_KW.length);
check('WORKFLOW_KW length matches Python', 7, WORKFLOW_KW.length);
check('WORKFLOW_KW[0] === 工作流', '工作流', WORKFLOW_KW[0]);
check('ANSWER_KW[0]   === 什么是', '什么是', ANSWER_KW[0]);

// ── detectExplicitSingleAgent (2026-05-27 regression: "一个却来三个") ────────
// The exact bug report goal MUST be detected as single-agent intent.
check('single #1 bug-report goal → single=true',
  true, detectExplicitSingleAgent('帮我创建一个开发工程师agent').single);
check('single #2 "单个 agent" → single=true',
  true, detectExplicitSingleAgent('给我一个单个 agent 就行').single);
check('single #3 "create a single agent" → single=true',
  true, detectExplicitSingleAgent('create a single agent for code review').single);
check('single #4 "one agent" → single=true',
  true, detectExplicitSingleAgent('I just need one agent').single);
check('single #5 "一名助手" → single=true',
  true, detectExplicitSingleAgent('帮我做一名客服助手').single);

// Team / plural intent MUST NOT be forced to single.
check('single #6 "团队" overrides singular phrase → single=false',
  false, detectExplicitSingleAgent('创建一个开发团队').single);
check('single #7 explicit team request → single=false',
  false, detectExplicitSingleAgent('帮我搭一个 review 团队，要 3 个 agent').single);
check('single #8 plain team goal → single=false',
  false, detectExplicitSingleAgent('搭一个论文评审 team').single);
check('single #9 no quantity phrase → single=false',
  false, detectExplicitSingleAgent('帮我写个 agent 做代码审查').single);
check('single #10 empty goal → single=false',
  false, detectExplicitSingleAgent('').single);

// ── Summary ───────────────────────────────────────────────────────────────
console.log(`\n${pass} pass / ${fail} fail`);
if (fail > 0) {
  process.exit(1);
}

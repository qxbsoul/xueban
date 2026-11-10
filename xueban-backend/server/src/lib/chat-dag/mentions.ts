/**
 * mentions.ts — @ 提及定向派活解析（2026-11-05，OpenBot「直接点名 = 定向 work assignment」语义）。
 *
 * 群聊里用户直接 @ 某 agent 是最明确的派活意图：无需 LLM 意图拆解，消息只路由给被
 * 点名的成员（DAG 路径收缩为被提及节点诱导子图；串行路径只派发被提及者）。本模块
 * 全纯函数，方便单测：
 *   - parseMentions        — 从消息文本提取 @token，对照候选成员匹配，返回命中 id + 清洗文本；
 *   - restrictTeamToMentions — 计算 DAG 子图（members 按 team.members 原序过滤、edges 两端均在子集内）。
 *
 * 分词规则（菜单插入恒带尾随空格；手打场景按下列规则，覆盖中文无空格直连）：
 *   - '@' 须在文首、空白/中西文标点后（a@b 邮件式文本不误命中），或紧贴前一个
 *     @表达式的尾（「@A@B」粘连双点名）；'@@x' 两个 @ 都不是表达式尾 → 不误命中；
 *   - ⓪ verbatim 名/id 原文最长直配（菜单插入=name 原文；中文任务常无空格直接
 *     相连「@码农鸡修一下」也能命中）；尾随 ASCII 词字符视为名字未打完不截断
 *     （@dev01x ≠ @dev01）；同长歧义 → 落回 ①；
 *   - ① token = '@' 后连续「非空白非标点」串；命中优先级：精确 name → 精确 id →
 *     忽略大小写精确 → 唯一前缀（长名敲一半）；歧义或全不命中 → 当普通文本忽略；
 *   - 命中尾部标点（手打「@读读，」）剥离后再删（不悬挂标点）。
 */
import type { TeamRunShape } from '../team-source';

export interface MentionCandidate {
  id: string;
  name: string;
}

export interface MentionParseResult {
  /** 命中的成员 id（按出现顺序去重）。 */
  mentionedIds: string[];
  /** 删除 @token 后的清洗文本（多余空白折叠、trim）。 */
  cleanText: string;
}

/** '@' 触发边界：文首、空白/中西文标点后（中文手打「@阿Dev，@小Q。」不带空格）。 */
const BOUNDARY_RE = /[\s，。！？、：；）】」』》〉【「『,.!?;:)\]]/;
/** token = 连续「非空白非标点非 @」串（粘连场景的兜底分词）。 */
const TOKEN_RE = /^[^\s，。！？、：；）】」』》〉【「『,.!?;:)\]}"'`@]+/;
/** verbatim 命中后若紧跟 ASCII 词字符，视为名字未打完（@dev01x ≠ @dev01），不截断。 */
const ASCII_WORD_RE = /[A-Za-z0-9_]/;
/** token 后可吞掉的中西文标点（悬挂清理用）。 */
const PUNCT_TAIL_RE = /[，。！？、：；）】」』》〉【「『,.!?;:)\]}"'`]/;

/**
 * 解析一条消息里的 @ 提及。无 '@'、无候选或全不命中时返回零命中
 * （mentionedIds 为空，cleanText 原文透传）——调用方按「未提及」走现状路径。
 */
export function parseMentions(
  text: string,
  candidates: MentionCandidate[],
): MentionParseResult {
  if (!text || !text.includes('@') || candidates.length === 0) {
    return { mentionedIds: [], cleanText: text };
  }

  const mentioned: string[] = [];
  /** 命中 token 的删除区间 [start, end)（含 '@' 与 token，不含前导空白）。 */
  const spans: Array<[number, number]> = [];
  /** 最近一个 @表达式（无论是否命中）的 token 尾 index —— 粘连「@A@B」的触发边界。 */
  let lastExprEnd = -1;

  let i = text.indexOf('@');
  while (i !== -1) {
    // '@' 触发边界：文首 / 空白或标点后 / 紧贴前一表达式尾（粘连）。@@ 不误命中：
    // 首个 @ 后无 token 不成表达式，第二个 @ 既非文首也非表达式尾 → 双双跳过。
    const boundaryOk = i === 0 || BOUNDARY_RE.test(text[i - 1]) || i === lastExprEnd;
    if (!boundaryOk) {
      i = text.indexOf('@', i + 1);
      continue;
    }
    const rest = text.slice(i + 1);
    // ⓪ verbatim 名/id 原文最长直配（中文任务无空格直连主路径）；
    // ① 旧 token 分词兜底（「@Q 敲一半」唯一前缀等）。
    const vb = verbatimMatch(rest, candidates);
    const token = vb ? rest.slice(0, vb.len) : (TOKEN_RE.exec(rest)?.[0] ?? '');
    const hitId = vb ? vb.id : token ? matchCandidate(token, candidates) : null;
    if (token) lastExprEnd = i + 1 + token.length;
    if (hitId) {
      if (!mentioned.includes(hitId)) mentioned.push(hitId);
      // 删除区间向后吞掉紧随的标点（手打「@阿Dev，」/@Dev: — 不留悬挂标点）。
      let end = i + 1 + token.length;
      while (end < text.length && PUNCT_TAIL_RE.test(text[end])) end++;
      spans.push([i, end]);
    }
    i = text.indexOf('@', i + 1);
  }
  if (mentioned.length === 0) {
    return { mentionedIds: [], cleanText: text };
  }

  // 按区间倒序拼接清洗文本。
  let out = '';
  let pos = text.length;
  for (let j = spans.length - 1; j >= 0; j--) {
    const [s, e] = spans[j];
    out = text.slice(e, pos) + out;
    pos = s;
  }
  out = text.slice(0, pos) + out;
  out = out.replace(/[ \t]{2,}/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
  return { mentionedIds: mentioned, cleanText: out };
}

/**
 * verbatim 最长直配：候选 name/id 原文（忽略大小写）是 rest 的前缀即命中，
 * 取最长；同长不同 id 视为歧义返回 null（落回 token 路径）。尾随 ASCII 词字符
 * 的候选跳过（名字没打完/更长的别人名，交给最长匹配或 token 路径判）。
 */
function verbatimMatch(
  rest: string,
  candidates: MentionCandidate[],
): { id: string; len: number } | null {
  const lower = rest.toLowerCase();
  let bestLen = 0;
  let bestId: string | null = null;
  let ambiguous = false;
  for (const c of candidates) {
    for (const v of [c.name, c.id]) {
      if (!v) continue;
      const vl = v.toLowerCase();
      if (vl.length < bestLen || !lower.startsWith(vl)) continue;
      const nxt = rest[vl.length];
      if (nxt && ASCII_WORD_RE.test(nxt)) continue; // 名字未打完，不截断
      if (vl.length > bestLen) {
        bestLen = vl.length;
        bestId = c.id;
        ambiguous = false;
      } else if (c.id !== bestId) {
        ambiguous = true;
      }
    }
  }
  return ambiguous || !bestId ? null : { id: bestId, len: bestLen };
}

/** 单 token → 成员 id。四级匹配，歧义即弃（当普通文本）。 */
function matchCandidate(token: string, candidates: MentionCandidate[]): string | null {
  const exact =
    candidates.find((c) => c.name === token) ?? candidates.find((c) => c.id === token);
  if (exact) return exact.id;
  const lc = token.toLowerCase();
  const ci =
    candidates.find((c) => c.name.toLowerCase() === lc) ??
    candidates.find((c) => c.id.toLowerCase() === lc);
  if (ci) return ci.id;
  const pref = candidates.filter(
    (c) => c.name.toLowerCase().startsWith(lc) || c.id.toLowerCase().startsWith(lc),
  );
  return pref.length === 1 ? pref[0].id : null;
}

/**
 * 计算被提及节点的 DAG 诱导子图：
 *   - members 保留 team.members 原序（拓扑语义不变），只留 ids 子集；
 *   - edges 只留两端均在子集内的（conditional 返工边同样保留——@Dev @QA 定向
 *     组队时 QA 仍可评审 Dev）；跨子集边随节点裁剪自然消失；
 *   - policy_matrix 原样透传（gate 只按现存边查询，冗余行无副作用）。
 * ids 全不在 team.members 里 → members 为空，调用方据此回落现状路径。
 */
export function restrictTeamToMentions(team: TeamRunShape, ids: string[]): TeamRunShape {
  const idSet = new Set(ids);
  const members = team.members.filter((id) => idSet.has(id));
  const edges = team.edges.filter((e) => idSet.has(e.from) && idSet.has(e.to));
  return {
    members,
    edges,
    policy_matrix: team.policy_matrix,
    ...(team.name ? { name: team.name } : {}),
  };
}

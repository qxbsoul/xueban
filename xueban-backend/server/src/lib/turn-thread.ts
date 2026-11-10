/**
 * turn-thread.ts — Whole-thread 回放保真（2026-11-07，OpenBot「thread 丢段即
 * 测试红」契约）。
 *
 * 聊天派发路径把持久化线程组装成一次 LLM turn 的 (history, prompt) 时，此前
 * 用 `history.slice(0, -1)` 盲删末位来"剔除被提升为 prompt 的用户消息"。这只
 * 在末位恰好是那条用户消息时成立，两个实缺口：
 *
 *   1. 多 agent 串行轮（群/DM fan-out 第 2+ 个 agent）——每轮 reload 线程后
 *      末位是**上一个 agent 的回复**，被整条删掉：后发言者看不到先发言者说了
 *      什么（与「Reload each round so later agents see earlier replies」注释
 *      相反），而用户消息反而留在 history 里与 prompt 重复出现。
 *   2. 末位不是用户消息的任何线程（如无用户消息/异常触发）都会随机丢一段。
 *
 * 本模块把「提升用户轮 + 折叠相邻同角色」收成纯函数，保证三条契约（见
 * buildTurnThread）。Anthropic Messages API 要求 user/assistant 严格交替，两
 * 相邻同角色封包 → HTTP 422（见 anthropic-block-adapter.ts §toAnthropicMessages），
 * 故折叠不只是整洁性问题，是 provider 合法性问题。
 */

import type { ConversationMessage } from './conversation-types';

/** 从一条消息取全部 text block 的拼接（prompt 侧只消费文本段）。 */
export function textOf(message: ConversationMessage): string {
  return message.blocks
    .map((b) => (b.kind === 'text' ? b.text : ''))
    .join('');
}

/**
 * 折叠相邻同角色消息为一条，blocks 按原序拼接（每段仍是独立 block，段界不丢）。
 * usage 只在请求侧占位（适配器发线时丢弃），折叠取首条非空值。
 * 不改入参（返回新对象/新数组）。
 */
export function foldConsecutiveRoles(
  messages: ConversationMessage[],
): ConversationMessage[] {
  const out: ConversationMessage[] = [];
  for (const m of messages) {
    const last = out[out.length - 1];
    if (last && last.role === m.role) {
      const usage = last.usage ?? m.usage;
      out[out.length - 1] = {
        ...last,
        blocks: [...last.blocks, ...m.blocks],
        ...(usage ? { usage } : {}),
      };
    } else {
      out.push({ ...m, blocks: [...m.blocks] });
    }
  }
  return out;
}

export interface TurnThread {
  /**
   * 剔除被提升用户轮后的线程（相邻同角色已折叠），原序保留。
   * 不变量：只要线程里有 user 消息，history 不会以 'user' 收尾——prompt 由
   * callable 追加为最后一条 user 消息，保证接口缝上严格交替。
   */
  history: ConversationMessage[];
  /**
   * 提升后的 prompt 文本：被提升 user run 里较早的段在前（原序）、调用方传入
   * 的 promptText 在后，以 \n\n 连接。promptText 为空时只输出 run 文本。
   */
  prompt: string;
  /** 被提升的最后一条用户消息原文（mention 清洗/fallback 用）；无用户消息 = ''。 */
  latestUserText: string;
}

/**
 * 组装一次 turn 的 (history, prompt)。三条契约（测试见
 * lib/__tests__/turn-thread.test.ts，丢段/重复即红）：
 *
 *   1. 保真 —— 线程每个文本段在 history + prompt 中恰好出现一次。被提升的
 *      最后一条用户消息由 promptText 承载（plain 情形 promptText 即其原文）；
 *      与它连续的更早用户段并入 prompt 前缀。若调用方用派生 promptText（@
 *      清洗文本 / 意图拆解子任务），被提升消息视为**被替换**（与旧
 *      slice(0,-1) 语义一致，@ 噪音/原文不重复回灌）。
 *   2. 合法 —— history 内相邻同角色折叠；history 不以 'user' 收尾（见
 *      TurnThread.history 注释），拼上 prompt 后整串严格交替。
 *   3. 不丢 —— 除被提升（或被替换）的用户轮外，任何消息都不被丢弃：多 agent
 *      串行轮的前序回复、末位非用户的线程全部保留。
 */
export function buildTurnThread(
  messages: ConversationMessage[],
  promptText: string,
): TurnThread {
  // 定位最后一条 user 消息。
  let i = -1;
  for (let k = messages.length - 1; k >= 0; k--) {
    if (messages[k].role === 'user') {
      i = k;
      break;
    }
  }
  if (i < 0) {
    // 无用户消息：无可提升，全线程保留（旧 slice(0,-1) 在这里会盲丢末段）。
    return {
      history: foldConsecutiveRoles(messages),
      prompt: promptText,
      latestUserText: '',
    };
  }
  // user run：从最后一条 user 往前扩到连续 user 段（双连发/多连发的整段）。
  let j = i;
  while (j - 1 >= 0 && messages[j - 1].role === 'user') j--;
  const runTexts = messages.slice(j, i + 1).map(textOf);
  const latestUserText = runTexts[runTexts.length - 1] ?? '';
  // run 之外的两截（前段 + 后段）原序保留——后段是本回合更早 agent 的回复
  // （串行 fan-out 逐轮 reload 时就在尾部），正是旧 slice(0,-1) 误删的部分。
  const prior = [...messages.slice(0, j), ...messages.slice(i + 1)];
  // run 里较早的用户段并入 prompt 前缀（内容不丢、接口缝不出现相邻 user）。
  const prefix = runTexts.slice(0, -1).filter((t) => t.length > 0);
  const prompt = [...prefix, promptText].filter((t) => t.length > 0).join('\n\n');
  return {
    history: foldConsecutiveRoles(prior),
    prompt,
    latestUserText,
  };
}

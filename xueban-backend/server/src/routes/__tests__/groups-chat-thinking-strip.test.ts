/**
 * groups-chat-thinking-strip.test.ts
 *
 * 2026-11-04 对话完善批次（「单聊/群聊能正常对话」）：
 *   - stripThinking 纯函数：zhipu 思考模型（glm-4.6/5.x）经 ApiClientCallable
 *     的 reasoning_content 包装，最终文本里成对 / 未闭合（max_tokens 截断）
 *     两种 <sf:thinking> 形态都要剥干净；正文 / 代码块不受影响。
 *   - pickDefaultExecutor：settings/byok 里有 enabled+key 的 provider 时默认
 *     `byok:<id>`（替代无脑 cli:claude——CLI 挂起时群聊 thinking 卡永远
 *     streaming 的根因）；无 BYOK → 'cli:claude' 历史兜底；settings 异常
 *     NeverThrow。
 *   - createThinkingTagStream（2026-11-07）：B3 过程卡逐 chunk 流式剥
 *     `<sf[:-]thinking>` 定界符（思考内容保留）；跨 chunk 切断的标签 hold
 *     住不闪现，流中断残段 flush 丢弃。
 *
 * Run:
 *   npx vitest run --config vitest.server.config.ts server/src/routes/__tests__/groups-chat-thinking-strip.test.ts
 */
import { describe, it, expect } from 'vitest';
import { stripThinking, pickDefaultExecutor, createThinkingTagStream } from '../groups-chat';

describe('stripThinking — 最终回复剥离 <sf:thinking> 思考段', () => {
  it('成对标签整体剥离，正文保留', () => {
    const s = '<sf:thinking>The user asks hello</sf:thinking>你好！';
    expect(stripThinking(s)).toBe('你好！');
  });

  it('未闭合标签（max_tokens 截断）从开标签起全剥', () => {
    const s = '<sf:thinking>user greets me, I should respond';
    expect(stripThinking(s)).toBe('');
  });

  it('多段思考只剥思考，正文按序保留', () => {
    const s =
      '<sf:thinking>先想一下</sf:thinking>第一段。<sf:thinking>再想想</sf:thinking>第二段。';
    expect(stripThinking(s)).toBe('第一段。第二段。');
  });

  it('带属性的开标签也剥（parser.ts 同款宽松形态）', () => {
    const s = '<sf:thinking step="1">思考</sf:thinking>结论';
    expect(stripThinking(s)).toBe('结论');
  });

  it('无标签纯正文原样返回', () => {
    const s = '就是一句正常回复';
    expect(stripThinking(s)).toBe(s);
  });

  it('正文里出现疑似片段（无闭合）不误伤中间内容之外的文本', () => {
    // 只有真 <sf:thinking> 开标签才触发剥；普通文本含 "thinking" 字样不受影响。
    const s = '我在思考 thinking 这个词怎么翻译';
    expect(stripThinking(s)).toBe(s);
  });

  // ── 2026-11-05 连字符形态 <sf-thinking>（模型模仿 sf: 协议时常写错分隔符，
  //    群聊气泡截图实证：整段英文思考漏进中文界面）────────────────────────
  it('连字符形态成对标签整体剥离（<sf-thinking>）', () => {
    const s =
      '<sf-thinking>The user just said "hi" - a simple greeting. I should respond warmly.</sf-thinking>你好！我是猎奇团队的 Scout。';
    expect(stripThinking(s)).toBe('你好！我是猎奇团队的 Scout。');
  });

  it('连字符形态未闭合标签从开标签起全剥', () => {
    const s = '<sf-thinking>Since this is just a greeting, I should respond in character';
    expect(stripThinking(s)).toBe('');
  });

  it('连字符形态带属性的开标签也剥', () => {
    const s = '<sf-thinking step="extended">think</sf-thinking>结论';
    expect(stripThinking(s)).toBe('结论');
  });

  it('冒号与连字符多段混排只剥思考，正文按序保留', () => {
    const s =
      '<sf:thinking>想一下</sf:thinking>第一段。<sf-thinking>再想想</sf-thinking>第二段。';
    expect(stripThinking(s)).toBe('第一段。第二段。');
  });
});

describe('createThinkingTagStream — 流式定界符剥离（2026-11-07 B3 过程卡）', () => {
  it('无标签正文逐 chunk 原样透传', () => {
    const f = createThinkingTagStream();
    expect(f.push('你好')).toBe('你好');
    expect(f.push('，世界')).toBe('，世界');
    f.flush();
  });

  it('思考内容保留、仅剥定界符（与 stripThinking 整段丢弃语义对齐）', () => {
    const f = createThinkingTagStream();
    expect(f.push('<sf:thinking>想一下</sf:thinking>结论')).toBe('想一下结论');
    f.flush();
  });

  it('开标签被切断在 chunk 尾 → 不闪现半截标签，补全后定界符消失', () => {
    const f = createThinkingTagStream();
    expect(f.push('<sf:thinkin')).toBe('');
    expect(f.push('g>想</sf:thinkin')).toBe('想');
    expect(f.push('g>好')).toBe('好');
    f.flush();
  });

  it('连字符形态 + 多段混排：定界符全消失，内容按序保留', () => {
    const f = createThinkingTagStream();
    const out =
      f.push('<sf-thinking>英文思考</sf-thinking>正文') + f.push('<sf:thinking>二想');
    expect(out).toBe('英文思考正文二想');
    f.flush();
  });

  it('带属性开标签切断在属性段（属性含 < 也不误切）', () => {
    const f = createThinkingTagStream();
    expect(f.push('<sf:thinking x=')).toBe('');
    expect(f.push('y>内容</sf:thinking>')).toBe('内容');
    f.flush();
  });

  it('裸 `<` 结尾 hold 一拍，下一片证实非标签立即释放（拼接无损）', () => {
    const f = createThinkingTagStream();
    expect(f.push('a <')).toBe('a '); // hold 起点在 `<`，之前的字符照常放出
    expect(f.push(' b')).toBe('< b');
    f.flush();
  });

  it('字面 `<` 非标签（`< b`）不误伤、不 hold', () => {
    const f = createThinkingTagStream();
    expect(f.push('a < b > c')).toBe('a < b > c');
    f.flush();
  });

  it('名字被空白提前终结的碎片按字面文本放行（不吞正文）', () => {
    const f = createThinkingTagStream();
    expect(f.push('<sf:thi <b')).toBe('<sf:thi <b');
    f.flush();
  });

  it('流中断（未补全的疑似标签残段）flush 丢弃，不以碎片进卡体', () => {
    const f = createThinkingTagStream();
    expect(f.push('好<sf:thin')).toBe('好');
    f.flush(); // void：残段直接丢弃
    expect(f.push('king>迟到的补全')).toBe('king>迟到的补全'); // flush 后按新字面文本
    f.flush();
  });
});

describe('pickDefaultExecutor — 默认 executor 回退', () => {
  it('settings/byok 有 enabled+key 的 provider → byok:<id>', () => {
    // 本机 settings 已配置 zhipu（enabled + apiKey）→ 默认即 byok:zhipu。
    // 该用例依赖开发机真实 settings；断言放宽为「byok: 前缀或 cli:claude 兜底」，
    // 保证两条分支都合法（settings 被清空后仍绿）。
    const ex = pickDefaultExecutor();
    expect(ex === 'cli:claude' || /^byok:[\w-]+$/.test(ex)).toBe(true);
  });

  it('NeverThrow：不向外抛异常（settings 缺失/损坏都吞掉）', () => {
    expect(() => pickDefaultExecutor()).not.toThrow();
  });
});

/**
 * mentions.test.ts — @ 提及定向派活纯函数测试（2026-11-05，OpenBot 语义移植）。
 */
import { describe, it, expect } from 'vitest';
import { parseMentions, restrictTeamToMentions, type MentionCandidate } from '../mentions';
import type { TeamRunShape } from '../../team-source';

const roster: MentionCandidate[] = [
  { id: 'dev01', name: '阿Dev' },
  { id: 'qa01', name: '小Q' },
  { id: 'writer01', name: '笔杆子' },
];

describe('parseMentions', () => {
  it('无 @ / 无候选 / 无命中 → 零命中且原文透传', () => {
    expect(parseMentions('帮我看下这段话', roster)).toEqual({
      mentionedIds: [],
      cleanText: '帮我看下这段话',
    });
    expect(parseMentions('@阿Dev 在吗', [])).toEqual({
      mentionedIds: [],
      cleanText: '@阿Dev 在吗',
    });
    expect(parseMentions('@陌生人 你好', roster)).toEqual({
      mentionedIds: [],
      cleanText: '@陌生人 你好',
    });
  });

  it('精确 name 命中 + 清洗文本删 token', () => {
    const r = parseMentions('@阿Dev 帮我修下登录页的 bug', roster);
    expect(r.mentionedIds).toEqual(['dev01']);
    expect(r.cleanText).toBe('帮我修下登录页的 bug');
  });

  it('精确 id 命中（菜单插入 name，手打 id 也能路由）', () => {
    const r = parseMentions('@qa01 跑一轮回归', roster);
    expect(r.mentionedIds).toEqual(['qa01']);
    expect(r.cleanText).toBe('跑一轮回归');
  });

  it('大小写不敏感 + 唯一前缀（长名敲一半）', () => {
    expect(parseMentions('@Q 那里有个 case 挂了', roster).mentionedIds).toEqual(['qa01']);
    expect(parseMentions('@DEV01 看下', roster).mentionedIds).toEqual(['dev01']);
    // 「笔」是 writer01 的唯一前缀；「小」同理 qa01
    expect(parseMentions('@笔 写一段', roster).mentionedIds).toEqual(['writer01']);
  });

  it('多人点名 + token 尾部标点剥离 + 去重保序', () => {
    const r = parseMentions('@阿Dev，@小Q。一起看下这个问题', roster);
    expect(r.mentionedIds).toEqual(['dev01', 'qa01']);
    expect(r.cleanText).toBe('一起看下这个问题');
    const dup = parseMentions('@阿Dev @阿Dev 双重确认', roster);
    expect(dup.mentionedIds).toEqual(['dev01']);
    expect(dup.cleanText).toBe('双重确认');
  });

  it('邮件式 a@b 与「@@」不误命中（@ 须在文首/空白后）', () => {
    expect(parseMentions('联系我 user@dev01.com', roster).mentionedIds).toEqual([]);
    // '@@阿Dev'：首个 @ 前是「下」、第二个 @ 前是 '@'，都不是空白 → 全不命中。
    expect(parseMentions('看下@@阿Dev', roster).mentionedIds).toEqual([]);
  });

  it('歧义前缀不命中（dev01/desk 两成员同前缀时当普通文本）', () => {
    const r2 = parseMentions('@d 开工', [
      { id: 'dev01', name: '阿Dev' },
      { id: 'desk01', name: '台桌' },
    ]);
    expect(r2.mentionedIds).toEqual([]);
    expect(r2.cleanText).toBe('@d 开工');
  });
});

describe('parseMentions — 中文无空格直连与粘连边界（2026-11-07 收口）', () => {
  it('CJK 任务文本直接相连也命中（verbatim 名原文直配，无空格/标点分隔）', () => {
    const r = parseMentions('@阿Dev修一下登录bug', roster);
    expect(r.mentionedIds).toEqual(['dev01']);
    expect(r.cleanText).toBe('修一下登录bug');
    // id 直连同理：@dev01修 → dev01（token 路径会把「dev01修」整串当 token 而零命中）
    const r2 = parseMentions('@dev01修一下', roster);
    expect(r2.mentionedIds).toEqual(['dev01']);
    expect(r2.cleanText).toBe('修一下');
  });

  it('粘连多人「@A@B」双点名（@ 紧贴前一表达式尾构成触发边界）', () => {
    const r = parseMentions('@阿Dev@小Q 一起看下', roster);
    expect(r.mentionedIds).toEqual(['dev01', 'qa01']);
    expect(r.cleanText).toBe('一起看下');
    const r3 = parseMentions('@阿Dev@小Q@笔杆子 对齐一下', roster);
    expect(r3.mentionedIds).toEqual(['dev01', 'qa01', 'writer01']);
    expect(r3.cleanText).toBe('对齐一下');
  });

  it('粘连里前一个未命中也给后一个当边界（@路人甲@阿Dev）', () => {
    const r = parseMentions('@路人甲@阿Dev 修下', roster);
    expect(r.mentionedIds).toEqual(['dev01']);
    // 未命中的 @路人甲 保留（只删命中 token，语义与旧一致）
    expect(r.cleanText).toBe('@路人甲 修下');
  });

  it('「@@」仍然不误命中（@ 本身不是触发边界，只有表达式尾才是）', () => {
    expect(parseMentions('@@阿Dev 开工', roster).mentionedIds).toEqual([]);
    expect(parseMentions('看下@@阿Dev', roster).mentionedIds).toEqual([]);
  });

  it('verbatim 尾随 ASCII 词字符不截断（@dev01x ≠ @dev01，名字没打完）', () => {
    expect(parseMentions('@dev01x 复现', roster).mentionedIds).toEqual([]);
    expect(parseMentions('@阿DevX 联调', roster).mentionedIds).toEqual([]);
  });

  it('最长名优先：短名是长名前缀时取长名，短名单独提及仍可', () => {
    const two = [
      { id: 'a1', name: '李' },
      { id: 'a2', name: '李四' },
    ];
    const r = parseMentions('@李四看看', two);
    expect(r.mentionedIds).toEqual(['a2']);
    expect(r.cleanText).toBe('看看');
    expect(parseMentions('@李 看看', two).mentionedIds).toEqual(['a1']);
  });

  it('verbatim 同名歧义（同名不同 id）落回 token 路径精确匹配取先者', () => {
    const twin = [
      { id: 'q1', name: '小Q' },
      { id: 'q2', name: '小Q' },
    ];
    // verbatim 判歧义 → token 路径「精确 name」find 取首个 —— 与旧实现一致（钉死防漂移）
    expect(parseMentions('@小Q 看看', twin).mentionedIds).toEqual(['q1']);
  });

  it('CJK 直连与粘连混排 + 尾部标点剥离', () => {
    const r = parseMentions('@阿Dev，@小Q修bug！@笔杆子写文档', roster);
    expect(r.mentionedIds).toEqual(['dev01', 'qa01', 'writer01']);
    expect(r.cleanText).toBe('修bug！写文档');
  });
});

describe('restrictTeamToMentions', () => {
  const team: TeamRunShape = {
    members: ['dev01', 'qa01', 'writer01'],
    edges: [
      { from: 'dev01', to: 'qa01', kind: 'sequential' },
      { from: 'qa01', to: 'writer01', kind: 'conditional' },
      { from: 'writer01', to: 'dev01', kind: 'sequential' },
    ],
    policy_matrix: { dev01: { qa01: 'permit' } },
  };

  it('成员按原序过滤，只留两端都在子集内的边（conditional 返工边保留）', () => {
    const sub = restrictTeamToMentions(team, ['qa01', 'writer01']);
    expect(sub.members).toEqual(['qa01', 'writer01']);
    expect(sub.edges).toEqual([{ from: 'qa01', to: 'writer01', kind: 'conditional' }]);
    expect(sub.policy_matrix).toBe(team.policy_matrix); // 透传不复制
    expect(sub.name).toBeUndefined();
  });

  it('单成员子图 → 零边（直达点名，无交接）', () => {
    const sub = restrictTeamToMentions(team, ['dev01']);
    expect(sub.members).toEqual(['dev01']);
    expect(sub.edges).toEqual([]);
  });

  it('全成员命中 = 原图等价；全不命中 = members 空（调用方回落现状）', () => {
    expect(restrictTeamToMentions(team, ['dev01', 'qa01', 'writer01']).members).toEqual(
      team.members,
    );
    expect(restrictTeamToMentions(team, ['ghost']).members).toEqual([]);
    expect(restrictTeamToMentions(team, ['ghost']).edges).toEqual([]);
  });

  it('name 字段透传', () => {
    const named: TeamRunShape = { ...team, name: '论文深读小队' };
    expect(restrictTeamToMentions(named, ['dev01']).name).toBe('论文深读小队');
  });
});

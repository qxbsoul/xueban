import assert from 'node:assert/strict';
import { resolveUnitTarget, resolveCrossSectionTarget } from '../src/components/unit-navigation';

const units = [
  { id: 'unit-a-1', title: '平方根', point_titles: ['平方根的概念', '开平方'] },
  { id: 'unit-b-2', title: '算术平方根', point_titles: ['算术平方根的概念'] },
  { id: 'unit-a-1', title: '重复标题', point_titles: ['重复知识点'] },
];
assert.equal(resolveUnitTarget(units, '平方根')?.unit.id, 'unit-a-1');
assert.equal(resolveUnitTarget(units, '平方根', 'unit-b-2')?.unit.id, 'unit-b-2');
assert.equal(resolveUnitTarget(units, '算术平方根的概念')?.unit.id, 'unit-b-2');
assert.equal(resolveUnitTarget(units, '重复知识点')?.unit.id, 'unit-a-1');
assert.equal(resolveUnitTarget(units, '不存在'), null);

const sections = [
  { id: 's1', lesson: { id: 'lesson-1' }, knowledge_points: [{ title: '平方根' }] },
  { id: 's2', lesson: { id: 'lesson-2' }, knowledge_points: [{ title: '立方根' }] },
];
assert.equal(resolveCrossSectionTarget(sections, '立方根', 'lesson-1')?.lesson?.id, 'lesson-2');
assert.equal(resolveCrossSectionTarget(sections, '平方根', 'lesson-1'), null);
assert.equal(resolveCrossSectionTarget(sections, '不存在', 'lesson-1'), null);
console.log('unit navigation: ok');
import assert from 'node:assert/strict';
import { sourceTableStatus, sourceTableStatusLabel } from '../src/components/table-status';

assert.equal(sourceTableStatus({ markdown: '<table><tbody><tr><td>1</td></tr></tbody></table>' }), 'needs_review');
assert.equal(sourceTableStatus({ markdown: '<table data-table-reliable="true" data-table-status="structure_validated"></table>' }), 'structure_validated');
assert.match(sourceTableStatusLabel('structure_validated'), /内容待核对/);
assert.equal(sourceTableStatus({ markdown: '<table data-table-status="content_verified"></table>' }), 'content_verified');
assert.equal(sourceTableStatusLabel('content_verified'), '内容已验证');
assert.equal(sourceTableStatus({ markdown: '<table data-table-status="failed"></table>' }), 'failed');
assert.equal(sourceTableStatusLabel('failed'), '表格校验失败');
assert.equal(sourceTableStatus({ table_status: 'structure_validated', markdown: '' }), 'structure_validated');
assert.equal(sourceTableStatus({ table_status: '', markdown: '' }), 'needs_review');
assert.equal(sourceTableStatus({ markdown: '<table data-table-status="unknown"></table>' }), 'needs_review');
console.log('table status states: ok');
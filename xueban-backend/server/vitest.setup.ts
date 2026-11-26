import path from 'path';

/**
 * 每个测试文件使用独立数据目录，避免测试文件之间共享 sqlite 造成串数据。
 * 目录位于本次运行创建的临时根目录下，测试结束由 global teardown 统一删除。
 */
const base = process.env.XUEBAN_DATA_DIR || '';
if (!base) throw new Error('缺少 XUEBAN_DATA_DIR：拒绝运行 Node 测试');
const sequence = Number(process.env.__XUEBAN_FILE_SEQ || '0') + 1;
process.env.__XUEBAN_FILE_SEQ = String(sequence);
process.env.XUEBAN_DATA_DIR = path.join(base, `file-${process.pid}-${sequence}`);

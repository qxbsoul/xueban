import fs from 'fs';

/**
 * 测试隔离守卫：确认本次测试使用的是临时数据目录，而不是工作区真实 .xueban。
 * 测试结束后删除临时目录（保留日志中记录的路径便于排查）。
 */
export async function setup(): Promise<void> {
  const dir = process.env.XUEBAN_DATA_DIR || '';
  const cwd = process.cwd();
  if (!dir) throw new Error('缺少 XUEBAN_DATA_DIR：拒绝运行 Node 测试');
  if (dir === cwd || dir.startsWith(cwd + '/.xueban') || dir.startsWith(cwd + '\\.xueban')) {
    throw new Error(`测试数据目录不能是工作区运行目录：${dir}`);
  }
  fs.mkdirSync(dir, { recursive: true });
  // eslint-disable-next-line no-console
  console.log(`[vitest] isolated data dir: ${dir}`);
}

export async function teardown(): Promise<void> {
  const dir = process.env.XUEBAN_DATA_DIR || '';
  if (!dir) return;
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    // 清理失败不影响测试结论
  }
  // 测试自身也会在 TEMP 下创建 sf-* 临时目录；统一清掉本次运行根目录。
  const root = require('path').dirname(dir);
  if (require('path').basename(root) === '_vitest_tmp') {
    try {
      fs.rmSync(root, { recursive: true, force: true });
    } catch {
      // ignore
    }
  }
}

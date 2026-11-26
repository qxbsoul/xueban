/**
 * Vitest configuration for the standalone Node/Express backend.
 *
 * Run from server/:
 *   npx vitest run
 *
 * Standalone tsx script tests are run separately by:
 *   npm run test:scripts
 */
import { defineConfig } from 'vitest/config';
import path from 'path';
import fs from 'fs';
import os from 'os';

// 每次测试运行使用独立临时数据目录，绝不写工作区真实 .xueban/app.sqlite。
// 优先放在 Git 仓库之外（某些测试会用 git 判断“普通目录”），也避免 Windows
// 上 Node 对用户 Temp 目录的 EPERM 限制。
function createTestDataDir(): string {
  const preferred = path.resolve(__dirname, '..', '..', '..');
  const candidates = [preferred, os.tmpdir()];
  for (const base of candidates) {
    try {
      const root = path.join(base, '_vitest_tmp');
      fs.mkdirSync(root, { recursive: true });
      // Vite/Vitest 自身也会用 os.tmpdir() 建 SSR 目录；统一指到可写目录，
      // 避免受限 Windows 环境下 Node 在用户 Temp 目录收到 EPERM。
      process.env.TEMP = root;
      process.env.TMP = root;
      return fs.mkdtempSync(path.join(root, 'run-'));
    } catch {
      // 尝试下一个候选目录
    }
  }
  throw new Error('无法创建测试临时目录');
}

const testDataDir = process.env.XUEBAN_TEST_DATA_DIR?.trim() || createTestDataDir();
process.env.XUEBAN_DATA_DIR = testDataDir;

export default defineConfig({
  // Keep the monorepo root as cwd: several integration tests exercise root
  // scripts and Python files even though the runner now belongs to server/.
  root: path.resolve(__dirname, '..'),
  test: {
    globals: true,
    environment: 'node',
    env: { XUEBAN_DATA_DIR: testDataDir, XUEBAN_REPO_ROOT: path.resolve(__dirname, '..') },
    globalSetup: [path.resolve(__dirname, 'vitest.global-setup.ts')],
    setupFiles: [path.resolve(__dirname, 'vitest.setup.ts')],
    include: ['server/src/**/*.{test,spec}.{ts,tsx}'],
    // Many *.test.ts files are standalone check()/assert() scripts. Vitest can
    // collect them without executing their assertions; npm run test:scripts is
    // therefore a required companion gate, not an optional extra.
    passWithNoTests: true,
    // skill-loader.test.ts verifies a CommonJS require() lazy-loading path that
    // is not available when Vitest transforms the module to ESM. It remains in
    // the standalone tsx test suite.
    exclude: [
      '**/node_modules/**',
      '**/dist/**',
      'server/src/loaders/skill-loader.test.ts',
    ],
    testTimeout: 30000,
    hookTimeout: 30000,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});

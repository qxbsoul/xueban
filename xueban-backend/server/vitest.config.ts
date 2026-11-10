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

export default defineConfig({
  // Keep the monorepo root as cwd: several integration tests exercise root
  // scripts and Python files even though the runner now belongs to server/.
  root: path.resolve(__dirname, '..'),
  test: {
    globals: true,
    environment: 'node',
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

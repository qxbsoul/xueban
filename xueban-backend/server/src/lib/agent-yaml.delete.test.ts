/**
 * agent-yaml deleteAgent — unit test (2026-05-30).
 *
 * Backs the /api/agents DELETE fallback that removes a local yaml soul-template
 * when Python 404s. fs is mocked so the test never touches real .agent.yaml
 * files (see memory/feedback_tests_isolate_data_dir).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import { deleteAgent } from './agent-yaml';

vi.mock('fs', () => ({
  default: { existsSync: vi.fn(), unlinkSync: vi.fn() },
}));

const mockFs = fs as unknown as { existsSync: ReturnType<typeof vi.fn>; unlinkSync: ReturnType<typeof vi.fn> };

describe('agent-yaml deleteAgent', () => {
  beforeEach(() => vi.clearAllMocks());

  it('unlinks the resolved .agent.yaml and returns true', () => {
    mockFs.existsSync.mockReturnValue(true);
    expect(deleteAgent('reader')).toBe(true);
    expect(mockFs.unlinkSync).toHaveBeenCalledTimes(1);
    expect(String(mockFs.unlinkSync.mock.calls[0][0])).toContain('reader.agent.yaml');
  });

  it('returns false (no unlink) when no yaml file exists for the id', () => {
    mockFs.existsSync.mockReturnValue(false);
    expect(deleteAgent('ghost')).toBe(false);
    expect(mockFs.unlinkSync).not.toHaveBeenCalled();
  });

  it('rejects an invalid id without touching fs', () => {
    expect(deleteAgent('../etc/passwd')).toBe(false);
    expect(mockFs.existsSync).not.toHaveBeenCalled();
    expect(mockFs.unlinkSync).not.toHaveBeenCalled();
  });
});

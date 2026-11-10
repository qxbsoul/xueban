/**
 * groups-chat-dag-branch.test.ts
 *
 * 纯单元测试 shouldRunTeamDag() 分支判定逻辑(无 I/O 依赖)。
 *
 * 选择 Fallback 路径(而非完整 runFanout 集成测试)的原因:
 *   - loadTeamForRun 读取磁盘上的 .json 文件(cwd 相对路径),在 vitest 环境
 *     下驱动文件读取路径需要 monkeypatch 模块或传入 dirs 参数,与测试框架
 *     (tsx/vitest in server/) 不兼容。
 *   - 完整多 agent E2E(真 LLM DAG 调度)需要 API key,已明确 DEFERRED。
 *   - shouldRunTeamDag 是分支决策的唯一闸口:它覆盖所有判定路径,是可靠的
 *     客观验收点(Script/Validation = 客观关卡,不靠 AI 自我汇报)。
 *
 * Run:
 *   cd server
 *   npx vitest run src/routes/__tests__/groups-chat-dag-branch.test.ts
 */
import { describe, it, expect } from 'vitest';
import { shouldRunTeamDag } from '../groups-chat';

describe('shouldRunTeamDag — 分支判定', () => {
  it('team_id 有值 → 走 DAG 路径', () => {
    expect(shouldRunTeamDag({ team_id: 'team-abc-123' })).toBe(true);
  });

  it('team_id 为 undefined → 串行降级', () => {
    expect(shouldRunTeamDag({ team_id: undefined })).toBe(false);
  });

  it('team_id 为 null → 串行降级', () => {
    expect(shouldRunTeamDag({ team_id: null })).toBe(false);
  });

  it('team_id 为空字符串 → 串行降级', () => {
    expect(shouldRunTeamDag({ team_id: '' })).toBe(false);
  });

  it('team_id 为任意非空字符串 → true', () => {
    for (const id of ['t1', 'team-x', '00000000-0000-0000-0000-000000000001']) {
      expect(shouldRunTeamDag({ team_id: id })).toBe(true);
    }
  });
});

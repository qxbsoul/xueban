/**
 * conversations.ts — Story 15.16 — REST API for Conversation + Message.
 *
 *   GET  /api/projects/:pid/conversations          → list (newest first)
 *   POST /api/projects/:pid/conversations          → create → 201 (404 if project missing)
 *   GET  /api/conversations/:cid/messages          → list ASC by created_at
 *   POST /api/conversations/:cid/messages          → append → 201 (404 if convo missing)
 *   PATCH /api/conversations/by-group/:gid         → GAP A3 回写（2026-08-23）改会话
 *                                                    归属项目（「移入项目」）
 *
 * Two routers are exported because the routes live under different mount
 * points. index.ts mounts them at /api/projects and /api/conversations.
 */

import { Router, Request, Response } from 'express';
import {
  appendMessage,
  createConversation,
  getConversation,
  getConversationByGroup,
  listConversations,
  listMessages,
  setConversationProject,
} from '../storage/conversations';
import { getOrCreateProject, getProject } from '../storage/projects';

// Mounted at /api/projects — handles per-project conversation listing/creation.
export const projectScopedConversationsRouter = Router({ mergeParams: true });

projectScopedConversationsRouter.get(
  '/:pid/conversations',
  (req: Request, res: Response) => {
    if (!getProject(req.params.pid)) {
      res
        .status(404)
        .json({ error: { code: 'NOT_FOUND', message: 'Project not found' } });
      return;
    }
    res.json(listConversations(req.params.pid));
  },
);

projectScopedConversationsRouter.post(
  '/:pid/conversations',
  (req: Request, res: Response) => {
    if (!getProject(req.params.pid)) {
      res
        .status(404)
        .json({ error: { code: 'NOT_FOUND', message: 'Project not found' } });
      return;
    }
    const body = (req.body ?? {}) as { title?: unknown };
    const title =
      typeof body.title === 'string' && body.title.trim().length > 0
        ? body.title.trim()
        : undefined;
    const c = createConversation(req.params.pid, title);
    res.status(201).json(c);
  },
);

// Mounted at /api/conversations — message endpoints keyed on conversation_id.
const router = Router();

/**
 * GAP A3 回写（2026-08-23）— 「移入项目」：按互链群 id（B1 group_id ↔ Python
 * 群/DM）改会话归属项目。前端 rail 只有 group_id 没有 conversation_id，故以
 * 群为键；群还没有互链 conversation（尚未发过消息/run）时 find-or-create
 * 落目标项目——语义与 POST /api/run-sessions 的 group_id 分支一致。
 *
 * body.project_id：非空 string → 目标项目（不存在 → 404 PROJECT_NOT_FOUND）；
 * null / '' → 默认项目（getOrCreateProject('default')，「移出项目」回默认组）。
 */
router.patch('/by-group/:gid', (req: Request, res: Response) => {
  const gid = req.params.gid;
  const body = (req.body ?? {}) as { project_id?: unknown };
  if (!('project_id' in body)) {
    res.status(400).json({
      error: {
        code: 'INVALID_PROJECT_ID',
        message: 'project_id is required (string, or null/"" for the default project)',
      },
    });
    return;
  }
  let projectId: string;
  if (typeof body.project_id === 'string' && body.project_id.trim().length > 0) {
    const p = getProject(body.project_id.trim());
    if (!p) {
      res
        .status(404)
        .json({ error: { code: 'PROJECT_NOT_FOUND', message: 'Project not found' } });
      return;
    }
    projectId = p.project_id;
  } else if (body.project_id === null || body.project_id === '') {
    projectId = getOrCreateProject('default', 'Default Project').project_id;
  } else {
    res.status(400).json({
      error: {
        code: 'INVALID_PROJECT_ID',
        message: 'project_id must be a string, null, or ""',
      },
    });
    return;
  }

  const existing = getConversationByGroup(gid);
  if (!existing) {
    res.status(201).json(createConversation(projectId, undefined, gid));
    return;
  }
  if (existing.project_id !== projectId) {
    setConversationProject(existing.conversation_id, projectId);
  }
  res.json(getConversation(existing.conversation_id));
});

router.get('/:cid/messages', (req: Request, res: Response) => {
  if (!getConversation(req.params.cid)) {
    res
      .status(404)
      .json({ error: { code: 'NOT_FOUND', message: 'Conversation not found' } });
    return;
  }
  res.json(listMessages(req.params.cid));
});

router.post('/:cid/messages', (req: Request, res: Response) => {
  if (!getConversation(req.params.cid)) {
    res
      .status(404)
      .json({ error: { code: 'NOT_FOUND', message: 'Conversation not found' } });
    return;
  }
  const body = (req.body ?? {}) as {
    role?: unknown;
    content?: unknown;
    run_id?: unknown;
  };
  if (
    body.role !== 'user' &&
    body.role !== 'assistant' &&
    body.role !== 'system'
  ) {
    res.status(400).json({
      error: { code: 'INVALID_ROLE', message: 'role must be user|assistant|system' },
    });
    return;
  }
  if (typeof body.content !== 'string' || body.content.length === 0) {
    res.status(400).json({
      error: { code: 'INVALID_CONTENT', message: 'content is required' },
    });
    return;
  }
  const m = appendMessage(req.params.cid, {
    role: body.role,
    content: body.content,
    run_id: typeof body.run_id === 'string' ? body.run_id : null,
  });
  res.status(201).json(m);
});

export default router;

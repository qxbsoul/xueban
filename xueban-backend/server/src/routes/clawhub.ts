/**
 * routes/clawhub.ts — ClawHub 安装会话 REST + SSE（2026-06）
 *
 *   POST /api/clawhub/install/:cliId          → 201 {install_id, cli_id, command, stream_url}
 *                                                404 未知 cliId · 422 copy-only · 409 已在安装
 *   GET  /api/clawhub/install/:installId/stream → SSE（install-start / install-log /
 *                                                install-done / install-error；15s 心跳）
 *   POST /api/clawhub/install/:installId/cancel → 204（SIGTERM→5s→SIGKILL）
 *
 * 复刻 run-sessions 的 POST-建会话 + GET-SSE 双端点模式（EventSource 只能 GET）。
 * 安装 SSE 为一次性短生命周期流：不做续传，断开即终止订阅（前端可重新发起）。
 */

import { Router, type Request, type Response } from 'express';
import {
  InstallError,
  cancelInstall,
  getInstall,
  startInstall,
  startUninstall,
  type InstallSession,
} from '../lib/claw-installer';

const router = Router();

/** SSE 心跳间隔（run-sessions 同款 15s）。 */
const HEARTBEAT_MS = 15_000;

router.post('/install/:cliId', (req: Request, res: Response) => {
  try {
    const session = startInstall(req.params.cliId);
    res.status(201).json({
      install_id: session.install_id,
      cli_id: session.cli_id,
      command: session.command,
      stream_url: `/api/clawhub/install/${session.install_id}/stream`,
    });
  } catch (err) {
    if (err instanceof InstallError) {
      res.status(err.status).json({ error: err.code, message: err.message });
      return;
    }
    res.status(500).json({
      error: 'INSTALL_INTERNAL',
      message: (err as Error).message,
    });
  }
});

router.post('/uninstall/:cliId', (req: Request, res: Response) => {
  try {
    const session = startUninstall(req.params.cliId);
    res.status(201).json({
      install_id: session.install_id,
      cli_id: session.cli_id,
      command: session.command,
      stream_url: `/api/clawhub/install/${session.install_id}/stream`,
    });
  } catch (err) {
    if (err instanceof InstallError) {
      res.status(err.status).json({ error: err.code, message: err.message });
      return;
    }
    res.status(500).json({
      error: 'UNINSTALL_INTERNAL',
      message: (err as Error).message,
    });
  }
});

router.get('/install/:installId/stream', (req: Request, res: Response) => {
  const session: InstallSession | undefined = getInstall(req.params.installId);
  if (!session) {
    res.status(404).json({
      error: 'INSTALL_SESSION_NOT_FOUND',
      message: `install session ${req.params.installId} not found`,
    });
    return;
  }

  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });

  let cursor = 0;
  let closed = false;

  const writeEvent = (event: string, data: Record<string, unknown>) => {
    if (closed) return;
    try {
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    } catch {
      closed = true;
    }
  };

  // drain 已累积事件（含 install-start），随后增量推送直到终态帧。
  const drain = () => {
    while (cursor < session.events.length) {
      const ev = session.events[cursor++];
      writeEvent(ev.event, ev.data);
    }
  };
  drain();

  const isTerminal =
    session.status === 'done' || session.status === 'error' || session.status === 'canceled';

  if (isTerminal) {
    res.end();
    return;
  }

  const pollTimer = setInterval(() => {
    drain();
    const terminal =
      session.status === 'done' || session.status === 'error' || session.status === 'canceled';
    if (terminal) {
      cleanup();
      res.end();
    }
  }, 120);

  const heartbeat = setInterval(() => {
    if (!closed) {
      try {
        res.write(': ping\n\n');
      } catch {
        closed = true;
      }
    }
  }, HEARTBEAT_MS);
  if (typeof heartbeat.unref === 'function') heartbeat.unref();

  const cleanup = () => {
    clearInterval(pollTimer);
    clearInterval(heartbeat);
  };

  req.on('close', () => {
    closed = true;
    cleanup();
  });
});

router.post('/install/:installId/cancel', (req: Request, res: Response) => {
  const ok = cancelInstall(req.params.installId);
  if (!ok) {
    res.status(404).json({
      error: 'INSTALL_SESSION_NOT_FOUND',
      message: `install session ${req.params.installId} not found`,
    });
    return;
  }
  res.status(204).end();
});

export default router;

"""APScheduler BackgroundScheduler singleton — Story 14.2 AC3.

BackgroundScheduler (thread-based) is used instead of AsyncIOScheduler to keep
the scheduler completely isolated from FastAPI's asyncio event loop (Eng decision #11).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(daemon=True)
    return _scheduler


def start_scheduler() -> BackgroundScheduler:
    s = get_scheduler()
    if not s.running:
        s.start()
        logger.info("APScheduler BackgroundScheduler started")
    return s


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler BackgroundScheduler stopped")

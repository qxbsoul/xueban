"""FastAPI routers for Epic 4 fleet-level observability (Stories 4.7/4.8/4.9)
and Epic 9 knowledge endpoints (Story 9.1)."""

from xueban.api.archive import router as archive_router, ArchiveService
from xueban.api.policy_observability import (
    router as policy_obs_router,
    PolicyObsAggregator,
)
from xueban.api.knowledge import (
    router as knowledge_router,
    KnowledgeService,
)
from xueban.api.runs import router as runs_router

__all__ = [
    "",
    "archive_router",
    "policy_obs_router",
    "knowledge_router",
    "runs_router",
    "OpsAggregator",
    "ArchiveService",
    "PolicyObsAggregator",
    "KnowledgeService",
]

"""Kit Smoke Eval Pack registry — Story 10.6

Each Kit module under this package exports a module-level
``KIT_SMOKE_EVAL_PACK: KitSmokeEvalPack``.  The :func:`get_eval_pack`
helper resolves a kit id (canonical or short alias) to its EvalPack.

Public exports:
  KitSmokeEvalPack / SmokeCase / RegressionCase / SuggestedFix
  SmokeRunOptions / SmokeRunReport / SmokeCaseResult
  RegressionReport / RegressionVerdict
  KitSmokeRunner
  get_eval_pack / list_eval_pack_ids
"""
from __future__ import annotations

from .runner import (
    KitSmokeRunner,
    RegressionCase,
    RegressionReport,
    SmokeCase,
    SmokeCaseResult,
    SmokeRunOptions,
    SmokeRunReport,
    SuggestedFix,
    KitSmokeEvalPack,
    get_eval_pack,
    list_eval_pack_ids,
    register_eval_pack,
    BASELINE_DIR,
)

__all__ = [
    "KitSmokeRunner",
    "RegressionCase",
    "RegressionReport",
    "SmokeCase",
    "SmokeCaseResult",
    "SmokeRunOptions",
    "SmokeRunReport",
    "SuggestedFix",
    "KitSmokeEvalPack",
    "get_eval_pack",
    "list_eval_pack_ids",
    "register_eval_pack",
    "BASELINE_DIR",
]

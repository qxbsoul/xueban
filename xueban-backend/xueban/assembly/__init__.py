# xueban/assembly — catalog-level activation and connection resolution.
# This module handles "which blocks to activate for a given goal" (ActivationSelector)
# and "how to connect them" (ConnectionResolver).
# It is NOT the runtime node-level activation (WorkflowActivationSpec in highlevel.py).
from xueban.assembly.activation import (
    ActivationResult,
    ActivationSelector,
    CatalogActivationCandidate,
    ConnectionResolver,
)
from xueban.assembly.compile import CompilationError, compile, compile_agents

__all__ = [
    "ActivationResult",
    "ActivationSelector",
    "CatalogActivationCandidate",
    "ConnectionResolver",
    "CompilationError",
    "compile",
    "compile_agents",
]

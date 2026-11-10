from agentgraph.core.agent import Agent, AgentConfig, AgentResult
try:
    from agentgraph.core.graph import AgentGraph
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    AgentGraph = None

try:
    from agentgraph.core.router import RuleRouter, SwarmRouter
except ImportError:  # pragma: no cover - legacy optional export guard
    from agentgraph.core.router import RuleRouter
    SwarmRouter = None
from agentgraph.core.state import State, AgentState, AgentContext, AgentStatus
from agentgraph.core.node import (
    BaseNode,
    NodeConfig,
    NodeResult,
    NodeStatus,
    FunctionNode,
    node as node_decorator,
)
from agentgraph.core.registry import (
    NodeRegistry,
    RegistryEntry,
    get_registry,
    register_node,
    create_node,
    list_nodes,
    node_exists,
)
from agentgraph.core.errors import (
    # Error classes
    AgentGraphError,
    ValidationError,
    ExecutionError,
    NodeError,
    CircuitBreakerError,
    ErrorCode,
    # Validation functions
    raise_if_empty,
    raise_if_length,
    validate_type,
    validate_required,
    validate_range,
    # Error handling
    ErrorLogger,
    ErrorHandler,
    ErrorLog,
    get_error_logger,
    set_error_logger,
    # Circuit breaker
    CircuitBreaker,
    CircuitState,
    circuit_breaker,
)

__all__ = [
    # Agent
    "Agent",
    "AgentConfig",
    "AgentResult",
    # Graph
    "AgentGraph",
    # Router
    "RuleRouter",
    "SwarmRouter",
    # State
    "State",
    "AgentState",
    "AgentContext",
    "AgentStatus",
    # Node
    "BaseNode",
    "NodeConfig",
    "NodeResult",
    "NodeStatus",
    "FunctionNode",
    "node_decorator",
    # Registry
    "NodeRegistry",
    "RegistryEntry",
    "get_registry",
    "register_node",
    "create_node",
    "list_nodes",
    "node_exists",
    # Errors
    "AgentGraphError",
    "ValidationError",
    "ExecutionError",
    "NodeError",
    "CircuitBreakerError",
    "ErrorCode",
    # Validation
    "raise_if_empty",
    "raise_if_length",
    "validate_type",
    "validate_required",
    "validate_range",
    # Error handling
    "ErrorLogger",
    "ErrorHandler",
    "ErrorLog",
    "get_error_logger",
    "set_error_logger",
    # Circuit breaker
    "CircuitBreaker",
    "CircuitState",
    "circuit_breaker",
]

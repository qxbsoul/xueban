"""
Node module for AgentGraph.

This module provides the base node abstraction for the graph system.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from enum import Enum
import uuid


class NodeStatus(Enum):
    """Node execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class NodeConfig:
    """Configuration for a node.

    Attributes:
        id: Unique identifier for the node.
        name: Human-readable name for the node.
        description: Optional description of what the node does.
        tags: Optional list of tags for categorization.
        metadata: Optional additional metadata.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.name:
            self.name = self.id

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NodeConfig":
        """Create config from dictionary."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data.get("name", ""),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
        )


@dataclass
class NodeResult:
    """Result of node execution.

    Attributes:
        success: Whether the execution was successful.
        output: Output data from the execution.
        error: Error message if execution failed.
        metadata: Additional metadata about the execution.
    """
    success: bool = True
    output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseNode(ABC):
    """Abstract base class for all nodes in the graph.

    All nodes must implement the execute method which takes inputs
    and returns outputs as dictionaries.
    """

    def __init__(self, config: Optional[NodeConfig] = None):
        """Initialize the node.

        Args:
            config: Optional node configuration.
        """
        self.config = config or NodeConfig()
        self._status: NodeStatus = NodeStatus.PENDING
        self._inputs: Dict[str, Any] = {}
        self._outputs: Dict[str, Any] = {}
        self._error: Optional[str] = None

    @property
    def id(self) -> str:
        """Get node ID."""
        return self.config.id

    @property
    def name(self) -> str:
        """Get node name."""
        return self.config.name

    @property
    def status(self) -> NodeStatus:
        """Get current node status."""
        return self._status

    @property
    def description(self) -> str:
        """Get node description."""
        return self.config.description

    @abstractmethod
    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the node logic.

        Args:
            inputs: Input data for the node.

        Returns:
            Output data from the node.

        Raises:
            ExecutionError: If execution fails.
        """
        pass

    def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate input data before execution.

        Override this method to add custom validation logic.

        Args:
            inputs: Input data to validate.

        Returns:
            True if inputs are valid, False otherwise.
        """
        return True

    def validate_outputs(self, outputs: Dict[str, Any]) -> bool:
        """Validate output data after execution.

        Override this method to add custom validation logic.

        Args:
            outputs: Output data to validate.

        Returns:
            True if outputs are valid, False otherwise.
        """
        return True

    async def run(self, inputs: Dict[str, Any]) -> NodeResult:
        """Run the node with the given inputs.

        This method handles status updates and error handling.

        Args:
            inputs: Input data for the node.

        Returns:
            NodeResult containing the execution result.
        """
        self._status = NodeStatus.RUNNING
        self._inputs = inputs.copy()
        self._error = None

        try:
            # Validate inputs
            if not self.validate_inputs(inputs):
                raise ValueError(f"Input validation failed for node {self.name}")

            # Execute
            outputs = await self.execute(inputs)

            # Validate outputs
            if not self.validate_outputs(outputs):
                raise ValueError(f"Output validation failed for node {self.name}")

            self._outputs = outputs
            self._status = NodeStatus.COMPLETED

            return NodeResult(
                success=True,
                output=outputs,
                metadata={"node_id": self.id, "node_name": self.name}
            )

        except Exception as e:
            self._status = NodeStatus.FAILED
            self._error = str(e)
            self._outputs = {}

            return NodeResult(
                success=False,
                output={},
                error=str(e),
                metadata={"node_id": self.id, "node_name": self.name}
            )

    def reset(self) -> None:
        """Reset node to initial state."""
        self._status = NodeStatus.PENDING
        self._inputs = {}
        self._outputs = {}
        self._error = None

    def get_info(self) -> Dict[str, Any]:
        """Get node information."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self._status.value,
            "config": self.config.to_dict(),
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.id!r}, name={self.name!r})"


class FunctionNode(BaseNode):
    """A node that wraps a function."""

    def __init__(
        self,
        func,
        config: Optional[NodeConfig] = None,
        name: Optional[str] = None
    ):
        """Initialize function node.

        Args:
            func: The function to wrap.
            config: Optional node configuration.
            name: Optional name (defaults to function name).
        """
        if config is None:
            config = NodeConfig(
                name=name or func.__name__,
                description=func.__doc__ or ""
            )
        super().__init__(config)
        self._func = func

    async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the wrapped function."""
        import asyncio
        import inspect

        if inspect.iscoroutinefunction(self._func):
            result = await self._func(**inputs)
        else:
            result = self._func(**inputs)

        if isinstance(result, dict):
            return result
        return {"result": result}


def node(
    name: Optional[str] = None,
    description: str = "",
    tags: Optional[List[str]] = None
):
    """Decorator to create a FunctionNode from a function.

    Args:
        name: Optional name for the node.
        description: Optional description.
        tags: Optional tags.

    Returns:
        Decorator function.
    """
    def decorator(func):
        config = NodeConfig(
            name=name or func.__name__,
            description=description or func.__doc__ or "",
            tags=tags or []
        )
        return FunctionNode(func, config=config)
    return decorator

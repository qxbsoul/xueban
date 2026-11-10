from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import time


class AgentStatus(Enum):
    """Status of an agent."""
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class State:
    """Basic state container for workflow execution.

    Attributes:
        input: The input string for the workflow.
        user_id: The user identifier.
        history: List of historical interactions.
        metadata: Additional metadata.
    """
    input: str
    user_id: str
    history: list = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)

    def set(self, key: str, value: Any):
        self.metadata[key] = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input": self.input,
            "user_id": self.user_id,
            "history": self.history,
            "metadata": self.metadata,
        }


@dataclass
class AgentState:
    """State container for an individual agent.

    Tracks the current state, memory, and performance metrics
    for a single agent instance.

    Attributes:
        agent_id: Unique identifier for the agent.
        status: Current status of the agent.
        memory: Agent's working memory.
        metrics: Performance and operational metrics.
        created_at: Timestamp when the state was created.
        updated_at: Timestamp when the state was last updated.
    """
    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    memory: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def update_status(self, status: AgentStatus) -> None:
        """Update the agent status.

        Args:
            status: The new status.
        """
        self.status = status
        self.updated_at = datetime.utcnow()

    def set_memory(self, key: str, value: Any) -> None:
        """Set a value in the agent's memory.

        Args:
            key: The memory key.
            value: The value to store.
        """
        self.memory[key] = value
        self.updated_at = datetime.utcnow()

    def get_memory(self, key: str, default: Any = None) -> Any:
        """Get a value from the agent's memory.

        Args:
            key: The memory key.
            default: Default value if key not found.

        Returns:
            The stored value or default.
        """
        return self.memory.get(key, default)

    def clear_memory(self) -> None:
        """Clear all memory."""
        self.memory.clear()
        self.updated_at = datetime.utcnow()

    def record_metric(self, name: str, value: Any) -> None:
        """Record a metric value.

        Args:
            name: The metric name.
            value: The metric value.
        """
        if name not in self.metrics:
            self.metrics[name] = []
        self.metrics[name].append({
            "value": value,
            "timestamp": datetime.utcnow().isoformat()
        })
        self.updated_at = datetime.utcnow()

    def get_metric(self, name: str) -> Optional[List[Dict[str, Any]]]:
        """Get metric history.

        Args:
            name: The metric name.

        Returns:
            List of metric records or None.
        """
        return self.metrics.get(name)

    def get_latest_metric(self, name: str) -> Optional[Any]:
        """Get the latest value of a metric.

        Args:
            name: The metric name.

        Returns:
            The latest metric value or None.
        """
        history = self.metrics.get(name)
        if history:
            return history[-1]["value"]
        return None

    def increment_counter(self, name: str, amount: int = 1) -> int:
        """Increment a counter metric.

        Args:
            name: The counter name.
            amount: Amount to increment by.

        Returns:
            The new counter value.
        """
        current = self.get_latest_metric(name) or 0
        new_value = current + amount
        self.record_metric(name, new_value)
        return new_value

    def to_dict(self) -> Dict[str, Any]:
        """Convert state to dictionary."""
        return {
            "agent_id": self.agent_id,
            "status": self.status.value,
            "memory": self.memory,
            "metrics": self.metrics,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentState":
        """Create state from dictionary."""
        return cls(
            agent_id=data["agent_id"],
            status=AgentStatus(data.get("status", "idle")),
            memory=data.get("memory", {}),
            metrics=data.get("metrics", {}),
            created_at=datetime.fromisoformat(data["created_at"])
                if "created_at" in data else datetime.utcnow(),
            updated_at=datetime.fromisoformat(data["updated_at"])
                if "updated_at" in data else datetime.utcnow(),
        )


@dataclass
class AgentContext:
    """Execution context for a workflow.

    Maintains the workflow state, variables, and execution history
    for a workflow run.

    Attributes:
        workflow_id: Unique identifier for the workflow run.
        variables: Workflow variables.
        history: Execution history of nodes/agents.
        parent_context: Optional parent context for nested workflows.
        start_time: When the workflow started.
        end_time: When the workflow ended (if completed).
    """
    workflow_id: str
    variables: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    parent_context: Optional["AgentContext"] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    def set_variable(self, key: str, value: Any) -> None:
        """Set a workflow variable.

        Args:
            key: The variable name.
            value: The variable value.
        """
        self.variables[key] = value

    def get_variable(self, key: str, default: Any = None) -> Any:
        """Get a workflow variable.

        Args:
            key: The variable name.
            default: Default value if not found.

        Returns:
            The variable value or default.
        """
        if key in self.variables:
            return self.variables[key]
        if self.parent_context:
            return self.parent_context.get_variable(key, default)
        return default

    def has_variable(self, key: str) -> bool:
        """Check if a variable exists.

        Args:
            key: The variable name.

        Returns:
            True if the variable exists.
        """
        if key in self.variables:
            return True
        if self.parent_context:
            return self.parent_context.has_variable(key)
        return False

    def delete_variable(self, key: str) -> bool:
        """Delete a variable.

        Args:
            key: The variable name.

        Returns:
            True if the variable was deleted.
        """
        if key in self.variables:
            del self.variables[key]
            return True
        return False

    def add_history_entry(
        self,
        node_id: str,
        action: str,
        result: Optional[Any] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add an entry to the execution history.

        Args:
            node_id: The node/agent ID.
            action: The action performed.
            result: The result of the action.
            error: Error message if failed.
            metadata: Additional metadata.
        """
        entry = {
            "node_id": node_id,
            "action": action,
            "timestamp": datetime.utcnow().isoformat(),
            "elapsed": time.time() - self.start_time,
        }
        if result is not None:
            entry["result"] = result
        if error is not None:
            entry["error"] = error
        if metadata:
            entry["metadata"] = metadata

        self.history.append(entry)

    def get_history(
        self,
        node_id: Optional[str] = None,
        action: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get history entries.

        Args:
            node_id: Optional filter by node ID.
            action: Optional filter by action.

        Returns:
            List of matching history entries.
        """
        entries = self.history
        if node_id:
            entries = [e for e in entries if e.get("node_id") == node_id]
        if action:
            entries = [e for e in entries if e.get("action") == action]
        return entries

    def get_last_entry(self) -> Optional[Dict[str, Any]]:
        """Get the last history entry.

        Returns:
            The last entry or None.
        """
        return self.history[-1] if self.history else None

    def complete(self) -> None:
        """Mark the workflow as completed."""
        self.end_time = time.time()

    @property
    def elapsed_time(self) -> float:
        """Get elapsed time in seconds."""
        end = self.end_time or time.time()
        return end - self.start_time

    @property
    def is_completed(self) -> bool:
        """Check if the workflow is completed."""
        return self.end_time is not None

    def create_child_context(self, workflow_id: str) -> "AgentContext":
        """Create a child context for nested workflows.

        Args:
            workflow_id: The child workflow ID.

        Returns:
            A new child context.
        """
        return AgentContext(
            workflow_id=workflow_id,
            variables={},  # Child starts with empty variables
            history=[],
            parent_context=self,
            start_time=time.time()
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert context to dictionary."""
        return {
            "workflow_id": self.workflow_id,
            "variables": self.variables,
            "history": self.history,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "elapsed_time": self.elapsed_time,
            "is_completed": self.is_completed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentContext":
        """Create context from dictionary."""
        return cls(
            workflow_id=data["workflow_id"],
            variables=data.get("variables", {}),
            history=data.get("history", []),
            start_time=data.get("start_time", time.time()),
            end_time=data.get("end_time"),
        )
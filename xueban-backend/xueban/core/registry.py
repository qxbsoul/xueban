"""
Registry module for AgentGraph.

This module provides the node registry for managing and discovering nodes.
"""

from typing import Dict, List, Optional, Type, Any, Callable, TypeVar
from dataclasses import dataclass, field
import threading
import inspect

from agentgraph.core.node import BaseNode, NodeConfig
from agentgraph.core.errors import (
    ValidationError,
    NodeError,
    raise_if_empty,
    validate_type,
)


T = TypeVar('T')


@dataclass
class RegistryEntry:
    """Represents an entry in the registry.

    Attributes:
        node_class: The node class.
        config: Default configuration for the node.
        factory: Optional factory function to create instances.
        metadata: Additional metadata.
        tags: Tags for categorization.
    """
    node_class: Type[BaseNode]
    config: NodeConfig
    factory: Optional[Callable] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)

    def create_instance(self, **kwargs) -> BaseNode:
        """Create an instance of the node.

        Args:
            **kwargs: Arguments to pass to the constructor.

        Returns:
            A new node instance.
        """
        if self.factory:
            return self.factory(**kwargs)

        config = self.config
        if 'config' in kwargs:
            config = kwargs.pop('config')
        elif 'name' in kwargs or 'description' in kwargs:
            config = NodeConfig(
                id=kwargs.get('id', self.config.id),
                name=kwargs.get('name', self.config.name),
                description=kwargs.get('description', self.config.description),
                tags=kwargs.get('tags', self.config.tags),
                metadata=kwargs.get('metadata', self.config.metadata),
            )

        return self.node_class(config=config, **kwargs)


class NodeRegistry:
    """Registry for managing node types.

    The registry provides a central location for registering, discovering,
    and instantiating nodes.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls) -> "NodeRegistry":
        """Singleton pattern to ensure only one registry exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the registry."""
        if self._initialized:
            return

        self._entries: Dict[str, RegistryEntry] = {}
        self._name_index: Dict[str, str] = {}  # name -> id mapping
        self._tag_index: Dict[str, List[str]] = {}  # tag -> [ids] mapping
        self._initialized = True

    def register(
        self,
        node_class: Type[BaseNode],
        name: Optional[str] = None,
        description: str = "",
        tags: Optional[List[str]] = None,
        factory: Optional[Callable] = None,
        metadata: Optional[Dict[str, Any]] = None,
        override: bool = False
    ) -> str:
        """Register a node class.

        Args:
            node_class: The node class to register.
            name: Optional name (defaults to class name).
            description: Optional description.
            tags: Optional tags for categorization.
            factory: Optional factory function.
            metadata: Optional metadata.
            override: Whether to override existing registration.

        Returns:
            The registered node ID.

        Raises:
            ValidationError: If node already registered and override is False.
        """
        validate_type(node_class, type, "node_class")

        # Create config
        config = NodeConfig(
            name=name or node_class.__name__,
            description=description or node_class.__doc__ or "",
            tags=tags or [],
            metadata=metadata or {}
        )

        node_id = config.id

        # Check for existing registration
        if node_id in self._entries and not override:
            raise ValidationError(
                f"Node '{node_id}' is already registered. Use override=True to replace.",
                details={"node_id": node_id}
            )

        # Check for name collision
        if config.name in self._name_index and not override:
            existing_id = self._name_index[config.name]
            if existing_id != node_id:
                raise ValidationError(
                    f"Node name '{config.name}' is already registered.",
                    details={"name": config.name, "existing_id": existing_id}
                )

        # Create entry
        entry = RegistryEntry(
            node_class=node_class,
            config=config,
            factory=factory,
            metadata=metadata or {},
            tags=tags or []
        )

        # Register
        self._entries[node_id] = entry
        self._name_index[config.name] = node_id

        # Update tag index
        for tag in tags or []:
            if tag not in self._tag_index:
                self._tag_index[tag] = []
            if node_id not in self._tag_index[tag]:
                self._tag_index[tag].append(node_id)

        return node_id

    def unregister(self, node_id: str) -> bool:
        """Unregister a node.

        Args:
            node_id: The node ID to unregister.

        Returns:
            True if the node was unregistered, False if not found.
        """
        if node_id not in self._entries:
            return False

        entry = self._entries[node_id]

        # Remove from indexes
        if entry.config.name in self._name_index:
            del self._name_index[entry.config.name]

        for tag in entry.tags:
            if tag in self._tag_index and node_id in self._tag_index[tag]:
                self._tag_index[tag].remove(node_id)

        # Remove entry
        del self._entries[node_id]

        return True

    def get(self, node_id: str) -> Optional[RegistryEntry]:
        """Get a registry entry by ID.

        Args:
            node_id: The node ID.

        Returns:
            The registry entry, or None if not found.
        """
        return self._entries.get(node_id)

    def get_by_name(self, name: str) -> Optional[RegistryEntry]:
        """Get a registry entry by name.

        Args:
            name: The node name.

        Returns:
            The registry entry, or None if not found.
        """
        node_id = self._name_index.get(name)
        if node_id:
            return self._entries.get(node_id)
        return None

    def create(
        self,
        node_id: str,
        **kwargs
    ) -> BaseNode:
        """Create a node instance by ID.

        Args:
            node_id: The node ID.
            **kwargs: Arguments to pass to the constructor.

        Returns:
            A new node instance.

        Raises:
            NodeError: If node not found.
        """
        entry = self.get(node_id)
        if entry is None:
            raise NodeError(
                f"Node '{node_id}' not found in registry",
                code=None,
                details={"node_id": node_id}
            )
        return entry.create_instance(**kwargs)

    def create_by_name(
        self,
        name: str,
        **kwargs
    ) -> BaseNode:
        """Create a node instance by name.

        Args:
            name: The node name.
            **kwargs: Arguments to pass to the constructor.

        Returns:
            A new node instance.

        Raises:
            NodeError: If node not found.
        """
        entry = self.get_by_name(name)
        if entry is None:
            raise NodeError(
                f"Node '{name}' not found in registry",
                code=None,
                details={"name": name}
            )
        return entry.create_instance(**kwargs)

    def list(self) -> List[str]:
        """List all registered node IDs.

        Returns:
            List of node IDs.
        """
        return list(self._entries.keys())

    def list_names(self) -> List[str]:
        """List all registered node names.

        Returns:
            List of node names.
        """
        return list(self._name_index.keys())

    def list_by_tag(self, tag: str) -> List[str]:
        """List node IDs by tag.

        Args:
            tag: The tag to filter by.

        Returns:
            List of node IDs with the tag.
        """
        return self._tag_index.get(tag, []).copy()

    def list_tags(self) -> List[str]:
        """List all tags.

        Returns:
            List of tags.
        """
        return list(self._tag_index.keys())

    def search(
        self,
        query: str,
        search_description: bool = True
    ) -> List[RegistryEntry]:
        """Search for nodes by name or description.

        Args:
            query: The search query.
            search_description: Whether to search in descriptions.

        Returns:
            List of matching entries.
        """
        query = query.lower()
        results = []

        for entry in self._entries.values():
            if query in entry.config.name.lower():
                results.append(entry)
            elif search_description and query in entry.config.description.lower():
                results.append(entry)

        return results

    def clear(self) -> None:
        """Clear all registrations."""
        self._entries.clear()
        self._name_index.clear()
        self._tag_index.clear()

    def get_info(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a registered node.

        Args:
            node_id: The node ID.

        Returns:
            Dictionary with node information, or None if not found.
        """
        entry = self.get(node_id)
        if entry is None:
            return None

        return {
            "id": node_id,
            "name": entry.config.name,
            "description": entry.config.description,
            "tags": entry.tags,
            "metadata": entry.metadata,
            "class": entry.node_class.__name__,
            "module": entry.node_class.__module__,
        }

    def get_all_info(self) -> List[Dict[str, Any]]:
        """Get information about all registered nodes.

        Returns:
            List of dictionaries with node information.
        """
        return [
            self.get_info(node_id)
            for node_id in self._entries.keys()
        ]

    def __contains__(self, node_id: str) -> bool:
        """Check if a node is registered."""
        return node_id in self._entries

    def __len__(self) -> int:
        """Get the number of registered nodes."""
        return len(self._entries)

    def __iter__(self):
        """Iterate over registered node IDs."""
        return iter(self._entries.keys())


# Global registry instance
_global_registry: Optional[NodeRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> NodeRegistry:
    """Get the global node registry instance."""
    global _global_registry
    if _global_registry is None:
        with _registry_lock:
            if _global_registry is None:
                _global_registry = NodeRegistry()
    return _global_registry


def register_node(
    name: Optional[str] = None,
    description: str = "",
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None
):
    """Decorator to register a node class.

    Args:
        name: Optional name (defaults to class name).
        description: Optional description.
        tags: Optional tags for categorization.
        metadata: Optional metadata.

    Returns:
        Decorator function.
    """
    def decorator(cls: Type[BaseNode]) -> Type[BaseNode]:
        registry = get_registry()
        registry.register(
            node_class=cls,
            name=name,
            description=description,
            tags=tags,
            metadata=metadata
        )
        return cls

    return decorator


# Convenience functions

def create_node(name: str, **kwargs) -> BaseNode:
    """Create a node instance by name.

    Args:
        name: The node name.
        **kwargs: Arguments to pass to the constructor.

    Returns:
        A new node instance.
    """
    return get_registry().create_by_name(name, **kwargs)


def list_nodes() -> List[str]:
    """List all registered node names."""
    return get_registry().list_names()


def node_exists(name: str) -> bool:
    """Check if a node is registered."""
    return get_registry().get_by_name(name) is not None

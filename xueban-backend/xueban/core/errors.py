"""
Error handling module for AgentGraph.

This module provides error classes, logging, recovery mechanisms,
and the circuit breaker pattern for robust error handling.
"""

from typing import Optional, Any, Callable, Type, List, Dict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import logging
import traceback
import asyncio
from functools import wraps


class ErrorCode(Enum):
    """Standard error codes for AgentGraph."""
    # General errors
    UNKNOWN = "E000"
    INTERNAL_ERROR = "E001"

    # Validation errors (1xx)
    VALIDATION_ERROR = "E100"
    INVALID_INPUT = "E101"
    INVALID_OUTPUT = "E102"
    INVALID_CONFIG = "E103"
    MISSING_REQUIRED = "E104"
    TYPE_MISMATCH = "E105"
    EMPTY_VALUE = "E106"
    LENGTH_EXCEEDED = "E107"

    # Execution errors (2xx)
    EXECUTION_ERROR = "E200"
    EXECUTION_TIMEOUT = "E201"
    EXECUTION_CANCELLED = "E202"
    EXECUTION_FAILED = "E203"
    RESOURCE_EXHAUSTED = "E204"

    # Node errors (3xx)
    NODE_NOT_FOUND = "E300"
    NODE_EXECUTION_FAILED = "E301"
    NODE_VALIDATION_FAILED = "E302"
    NODE_TIMEOUT = "E303"

    # Graph errors (4xx)
    GRAPH_CYCLE = "E400"
    GRAPH_INVALID_EDGE = "E401"
    GRAPH_NO_PATH = "E402"

    # Agent errors (5xx)
    AGENT_NOT_FOUND = "E500"
    AGENT_EXECUTION_FAILED = "E501"
    AGENT_TIMEOUT = "E502"
    AGENT_BID_FAILED = "E503"

    # Memory errors (6xx)
    MEMORY_ERROR = "E600"
    MEMORY_READ_ERROR = "E601"
    MEMORY_WRITE_ERROR = "E602"

    # Circuit breaker errors (7xx)
    CIRCUIT_OPEN = "E700"
    CIRCUIT_TIMEOUT = "E701"


class AgentGraphError(Exception):
    """Base exception class for AgentGraph.

    All custom exceptions in AgentGraph should inherit from this class.

    Attributes:
        code: Error code for categorization.
        message: Human-readable error message.
        details: Additional error details.
    """

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.UNKNOWN,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.timestamp = datetime.utcnow()

    @property
    def code_value(self) -> str:
        """Get the string value of the error code."""
        return self.code.value

    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary representation."""
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
            "type": self.__class__.__name__,
        }

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.code.value}, message={self.message!r})"


class ValidationError(AgentGraphError):
    """Exception raised for validation errors."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.VALIDATION_ERROR,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message, code, details)


class ExecutionError(AgentGraphError):
    """Exception raised for execution errors."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.EXECUTION_ERROR,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message, code, details)


class NodeError(AgentGraphError):
    """Exception raised for node-related errors."""

    def __init__(
        self,
        message: str,
        node_id: Optional[str] = None,
        code: ErrorCode = ErrorCode.NODE_EXECUTION_FAILED,
        details: Optional[Dict[str, Any]] = None
    ):
        details = details or {}
        if node_id:
            details["node_id"] = node_id
        super().__init__(message, code, details)


class CircuitBreakerError(AgentGraphError):
    """Exception raised when circuit breaker is open."""

    def __init__(
        self,
        message: str = "Circuit breaker is open",
        code: ErrorCode = ErrorCode.CIRCUIT_OPEN,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message, code, details)


# Validation utility functions

def raise_if_empty(
    value: Any,
    name: str,
    message: Optional[str] = None
) -> Any:
    """Raise ValidationError if value is empty.

    Args:
        value: The value to check.
        name: Name of the parameter for error message.
        message: Optional custom error message.

    Returns:
        The value if not empty.

    Raises:
        ValidationError: If value is empty.
    """
    if value is None or (hasattr(value, '__len__') and len(value) == 0):
        raise ValidationError(
            message or f"'{name}' cannot be empty",
            code=ErrorCode.EMPTY_VALUE,
            details={"parameter": name, "value": repr(value)}
        )
    return value


def raise_if_length(
    value: Any,
    max_length: int,
    name: str,
    message: Optional[str] = None
) -> Any:
    """Raise ValidationError if value exceeds maximum length.

    Args:
        value: The value to check.
        max_length: Maximum allowed length.
        name: Name of the parameter for error message.
        message: Optional custom error message.

    Returns:
        The value if within length limit.

    Raises:
        ValidationError: If value exceeds maximum length.
    """
    if hasattr(value, '__len__') and len(value) > max_length:
        raise ValidationError(
            message or f"'{name}' exceeds maximum length of {max_length}",
            code=ErrorCode.LENGTH_EXCEEDED,
            details={
                "parameter": name,
                "max_length": max_length,
                "actual_length": len(value)
            }
        )
    return value


def validate_type(
    value: Any,
    expected_type: Type,
    name: str,
    message: Optional[str] = None
) -> Any:
    """Validate that value is of expected type.

    Args:
        value: The value to check.
        expected_type: Expected type or tuple of types.
        name: Name of the parameter for error message.
        message: Optional custom error message.

    Returns:
        The value if type is correct.

    Raises:
        ValidationError: If value is not of expected type.
    """
    if not isinstance(value, expected_type):
        raise ValidationError(
            message or f"'{name}' must be of type {expected_type.__name__}",
            code=ErrorCode.TYPE_MISMATCH,
            details={
                "parameter": name,
                "expected_type": expected_type.__name__,
                "actual_type": type(value).__name__
            }
        )
    return value


def validate_required(
    value: Any,
    name: str,
    message: Optional[str] = None
) -> Any:
    """Validate that a required value is provided.

    Args:
        value: The value to check.
        name: Name of the parameter for error message.
        message: Optional custom error message.

    Returns:
        The value if provided.

    Raises:
        ValidationError: If value is None.
    """
    if value is None:
        raise ValidationError(
            message or f"'{name}' is required",
            code=ErrorCode.MISSING_REQUIRED,
            details={"parameter": name}
        )
    return value


def validate_range(
    value: Any,
    min_value: Optional[Any] = None,
    max_value: Optional[Any] = None,
    name: str = "value",
    message: Optional[str] = None
) -> Any:
    """Validate that value is within a specified range.

    Args:
        value: The value to check.
        min_value: Minimum allowed value (inclusive).
        max_value: Maximum allowed value (inclusive).
        name: Name of the parameter for error message.
        message: Optional custom error message.

    Returns:
        The value if within range.

    Raises:
        ValidationError: If value is out of range.
    """
    if min_value is not None and value < min_value:
        raise ValidationError(
            message or f"'{name}' must be >= {min_value}",
            code=ErrorCode.VALIDATION_ERROR,
            details={"parameter": name, "min": min_value, "value": value}
        )
    if max_value is not None and value > max_value:
        raise ValidationError(
            message or f"'{name}' must be <= {max_value}",
            code=ErrorCode.VALIDATION_ERROR,
            details={"parameter": name, "max": max_value, "value": value}
        )
    return value


@dataclass
class ErrorLog:
    """Represents a single error log entry."""
    error: AgentGraphError
    context: Dict[str, Any] = field(default_factory=dict)
    stack_trace: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


class ErrorLogger:
    """Logger for tracking and recording errors.

    This class provides centralized error logging with context
    tracking and history management.
    """

    def __init__(
        self,
        name: str = "agentgraph",
        max_history: int = 1000,
        level: int = logging.ERROR
    ):
        """Initialize the error logger.

        Args:
            name: Logger name.
            max_history: Maximum number of errors to keep in history.
            level: Logging level.
        """
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._max_history = max_history
        self._error_history: List[ErrorLog] = []
        self._error_counts: Dict[ErrorCode, int] = {}

        # Add console handler if not present
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(level)
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)

    def log(
        self,
        error: AgentGraphError,
        context: Optional[Dict[str, Any]] = None,
        include_trace: bool = True
    ) -> None:
        """Log an error.

        Args:
            error: The error to log.
            context: Optional context information.
            include_trace: Whether to include stack trace.
        """
        # Create log entry
        log_entry = ErrorLog(
            error=error,
            context=context or {},
            stack_trace=traceback.format_exc() if include_trace else ""
        )

        # Add to history
        self._error_history.append(log_entry)
        if len(self._error_history) > self._max_history:
            self._error_history.pop(0)

        # Update counts
        self._error_counts[error.code] = self._error_counts.get(error.code, 0) + 1

        # Log to Python logger
        self._logger.error(
            str(error),
            extra={
                "error_code": error.code.value,
                "error_details": error.details,
                "context": context or {}
            }
        )

    def get_history(
        self,
        code: Optional[ErrorCode] = None,
        limit: int = 100
    ) -> List[ErrorLog]:
        """Get error history.

        Args:
            code: Optional error code to filter by.
            limit: Maximum number of entries to return.

        Returns:
            List of error log entries.
        """
        history = self._error_history
        if code:
            history = [e for e in history if e.error.code == code]
        return history[-limit:]

    def get_counts(self) -> Dict[ErrorCode, int]:
        """Get error counts by code."""
        return self._error_counts.copy()

    def clear_history(self) -> None:
        """Clear error history."""
        self._error_history.clear()

    def reset_counts(self) -> None:
        """Reset error counts."""
        self._error_counts.clear()


class ErrorHandler:
    """Handler for error recovery and retry logic.

    Provides mechanisms for handling errors gracefully with
    retry strategies and fallback functions.
    """

    def __init__(
        self,
        logger: Optional[ErrorLogger] = None,
        default_retries: int = 3,
        default_retry_delay: float = 1.0
    ):
        """Initialize the error handler.

        Args:
            logger: Optional error logger.
            default_retries: Default number of retries.
            default_retry_delay: Default delay between retries in seconds.
        """
        self._logger = logger or ErrorLogger()
        self._default_retries = default_retries
        self._default_retry_delay = default_retry_delay
        self._fallback_handlers: Dict[Type[Exception], Callable] = {}

    def register_fallback(
        self,
        exception_type: Type[Exception],
        handler: Callable
    ) -> None:
        """Register a fallback handler for an exception type.

        Args:
            exception_type: The exception type to handle.
            handler: The fallback handler function.
        """
        self._fallback_handlers[exception_type] = handler

    def handle(
        self,
        error: Exception,
        context: Optional[Dict[str, Any]] = None
    ) -> Any:
        """Handle an error using registered fallbacks.

        Args:
            error: The error to handle.
            context: Optional context information.

        Returns:
            Result from the fallback handler if available.

        Raises:
            The original error if no fallback is registered.
        """
        # Log the error
        if isinstance(error, AgentGraphError):
            self._logger.log(error, context)
        else:
            wrapped = AgentGraphError(
                str(error),
                code=ErrorCode.INTERNAL_ERROR,
                details={"original_type": type(error).__name__}
            )
            self._logger.log(wrapped, context)

        # Try fallback handlers
        for exc_type, handler in self._fallback_handlers.items():
            if isinstance(error, exc_type):
                return handler(error, context)

        # Re-raise if no handler
        raise error

    def with_retry(
        self,
        retries: Optional[int] = None,
        delay: Optional[float] = None,
        exceptions: tuple = (Exception,)
    ):
        """Decorator to add retry logic to a function.

        Args:
            retries: Number of retries (default from handler).
            delay: Delay between retries (default from handler).
            exceptions: Exception types to retry on.

        Returns:
            Decorator function.
        """
        retries = retries if retries is not None else self._default_retries
        delay = delay if delay is not None else self._default_retry_delay

        def decorator(func: Callable) -> Callable:
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_error = None
                for attempt in range(retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except exceptions as e:
                        last_error = e
                        if attempt < retries:
                            await asyncio.sleep(delay * (attempt + 1))
                raise last_error

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                last_error = None
                import time
                for attempt in range(retries + 1):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        last_error = e
                        if attempt < retries:
                            time.sleep(delay * (attempt + 1))
                raise last_error

            import asyncio
            if asyncio.iscoroutinefunction(func):
                return async_wrapper
            return sync_wrapper

        return decorator


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitStats:
    """Statistics for circuit breaker."""
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None


class CircuitBreaker:
    """Circuit breaker pattern implementation.

    Prevents cascading failures by stopping requests to a failing service.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        success_threshold: int = 3,
        timeout: float = 60.0,
        logger: Optional[ErrorLogger] = None
    ):
        """Initialize the circuit breaker.

        Args:
            name: Name for identification.
            failure_threshold: Number of failures before opening.
            success_threshold: Number of successes before closing.
            timeout: Time in seconds before attempting recovery.
            logger: Optional error logger.
        """
        self.name = name
        self._failure_threshold = failure_threshold
        self._success_threshold = success_threshold
        self._timeout = timedelta(seconds=timeout)
        self._logger = logger or ErrorLogger()

        self._state = CircuitState.CLOSED
        self._stats = CircuitStats()
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (failing)."""
        return self._state == CircuitState.OPEN

    @property
    def is_half_open(self) -> bool:
        """Check if circuit is half-open (testing)."""
        return self._state == CircuitState.HALF_OPEN

    def _should_attempt_recovery(self) -> bool:
        """Check if enough time has passed to attempt recovery."""
        if self._stats.last_failure_time is None:
            return False
        return datetime.utcnow() - self._stats.last_failure_time > self._timeout

    async def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state."""
        old_state = self._state
        self._state = new_state

        if new_state == CircuitState.CLOSED:
            self._stats.failure_count = 0
            self._stats.success_count = 0
        elif new_state == CircuitState.OPEN:
            self._stats.success_count = 0

        self._logger._logger.info(
            f"Circuit breaker '{self.name}' transitioned from {old_state.value} to {new_state.value}"
        )

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute a function through the circuit breaker.

        Args:
            func: The function to execute.
            *args: Function arguments.
            **kwargs: Function keyword arguments.

        Returns:
            The function result.

        Raises:
            CircuitBreakerError: If circuit is open.
        """
        async with self._lock:
            # Check if we should attempt recovery
            if self._state == CircuitState.OPEN:
                if self._should_attempt_recovery():
                    await self._transition_to(CircuitState.HALF_OPEN)
                else:
                    raise CircuitBreakerError(
                        f"Circuit breaker '{self.name}' is open",
                        details={"name": self.name, "state": self._state.value}
                    )

        # Execute the function
        try:
            import asyncio
            import inspect

            if inspect.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            await self._on_success()
            return result

        except Exception as e:
            await self._on_failure(e)
            raise

    async def _on_success(self) -> None:
        """Handle successful execution."""
        async with self._lock:
            self._stats.success_count += 1
            self._stats.last_success_time = datetime.utcnow()

            if self._state == CircuitState.HALF_OPEN:
                if self._stats.success_count >= self._success_threshold:
                    await self._transition_to(CircuitState.CLOSED)

    async def _on_failure(self, error: Exception) -> None:
        """Handle failed execution."""
        async with self._lock:
            self._stats.failure_count += 1
            self._stats.last_failure_time = datetime.utcnow()

            if isinstance(error, AgentGraphError):
                self._logger.log(error, {"circuit_breaker": self.name})
            else:
                wrapped = AgentGraphError(
                    str(error),
                    code=ErrorCode.EXECUTION_FAILED,
                    details={"circuit_breaker": self.name}
                )
                self._logger.log(wrapped, {"circuit_breaker": self.name})

            if self._state == CircuitState.HALF_OPEN:
                await self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._stats.failure_count >= self._failure_threshold:
                    await self._transition_to(CircuitState.OPEN)

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._stats.failure_count,
            "success_count": self._stats.success_count,
            "last_failure_time": (
                self._stats.last_failure_time.isoformat()
                if self._stats.last_failure_time else None
            ),
            "last_success_time": (
                self._stats.last_success_time.isoformat()
                if self._stats.last_success_time else None
            ),
        }

    async def reset(self) -> None:
        """Reset the circuit breaker."""
        async with self._lock:
            await self._transition_to(CircuitState.CLOSED)
            self._stats = CircuitStats()


def circuit_breaker(
    name: str = "default",
    failure_threshold: int = 5,
    success_threshold: int = 3,
    timeout: float = 60.0
):
    """Decorator to wrap a function with a circuit breaker.

    Args:
        name: Name for identification.
        failure_threshold: Number of failures before opening.
        success_threshold: Number of successes before closing.
        timeout: Time in seconds before attempting recovery.

    Returns:
        Decorator function.
    """
    breaker = CircuitBreaker(
        name=name,
        failure_threshold=failure_threshold,
        success_threshold=success_threshold,
        timeout=timeout
    )

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            return await breaker.call(func, *args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            import asyncio
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(breaker.call(func, *args, **kwargs))

        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# Global error logger instance
_global_logger: Optional[ErrorLogger] = None


def get_error_logger() -> ErrorLogger:
    """Get the global error logger instance."""
    global _global_logger
    if _global_logger is None:
        _global_logger = ErrorLogger()
    return _global_logger


def set_error_logger(logger: ErrorLogger) -> None:
    """Set the global error logger instance."""
    global _global_logger
    _global_logger = logger

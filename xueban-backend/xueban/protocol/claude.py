from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from enum import Enum
import asyncio


class FallbackStrategy(Enum):
    RETRY = "retry"
    CACHE = "cache"
    DELEGATE = "delegate"


@dataclass
class FallbackConfig:
    strategy: FallbackStrategy
    max_retries: int = 3
    delegate_to: Optional[str] = None
    cache_ttl: int = 3600


class FallbackChain:
    def __init__(self, configs: List[FallbackConfig]):
        self.configs = configs
        self._cache: Dict[str, Any] = {}
        self._retry_count = 0

    async def execute(
        self,
        func: Callable,
        *args,
        **kwargs
    ) -> tuple[bool, Any, str]:
        """
        执行函数，失败时按配置的策略降级。
        返回: (success, result, strategy_used)
        """
        for config in self.configs:
            try:
                if config.strategy == FallbackStrategy.RETRY:
                    result = await self._retry(func, config.max_retries, *args, **kwargs)
                    return True, result, "retry"
                elif config.strategy == FallbackStrategy.CACHE:
                    result = await self._from_cache_or_execute(func, *args, **kwargs)
                    return True, result, "cache"
                elif config.strategy == FallbackStrategy.DELEGATE:
                    result = await self._delegate(config.delegate_to, *args, **kwargs)
                    return True, result, "delegate"
            except Exception as e:
                continue

        return False, None, "failed"

    async def _retry(self, func, max_retries, *args, **kwargs):
        last_error = None
        for i in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e
                await asyncio.sleep(2 ** i)  # 指数退避
        raise last_error

    async def _from_cache_or_execute(self, func, *args, **kwargs):
        key = str((args, sorted(kwargs.items())))
        if key in self._cache:
            return self._cache[key]
        result = await func(*args, **kwargs)
        self._cache[key] = result
        return result

    async def _delegate(self, delegate_to, *args, **kwargs):
        # 由 AgentGraph 调用其他 agent
        raise NotImplementedError("Delegation requires AgentGraph context")


@dataclass
class ReasoningTrace:
    agent_id: str
    step: int
    reasoning: str
    action: Dict[str, Any]
    confidence: float
    timestamp: float

class ClaudeProtocol:
    def __init__(
        self,
        enable_trace: bool = True,
        enable_validation: bool = True,
        fallback_chain: Optional[FallbackChain] = None
    ):
        self.enable_trace = enable_trace
        self.enable_validation = enable_validation
        self.fallback_chain = fallback_chain
        self._traces: list[ReasoningTrace] = []
    
    def create_trace(
        self,
        agent_id: str,
        step: int,
        reasoning: str,
        action: Dict[str, Any],
        confidence: float
    ) -> ReasoningTrace:
        import time
        trace = ReasoningTrace(
            agent_id=agent_id,
            step=step,
            reasoning=reasoning,
            action=action,
            confidence=confidence,
            timestamp=time.time()
        )
        
        if self.enable_trace:
            self._traces.append(trace)
        
        return trace
    
    def get_traces(self) -> list[ReasoningTrace]:
        return self._traces
    
    def clear_traces(self):
        self._traces.clear()
    
    def format_trace(self) -> str:
        if not self._traces:
            return "No reasoning traces available."
        
        output = "Reasoning Trace:\n"
        for trace in self._traces:
            output += f"\n[Step {trace.step}] Agent: {trace.agent_id}\n"
            output += f"Reasoning: {trace.reasoning}\n"
            output += f"Confidence: {trace.confidence:.2f}\n"
            output += f"Action: {trace.action}\n"
        
        return output
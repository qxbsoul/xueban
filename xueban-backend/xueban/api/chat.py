"""
Chat Completions API — BYOK 模式

POST /api/chat/completions
Request headers:
  X-LLM-Key: <api key>
  X-LLM-Provider: zhipu | openai | claude | ollama (default: zhipu)
  X-LLM-Model: <model name> (optional, default per provider)

Request body:
  {
    "messages": [{"role": "user|assistant|system", "content": "..."}],
    "agent_id": "...",          # optional, to load agent soul as system prompt
    "stream": false              # optional, default false
  }

Response:
  {"data": {"content": "...", "model": "...", "provider": "...", "tokens_used": 0}, "meta": {}}
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Allowlist for agent_id to prevent path traversal
_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

DEFAULT_MODELS = {
    "zhipu": "glm-4-flash",
    "openai": "gpt-4o-mini",
    "claude": "claude-3-5-haiku-20241022",
    "ollama": "llama3.2",
    "deepseek": "deepseek-chat",
}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: Union[str, List[Any]]


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    agent_id: Optional[str] = None
    stream: bool = False


def _envelope(data: Any, **meta: Any) -> Dict[str, Any]:
    return {"data": data, "meta": meta}


@router.post("/completions")
async def chat_completions(
    body: ChatRequest,
    x_llm_key: Optional[str] = Header(None, alias="X-LLM-Key"),
    x_llm_provider: Optional[str] = Header(None, alias="X-LLM-Provider"),
    x_llm_model: Optional[str] = Header(None, alias="X-LLM-Model"),
) -> Dict[str, Any]:
    provider_name = (x_llm_provider or "zhipu").lower()

    if not x_llm_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "NO_API_KEY", "message": "X-LLM-Key header required"}},
        )

    # Lazy import to avoid startup dependency issues
    try:
        from xueban.llm import LLMConfig, create_provider, ProviderType
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "LLM_UNAVAILABLE", "message": str(e)}},
        )

    provider_map = {
        "zhipu": ProviderType.ZHIPU,
        "openai": ProviderType.OPENAI,
        "claude": ProviderType.CLAUDE,
        "ollama": ProviderType.OLLAMA,
        "deepseek": ProviderType.DEEPSEEK,
    }
    ptype = provider_map.get(provider_name)
    if ptype is None:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "UNKNOWN_PROVIDER", "message": f"Unknown provider: {provider_name}"}},
        )

    model = x_llm_model or DEFAULT_MODELS.get(provider_name, "")
    config = LLMConfig(model=model, api_key=x_llm_key)

    # Try to load agent soul if agent_id provided
    if body.agent_id:
        try:
            # Validate agent_id to prevent path traversal attacks
            if not _AGENT_ID_RE.match(body.agent_id):
                raise ValueError(f"Invalid agent_id format: {body.agent_id!r}")
            agents_dir = pathlib.Path(__file__).resolve().parents[2] / ".xueban" / "agents"
            agent_file = agents_dir / f"{body.agent_id}.json"
            if agent_file.exists():
                agent_data = json.loads(agent_file.read_text(encoding="utf-8"))
                soul = agent_data.get("soul", "")
                if soul:
                    # Prepend system message if not already present
                    has_system = any(m.role == "system" for m in body.messages)
                    if not has_system:
                        body.messages.insert(0, ChatMessage(role="system", content=soul))
        except Exception:
            pass  # ignore agent load errors, proceed without soul

    try:
        provider = create_provider(ptype, config)
        msgs = [{"role": m.role, "content": m.content} for m in body.messages]
        response = await provider.chat(msgs)
        return _envelope({
            "content": response.content,
            "model": response.model,
            "provider": provider_name,
            "tokens_used": response.tokens_used,
        })
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"error": {"code": "LLM_ERROR", "message": str(e)}},
        )

"""Model factory - real Anthropic and OpenAI models only.

Newer Claude models (Sonnet 5.5, Opus 5.x, Fable) use adaptive thinking: they reject non-default
temperature and forced tool_choice, so we omit temperature and use JSON-schema structured output.
OpenAI GPT-5.x are reasoning models: give them headroom and low effort for snappy caller turns.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from ..config import Settings

_ADAPTIVE = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-opus-4-8")
_NO_FORCED_TOOL = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1")


def chat_model(name: str, settings: Settings, max_tokens: int = 400, temperature: float = 0.3,
               streaming: bool = True) -> BaseChatModel:
    if name.startswith("claude"):
        from langchain_anthropic import ChatAnthropic
        kw: dict[str, Any] = {}
        if name.startswith(_ADAPTIVE):
            max_tokens = max(max_tokens, 4000)  # thinking shares the output budget
        else:
            kw["temperature"] = temperature
        return ChatAnthropic(model=name, api_key=settings.anthropic_api_key or None,
                             max_tokens=max_tokens, streaming=streaming, max_retries=2,
                             timeout=60, **kw)
    from langchain_openai import ChatOpenAI
    kw = {}
    if name.startswith(("gpt-5", "o1", "o3", "o4")):
        kw["reasoning_effort"] = "low"
        max_tokens = max(max_tokens, 2000)
    else:
        kw["temperature"] = temperature
    return ChatOpenAI(model=name, api_key=settings.openai_api_key or None, max_tokens=max_tokens,
                      streaming=streaming, max_retries=2, timeout=60, **kw)


def structured(model: BaseChatModel, schema: type) -> Any:
    name = getattr(model, "model", "") or getattr(model, "model_name", "") or ""
    if str(name).startswith(_NO_FORCED_TOOL):
        return model.with_structured_output(schema, method="json_schema")
    return model.with_structured_output(schema)

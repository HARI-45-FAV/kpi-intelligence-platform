"""Optional language-model layer.

The platform is a deterministic, governed BI system and remains the source of
truth. This package is the *only* place a model is contacted, and it is optional:
with ``LLM_ENABLED=false`` nothing here opens a connection, and every API,
KPI calculation, validation, connector and dashboard behaves exactly as it does
without it.

Deliberately thin. It knows how to talk to a model endpoint and nothing about
companies, KPIs, permissions or data sources -- those stay with the governed
layers that own them (``app.copilot`` for orchestration, ``app.core.deps`` for
authorisation). Provider-specific behaviour is confined to the transport
modules, so no other part of the codebase branches on which model is deployed.
"""

from app.llm.config import SUPPORTED_PROVIDERS, LLMConfig, get_llm_config, llm_config_from_settings
from app.llm.provider import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    LLMToolSpec,
    LLMUnavailable,
    LLMUsage,
    NullProvider,
    build_provider,
    strip_reasoning,
)

__all__ = [
    "SUPPORTED_PROVIDERS",
    "LLMConfig",
    "LLMMessage",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "LLMToolCall",
    "LLMToolSpec",
    "LLMUnavailable",
    "LLMUsage",
    "NullProvider",
    "build_provider",
    "get_llm_config",
    "llm_config_from_settings",
    "strip_reasoning",
]

"""Provider-independent language-model contract.

Everything above this module -- the Copilot orchestrator, the governed tools,
the API, the frontend -- talks to ``LLMProvider`` and the value objects here.
None of them know whether the answer came from Qwen served by vLLM, from
llama.cpp, or from a hosted API. That is the point: there is no
``if model == "Qwen"`` anywhere outside a transport module, and swapping models
is an environment change rather than a code change.

Two rules this layer enforces on the way back up:

* **No hidden reasoning escapes.** Models in this class emit thinking either in
  ``<think>`` blocks inside the content or in a separate ``reasoning_content``
  field. ``strip_reasoning`` removes the first; transports drop the second and
  never map it onto ``LLMResponse``. Chain-of-thought is not part of the
  contract, so it cannot reach a response body by accident.
* **No model output is trusted as data.** ``LLMResponse.tool_calls`` carries
  requests, not permissions. The tool registry validates arguments and applies
  the caller's company scope; a model asking for another company's KPI gets a
  refusal, not a query.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import PlatformError
from app.llm.config import LLMConfig, get_llm_config

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LLMUnavailable(PlatformError):
    """No model is configured, or the configuration is incomplete.

    A 503 rather than a 500: the deployment is working as designed, it simply
    has no model. The Copilot API prefers reporting this state in a normal
    response body, and only raises when a caller insists on a model answer.
    """

    status_code = 503
    code = "llm_unavailable"


class LLMProviderError(PlatformError):
    """The configured model endpoint failed or answered unusably."""

    status_code = 502
    code = "llm_provider_error"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LLMToolCall:
    """A tool the model *asked* for. Authorisation happens after this."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Set when the model produced malformed JSON arguments; the orchestrator
    # turns this into a refusal instead of guessing what it meant.
    argument_error: str | None = None


@dataclass(frozen=True, slots=True)
class LLMToolSpec:
    """A governed tool offered to the model, described by JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> LLMMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> LLMMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str | None, tool_calls: Sequence[LLMToolCall] = ()) -> LLMMessage:
        return cls(role="assistant", content=content, tool_calls=tuple(tool_calls))

    @classmethod
    def tool_result(cls, *, call_id: str, name: str, payload: Any) -> LLMMessage:
        """A governed tool's structured result, handed back as JSON text."""
        content = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        return cls(role="tool", content=content, tool_call_id=call_id, name=name)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One completed model turn, with reasoning already removed."""

    text: str = ""
    tool_calls: tuple[LLMToolCall, ...] = ()
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    finish_reason: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# ---------------------------------------------------------------------------
# Reasoning scrub
# ---------------------------------------------------------------------------
# Reasoning-capable models in this family wrap deliberation in a tag pair. The
# closing-tag-optional variant matters: a response truncated by a token limit
# can open a block and never close it, and leaking a half-finished thought is
# exactly what must not happen.
_REASONING_BLOCK = re.compile(
    r"<(think|thinking|reasoning|scratchpad)\b[^>]*>.*?(?:</\1\s*>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# A stray closing tag with no opener, left behind by some templates.
_ORPHAN_CLOSE = re.compile(r"</(think|thinking|reasoning|scratchpad)\s*>", re.IGNORECASE)


def strip_reasoning(text: str | None) -> str:
    """Remove model reasoning blocks from visible content.

    Applied by every transport before an ``LLMResponse`` is constructed, so no
    caller can forget it.
    """
    if not text:
        return ""
    cleaned = _REASONING_BLOCK.sub("", text)
    cleaned = _ORPHAN_CLOSE.sub("", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class LLMProvider(ABC):
    """Transport for one model endpoint.

    Implementations own every provider-specific detail: wire format, tool-call
    encoding, streaming frames, error shapes, reasoning-field names. They must
    not reach into the platform database, the connector registry or tenant data
    -- a provider's whole world is the message list it is handed.
    """

    name: str = "abstract"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @abstractmethod
    async def generate(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMToolSpec] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        """Run one model turn.

        ``stream=True`` selects the streaming transport and aggregates the
        deltas into a complete ``LLMResponse``; it does not hand the caller an
        async iterator. The Copilot API is request/response, and pretending
        otherwise would put a partial, unverified answer in front of a user.
        """

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
        return None


class NullProvider(LLMProvider):
    """Stands in when no model is configured.

    Exists so callers can hold a provider object unconditionally and branch on
    availability rather than on ``None``. Calling it raises the same
    ``LLMUnavailable`` the API reports, carrying the configured reason.
    """

    name = "null"

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMToolSpec] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        raise LLMUnavailable(
            self.config.unavailable_reason or "No language model is configured.",
            details={"provider": self.config.provider, "enabled": self.config.enabled},
        )


def build_provider(config: LLMConfig | None = None) -> LLMProvider:
    """Resolve the configured transport. The only provider dispatch in the codebase.

    Returns ``NullProvider`` when no usable model is configured, so importing a
    transport -- and with it ``httpx`` request machinery -- never happens on a
    deployment that runs without a model.
    """
    cfg = config or get_llm_config()
    if not cfg.is_available:
        return NullProvider(cfg)

    if cfg.provider == "openai_compatible":
        # Imported lazily: keeps this module dependency-free and keeps the
        # transport out of the import graph when the Copilot is disabled.
        from app.llm.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(cfg)

    if cfg.provider == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider(cfg)

    # Unreachable while `is_available` gates on SUPPORTED_PROVIDERS, but an
    # explicit failure beats a silent fallback to a different model.
    raise LLMUnavailable(
        f"No transport is registered for LLM_PROVIDER={cfg.provider!r}.",
        details={"provider": cfg.provider},
    )

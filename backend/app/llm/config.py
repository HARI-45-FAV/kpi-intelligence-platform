"""Language-model configuration, resolved from settings at call time.

Two things this module exists to guarantee:

* **The platform runs with no model.** ``LLMConfig.unavailable_reason`` is the
  single source of truth for "can the Copilot answer right now", and callers are
  expected to check it *before* importing or contacting anything. Nothing in
  ``app.llm`` imports a third-party AI package.
* **Credentials never travel.** ``describe()`` returns the shape the API and
  telemetry are allowed to see: provider, model, endpoint host. The API key is
  not in it, and there is no accessor that puts it in a response body, a log
  line or an audit entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.config import Settings, get_settings

# Provider names this build knows how to speak to. Kept here so a bad value in
# the environment is a clear configuration error rather than an import failure
# at the first Copilot request.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai_compatible",)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    enabled: bool
    provider: str
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_output_tokens: int
    request_timeout_seconds: int
    reasoning_effort: str | None
    tool_calling_enabled: bool
    max_tool_iterations: int
    input_cost_per_1k_usd: float
    output_cost_per_1k_usd: float

    # -- availability ----------------------------------------------------
    @property
    def unavailable_reason(self) -> str | None:
        """Why the Copilot cannot use a model, or ``None`` when it can.

        A string here is not an error condition -- it is the normal state of a
        deployment that has not configured a model, and the Copilot surfaces it
        verbatim instead of failing or improvising.
        """
        if not self.enabled:
            return (
                "The AI Copilot is disabled on this deployment (LLM_ENABLED=false). "
                "All governed retrieval, KPI, validation and connector features "
                "continue to work without it."
            )
        if self.provider not in SUPPORTED_PROVIDERS:
            return (
                f"LLM_PROVIDER={self.provider!r} is not a provider this build supports. "
                f"Supported: {', '.join(SUPPORTED_PROVIDERS)}."
            )
        if not self.base_url.strip():
            return "LLM_ENABLED is true but LLM_BASE_URL is empty."
        if not self.model.strip():
            return "LLM_ENABLED is true but LLM_MODEL is empty."
        return None

    @property
    def is_available(self) -> bool:
        return self.unavailable_reason is None

    # -- accounting ------------------------------------------------------
    def estimate_cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Cost for ``execution_logs.estimated_cost_usd``.

        Zero-rated by default, which is the honest answer for a self-hosted
        model: there is no per-token invoice to report.
        """
        cost = (prompt_tokens / 1000.0) * self.input_cost_per_1k_usd + (
            completion_tokens / 1000.0
        ) * self.output_cost_per_1k_usd
        return round(cost, 6)

    # -- safe description ------------------------------------------------
    @property
    def endpoint_host(self) -> str:
        """Host of the model endpoint, with any credential in the URL dropped."""
        parsed = urlparse(self.base_url if "://" in self.base_url else f"//{self.base_url}")
        host = parsed.hostname or ""
        return f"{host}:{parsed.port}" if parsed.port else host

    def describe(self) -> dict[str, object]:
        """Everything a response, log or audit entry may reveal about the model.

        Deliberately excludes ``api_key``. Callers that want to show model
        configuration use this; there is no other approved shape.
        """
        return {
            "enabled": self.enabled,
            "available": self.is_available,
            "provider": self.provider,
            "model": self.model if self.enabled else None,
            "endpoint_host": self.endpoint_host if self.enabled else None,
            "unavailable_reason": self.unavailable_reason,
        }


def llm_config_from_settings(settings: Settings) -> LLMConfig:
    return LLMConfig(
        enabled=settings.llm_enabled,
        provider=settings.llm_provider.strip().lower(),
        base_url=settings.llm_base_url.strip().rstrip("/"),
        api_key=settings.llm_api_key,
        model=settings.llm_model.strip(),
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        request_timeout_seconds=settings.llm_request_timeout_seconds,
        reasoning_effort=(settings.llm_reasoning_effort or "").strip().lower() or None,
        tool_calling_enabled=settings.llm_tool_calling_enabled,
        max_tool_iterations=max(1, settings.llm_max_tool_iterations),
        input_cost_per_1k_usd=settings.llm_input_cost_per_1k_usd,
        output_cost_per_1k_usd=settings.llm_output_cost_per_1k_usd,
    )


def get_llm_config() -> LLMConfig:
    """Read configuration fresh on every call.

    Not cached: an operator flipping ``LLM_ENABLED`` and a test monkeypatching
    settings should both take effect without a process restart being the only
    way to observe the change.
    """
    return llm_config_from_settings(get_settings())

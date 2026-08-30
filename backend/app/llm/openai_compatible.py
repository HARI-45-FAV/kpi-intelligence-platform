"""OpenAI-compatible chat-completions transport.

One transport covers every runtime that serves the OpenAI chat shape -- vLLM,
llama.cpp's server, Ollama, TGI, OpenAI itself -- which is why the default
model (``Qwen/Qwen3-30B-A3B-Instruct-2507`` behind vLLM) needs no code of its
own. All the model-specific handling lives in this file:

* tool calls encoded as ``{"type": "function", "function": {...}}``;
* ``reasoning_content`` (vLLM's separate reasoning channel) read and discarded;
* ``<think>`` blocks stripped from visible content;
* streaming SSE deltas reassembled into one complete response.

Built on ``httpx``, which the platform already depends on for the Supabase REST
connector, so enabling the Copilot installs nothing.

The transport is deliberately incurious about what it is carrying. It receives a
message list, returns a response, and has no access to the platform database,
the connector registry, stored credentials or tenant rows.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from app.llm.config import LLMConfig
from app.llm.provider import (
    LLMMessage,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    LLMToolSpec,
    LLMUsage,
    strip_reasoning,
)

# Fields carrying model deliberation. Read so they can be dropped: never mapped
# onto LLMResponse, never logged.
_REASONING_FIELDS = ("reasoning_content", "reasoning")


def _tool_wire(spec: LLMToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _message_wire(message: LLMMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    # An assistant turn that only requests tools has no content, and some
    # servers reject a missing key while others reject a null one -- "" is
    # accepted by both.
    payload["content"] = message.content if message.content is not None else ""
    if message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id or ""
        if message.name:
            payload["name"] = message.name
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, default=str),
                },
            }
            for call in message.tool_calls
        ]
    return payload


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Decode tool arguments, reporting malformed JSON instead of guessing."""
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, f"arguments were not valid JSON ({exc})"
    if not isinstance(parsed, dict):
        return {}, "arguments were not a JSON object"
    return parsed, None


def _tool_calls_from(raw_calls: Any) -> tuple[LLMToolCall, ...]:
    calls: list[LLMToolCall] = []
    for index, raw in enumerate(raw_calls or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        arguments, error = _parse_arguments(function.get("arguments"))
        calls.append(
            LLMToolCall(
                call_id=str(raw.get("id") or f"call_{index}"),
                name=name,
                arguments=arguments,
                argument_error=error,
            )
        )
    return tuple(calls)


def _usage_from(raw: Any, *, fallback: LLMUsage) -> LLMUsage:
    if not isinstance(raw, dict):
        return fallback
    return LLMUsage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
    )


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    # -- transport -------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            # Local servers are commonly started with no auth and the
            # convention is the literal "EMPTY"; sending it as a bearer token is
            # harmless and keeps one code path.
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                headers=headers,
                timeout=httpx.Timeout(float(self.config.request_timeout_seconds)),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # -- request ---------------------------------------------------------
    def _body(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMToolSpec] | None,
        stream: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [_message_wire(m) for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": stream,
        }
        if tools:
            body["tools"] = [_tool_wire(t) for t in tools]
            # "auto", never "required": a question answerable from retrieved
            # evidence should be answered, not forced through a tool call.
            body["tool_choice"] = "auto"
        if self.config.reasoning_effort:
            # Ollama's OpenAI-compatible API maps ``none`` to disabled
            # thinking for Qwen3.  This leaves response tokens for the answer
            # the user can actually see instead of an internal trace.
            body["reasoning_effort"] = self.config.reasoning_effort
        if stream:
            # vLLM and OpenAI both report usage on the final chunk when asked.
            # Servers that ignore this simply leave usage at zero, which the
            # telemetry records honestly rather than estimating.
            body["stream_options"] = {"include_usage": True}
        return body

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMToolSpec] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        body = self._body(messages, tools, stream)
        try:
            if stream:
                return await self._generate_streaming(body)
            return await self._generate_once(body)
        except httpx.TimeoutException as exc:
            raise LLMProviderError(
                f"The model at {self.config.endpoint_host} did not respond within "
                f"{self.config.request_timeout_seconds}s.",
                details={"provider": self.name, "model": self.config.model},
            ) from exc
        except httpx.HTTPError as exc:
            # Deliberately reports the host, not the URL or headers: a base URL
            # can carry a token and the headers certainly do.
            raise LLMProviderError(
                f"Could not reach the model endpoint at {self.config.endpoint_host}.",
                details={"provider": self.name, "reason": type(exc).__name__},
            ) from exc

    async def _generate_once(self, body: dict[str, Any]) -> LLMResponse:
        response = await self._http().post("/chat/completions", json=body)
        if response.status_code >= 400:
            raise LLMProviderError(
                self._error_message(response),
                details={
                    "provider": self.name,
                    "status": response.status_code,
                    "model": self.config.model,
                },
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMProviderError(
                f"The model at {self.config.endpoint_host} returned a non-JSON response.",
                details={"provider": self.name},
            ) from exc

        choices = payload.get("choices") or []
        if not choices:
            raise LLMProviderError(
                "The model returned no completion choices.",
                details={"provider": self.name, "model": self.config.model},
            )
        message = (choices[0] or {}).get("message") or {}
        # reasoning_content is read only to make it explicit that it is dropped.
        for key in _REASONING_FIELDS:
            message.pop(key, None)

        return LLMResponse(
            text=strip_reasoning(message.get("content")),
            tool_calls=_tool_calls_from(message.get("tool_calls")),
            usage=_usage_from(payload.get("usage"), fallback=LLMUsage()),
            model=str(payload.get("model") or self.config.model),
            finish_reason=(choices[0] or {}).get("finish_reason"),
        )

    async def _generate_streaming(self, body: dict[str, Any]) -> LLMResponse:
        """Consume the SSE stream and return one assembled response.

        Streaming is used for the transport benefit -- a long answer starts
        arriving immediately and the connection stays warm -- but the deltas are
        joined here. Nothing partial is exposed upward, because a half-formed
        answer has not yet been checked against the evidence it must be
        grounded in.
        """
        text_parts: list[str] = []
        # Tool-call fragments arrive indexed, with the name usually in the first
        # frame and arguments accumulating character by character.
        partial_tools: dict[int, dict[str, Any]] = {}
        usage = LLMUsage()
        model = self.config.model
        finish_reason: str | None = None

        async with self._http().stream("POST", "/chat/completions", json=body) as response:
            if response.status_code >= 400:
                await response.aread()
                raise LLMProviderError(
                    self._error_message(response),
                    details={
                        "provider": self.name,
                        "status": response.status_code,
                        "model": self.config.model,
                    },
                )
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    # A malformed frame is skipped rather than failing the turn;
                    # the aggregate is still checked for emptiness below.
                    continue

                model = str(chunk.get("model") or model)
                usage = _usage_from(chunk.get("usage"), fallback=usage)
                for choice in chunk.get("choices") or []:
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    for key in _REASONING_FIELDS:
                        delta.pop(key, None)
                    piece = delta.get("content")
                    if piece:
                        text_parts.append(str(piece))
                    for fragment in delta.get("tool_calls") or []:
                        self._absorb_tool_fragment(partial_tools, fragment)

        return LLMResponse(
            # Stripped after joining: a <think> block can span frames, so
            # scrubbing per delta would miss it.
            text=strip_reasoning("".join(text_parts)),
            tool_calls=_tool_calls_from(
                [partial_tools[index] for index in sorted(partial_tools)]
            ),
            usage=usage,
            model=model,
            finish_reason=finish_reason,
        )

    @staticmethod
    def _absorb_tool_fragment(
        partial: dict[int, dict[str, Any]], fragment: dict[str, Any]
    ) -> None:
        if not isinstance(fragment, dict):
            return
        index = int(fragment.get("index") or 0)
        slot = partial.setdefault(index, {"id": None, "function": {"name": "", "arguments": ""}})
        if fragment.get("id"):
            slot["id"] = fragment["id"]
        function = fragment.get("function") or {}
        if function.get("name"):
            slot["function"]["name"] = function["name"]
        if function.get("arguments"):
            slot["function"]["arguments"] += function["arguments"]

    # -- errors ----------------------------------------------------------
    def _error_message(self, response: httpx.Response) -> str:
        """Summarise a provider failure without echoing request material.

        The response body of a failed model call can contain the prompt, so only
        the provider's own error message is quoted, and only briefly.
        """
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
            elif payload.get("message"):
                detail = str(payload["message"])
        detail = detail.strip()[:300]
        base = (
            f"The model endpoint at {self.config.endpoint_host} returned "
            f"HTTP {response.status_code}"
        )
        if response.status_code in (401, 403):
            return f"{base}. Check LLM_API_KEY for this endpoint."
        if response.status_code == 404:
            return (
                f"{base}. Check LLM_BASE_URL points at an OpenAI-compatible "
                f"path and that model {self.config.model!r} is served there."
            )
        return f"{base}{f': {detail}' if detail else '.'}"

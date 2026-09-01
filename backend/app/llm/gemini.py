"""Google Gemini ``generateContent`` transport.

A second transport behind the same ``LLMProvider`` contract as
``openai_compatible``. Nothing above ``app.llm`` changes when a deployment
switches between them: the orchestrator, the governed tool registry, the
retrieval layer and the API all keep talking to ``LLMProvider``, and the choice
is ``LLM_PROVIDER`` in the environment rather than a code path.

Everything Gemini-shaped is confined to this file:

* messages carry ``role: "user" | "model"`` and a list of ``parts``, so system
  messages move to ``systemInstruction`` and tool results become
  ``functionResponse`` parts matched **by name** rather than by call id;
* tools are declared as ``functionDeclarations`` with an OpenAPI-subset schema,
  so the JSON Schema the registry publishes is translated rather than forwarded
  (``additionalProperties`` and ``$schema`` are rejected by the API);
* deliberation arrives as parts flagged ``thought: true`` -- dropped here, the
  same way ``reasoning_content`` is dropped in the OpenAI-compatible transport;
* streaming uses ``:streamGenerateContent?alt=sse`` and is reassembled into one
  complete response.

Built on ``httpx``, which the platform already depends on: enabling Gemini
installs no Google SDK, and no third-party AI package enters the import graph.

The transport is deliberately incurious about what it is carrying. It receives a
message list, returns a response, and has no access to the platform database,
the connector registry, stored credentials or tenant rows. The API key travels
in a request header and appears in no log line, response body or audit entry --
never in the URL, which is the one part of a failed request that tends to be
echoed.
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

# Keys the Gemini schema dialect accepts. Anything else in a published JSON
# Schema -- ``$schema``, ``additionalProperties``, ``examples`` -- is a 400 from
# the API, so the translation below keeps this set and discards the rest.
_SCHEMA_KEYS = (
    "type",
    "format",
    "description",
    "nullable",
    "enum",
    "items",
    "properties",
    "required",
    "minItems",
    "maxItems",
    "anyOf",
)


def _schema_wire(schema: Any) -> dict[str, Any]:
    """Translate a JSON Schema fragment into Gemini's OpenAPI subset.

    Recursive and lossy by design: an unsupported keyword is dropped rather than
    forwarded, because a tool the model cannot see is a smaller failure than a
    request the API refuses outright. Validation of what the model sends back is
    unaffected -- that happens in the governed tool registry against the real
    schema, not against this copy.
    """
    if not isinstance(schema, dict):
        return {"type": "STRING"}

    out: dict[str, Any] = {}
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        # JSON Schema's ``["string", "null"]`` union is Gemini's ``nullable``.
        concrete = [str(item) for item in raw_type if str(item).lower() != "null"]
        if len(concrete) < len(raw_type):
            out["nullable"] = True
        raw_type = concrete[0] if concrete else "string"
    if raw_type:
        # The wire enum is the proto name, which is upper case.
        out["type"] = str(raw_type).upper()

    for key in ("description", "format", "enum", "required", "minItems", "maxItems"):
        if schema.get(key) is not None:
            out[key] = schema[key]
    if schema.get("nullable") is True:
        out["nullable"] = True

    properties = schema.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {name: _schema_wire(value) for name, value in properties.items()}
        out.setdefault("type", "OBJECT")
    items = schema.get("items")
    if items is not None:
        out["items"] = _schema_wire(items)
        out.setdefault("type", "ARRAY")
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of:
        out["anyOf"] = [_schema_wire(entry) for entry in any_of]
        out.pop("type", None)

    return out or {"type": "STRING"}


def _tool_wire(specs: Sequence[LLMToolSpec]) -> list[dict[str, Any]]:
    """One ``tools`` entry holding every declaration, which is the shape the API wants."""
    declarations = [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": _schema_wire(spec.parameters),
        }
        for spec in specs
    ]
    return [{"functionDeclarations": declarations}]


def _tool_response_payload(content: str | None) -> dict[str, Any]:
    """A governed tool's result, as the object Gemini requires.

    ``functionResponse.response`` must be a JSON object. Tool results are already
    JSON text, so a decoded object passes through; a list or a bare value is
    wrapped rather than reshaped, and text that is not JSON at all is carried as
    text. Nothing is summarised or dropped: the model must see what the governed
    tool actually returned.
    """
    if not content:
        return {}
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        return {"result": content}
    if isinstance(decoded, dict):
        return decoded
    return {"result": decoded}


def _parts_for(message: LLMMessage) -> list[dict[str, Any]]:
    if message.role == "tool":
        return [
            {
                "functionResponse": {
                    # Gemini pairs a result with its request by function name,
                    # not by call id, so the id the orchestrator tracks is not
                    # sent. It stays in the platform's own turn record.
                    "name": message.name or "tool",
                    "response": _tool_response_payload(message.content),
                }
            }
        ]

    parts: list[dict[str, Any]] = []
    if message.content:
        parts.append({"text": message.content})
    for call in message.tool_calls:
        parts.append({"functionCall": {"name": call.name, "args": call.arguments or {}}})
    return parts


def _contents_wire(messages: Sequence[LLMMessage]) -> tuple[list[dict[str, Any]], str]:
    """Split the message list into Gemini's ``contents`` and its system instruction.

    Consecutive turns of the same role are merged. The API is lenient about this
    but tool-calling rounds naturally produce two user-role entries in a row (a
    function response followed by the next question), and one merged turn is the
    shape the model was trained on.
    """
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        role = "model" if message.role == "assistant" else "user"
        parts = _parts_for(message)
        if not parts:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    return contents, "\n\n".join(system_parts).strip()


def _usage_from(raw: Any, *, fallback: LLMUsage) -> LLMUsage:
    if not isinstance(raw, dict):
        return fallback
    return LLMUsage(
        prompt_tokens=int(raw.get("promptTokenCount") or 0),
        # Thinking tokens are billed but are not part of the answer; they are
        # counted here so the cost estimate is honest.
        completion_tokens=int(raw.get("candidatesTokenCount") or 0)
        + int(raw.get("thoughtsTokenCount") or 0),
    )


def _read_candidate(
    candidate: Any,
    *,
    text_parts: list[str],
    calls: list[dict[str, Any]],
) -> str | None:
    """Collect the visible text and the requested tools from one candidate.

    Returns the finish reason. Parts flagged ``thought`` are read only so it is
    explicit that they are discarded: Gemini's deliberation is not part of the
    provider contract and cannot reach a response body from here.
    """
    if not isinstance(candidate, dict):
        return None
    content = candidate.get("content") or {}
    for part in content.get("parts") or []:
        if not isinstance(part, dict) or part.get("thought") is True:
            continue
        text = part.get("text")
        if text:
            text_parts.append(str(text))
        call = part.get("functionCall")
        if isinstance(call, dict) and call.get("name"):
            calls.append(call)
    reason = candidate.get("finishReason")
    return str(reason) if reason else None


def _tool_calls_from(raw_calls: Sequence[dict[str, Any]]) -> tuple[LLMToolCall, ...]:
    calls: list[LLMToolCall] = []
    for index, raw in enumerate(raw_calls):
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        args = raw.get("args")
        if args is None:
            arguments: dict[str, Any] = {}
            error: str | None = None
        elif isinstance(args, dict):
            arguments, error = args, None
        else:
            # The API sends decoded arguments, so this is a shape the contract
            # does not allow rather than a parse failure. Reported, not guessed
            # at: the orchestrator turns it into a refusal.
            arguments, error = {}, "arguments were not a JSON object"
        calls.append(
            LLMToolCall(
                # Gemini issues no call id. A positional one keeps the platform's
                # own turn record addressable; it is never sent back.
                call_id=f"call_{index}",
                name=name,
                arguments=arguments,
                argument_error=error,
            )
        )
    return tuple(calls)


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    # -- transport -------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                # Header rather than the ``?key=`` query parameter the quickstart
                # uses: a URL is the part of a failed request that ends up in
                # logs and exception text.
                headers["x-goog-api-key"] = self.config.api_key
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
    def _path(self, *, stream: bool) -> str:
        # The model is part of the path here, not a body field.
        method = "streamGenerateContent" if stream else "generateContent"
        suffix = "?alt=sse" if stream else ""
        return f"/models/{self.config.model}:{method}{suffix}"

    def _body(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMToolSpec] | None,
    ) -> dict[str, Any]:
        contents, system_instruction = _contents_wire(messages)
        generation: dict[str, Any] = {
            "temperature": self.config.temperature,
            "maxOutputTokens": self.config.max_output_tokens,
        }
        if self.config.reasoning_effort == "none":
            # The one effort level with an unambiguous Gemini meaning: spend no
            # budget on thinking, leaving the response allowance for the answer
            # a user can actually see. Other levels are left to the model's own
            # dynamic budget rather than mapped to invented numbers.
            generation["thinkingConfig"] = {"thinkingBudget": 0}

        body: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if tools:
            body["tools"] = _tool_wire(tools)
            # AUTO, never ANY: a question answerable from retrieved evidence
            # should be answered, not forced through a tool call.
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        return body

    async def generate(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMToolSpec] | None = None,
        stream: bool = False,
    ) -> LLMResponse:
        body = self._body(messages, tools)
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
            # Reports the host, not the URL or the headers: one carries the model
            # path and the other carries the API key.
            raise LLMProviderError(
                f"Could not reach the model endpoint at {self.config.endpoint_host}.",
                details={"provider": self.name, "reason": type(exc).__name__},
            ) from exc

    async def _generate_once(self, body: dict[str, Any]) -> LLMResponse:
        response = await self._http().post(self._path(stream=False), json=body)
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

        candidates = payload.get("candidates") or []
        if not candidates:
            # A prompt refused by the safety layer comes back with no candidate
            # and a reason, which is a usable answer for the caller rather than
            # an empty one.
            feedback = payload.get("promptFeedback") or {}
            blocked = str(feedback.get("blockReason") or "").strip()
            raise LLMProviderError(
                "The model returned no completion candidates."
                + (f" Reported reason: {blocked}." if blocked else ""),
                details={"provider": self.name, "model": self.config.model},
            )

        text_parts: list[str] = []
        raw_calls: list[dict[str, Any]] = []
        finish_reason = _read_candidate(candidates[0], text_parts=text_parts, calls=raw_calls)

        return LLMResponse(
            text=strip_reasoning("".join(text_parts)),
            tool_calls=_tool_calls_from(raw_calls),
            usage=_usage_from(payload.get("usageMetadata"), fallback=LLMUsage()),
            model=str(payload.get("modelVersion") or self.config.model),
            finish_reason=finish_reason,
        )

    async def _generate_streaming(self, body: dict[str, Any]) -> LLMResponse:
        """Consume the SSE stream and return one assembled response.

        Streaming is used for the transport benefit -- a long answer starts
        arriving immediately and the connection stays warm -- but the frames are
        joined here. Nothing partial is exposed upward, because a half-formed
        answer has not yet been checked against the evidence it must be grounded
        in.
        """
        text_parts: list[str] = []
        raw_calls: list[dict[str, Any]] = []
        usage = LLMUsage()
        model = self.config.model
        finish_reason: str | None = None

        async with self._http().stream("POST", self._path(stream=True), json=body) as response:
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

                model = str(chunk.get("modelVersion") or model)
                # Each frame repeats the running totals, so the last one wins.
                usage = _usage_from(chunk.get("usageMetadata"), fallback=usage)
                for candidate in chunk.get("candidates") or []:
                    reason = _read_candidate(
                        candidate, text_parts=text_parts, calls=raw_calls
                    )
                    finish_reason = reason or finish_reason

        return LLMResponse(
            # Stripped after joining: a reasoning block can span frames, so
            # scrubbing per frame would miss it.
            text=strip_reasoning("".join(text_parts)),
            tool_calls=_tool_calls_from(raw_calls),
            usage=usage,
            model=model,
            finish_reason=finish_reason,
        )

    # -- errors ----------------------------------------------------------
    def _error_message(self, response: httpx.Response) -> str:
        """Summarise a provider failure without echoing request material.

        The response body of a failed model call can quote the prompt, so only
        the provider's own error message is used, and only briefly.
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
        detail = detail.strip()[:300]
        base = (
            f"The model endpoint at {self.config.endpoint_host} returned "
            f"HTTP {response.status_code}"
        )
        if response.status_code in (401, 403):
            return f"{base}. Check GEMINI_API_KEY."
        if response.status_code == 404:
            return f"{base}. Check that GEMINI_MODEL={self.config.model!r} exists for this key."
        if response.status_code == 429:
            return f"{base}. The key's request quota is exhausted{f': {detail}' if detail else '.'}"
        return f"{base}{f': {detail}' if detail else '.'}"

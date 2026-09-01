"""The Gemini transport against the wire shape Google actually sends.

The Copilot suite scripts the *provider*, which is the right level for asserting
things about the governed layer but leaves the transport itself uncovered. These
tests drive the real ``GeminiProvider`` over a mock HTTP layer using payloads in
the ``generateContent`` shape, so the integration ``verify_gemini.py`` checks
against a live key stays pinned in CI with no key, no network and no quota.

Four Gemini-specific hazards are what this file exists for:

1. **Deliberation arrives as a part, not a field.** Gemini 2.5 returns thinking
   as a content part flagged ``thought: true`` alongside the answer's part. A
   transport that only knew how to strip ``<think>`` blocks would pass its own
   tests and still put chain-of-thought in a response body.
2. **Tool results are matched by name.** There is no ``tool_call_id`` on the
   wire, so a result handed back under the wrong key silently becomes a
   different function's answer.
3. **The schema dialect is narrower than JSON Schema.** The governed tool
   registry publishes ``additionalProperties`` and friends, which this API
   rejects outright with a 400 -- meaning every tool-calling turn fails, not
   just an unusual one.
4. **The key belongs in a header.** A URL is the part of a failed request that
   ends up in logs and exception text.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from app.core.config import settings
from app.llm import llm_config_from_settings
from app.llm.provider import LLMMessage, LLMProviderError, LLMToolSpec

TEST_KEY = "test-gemini-key-not-a-real-one"
TEST_MODEL = "gemini-2.5-flash"


def _gemini_provider(handler):
    """A real transport whose HTTP layer is a recorded-response mock."""
    from app.llm.gemini import GeminiProvider

    config = dataclasses.replace(
        llm_config_from_settings(settings),
        enabled=True,
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key=TEST_KEY,
        model=TEST_MODEL,
    )
    provider = GeminiProvider(config)
    # Let the provider build its own client -- base URL, auth header and timeout
    # are part of what is under test -- then swap only the network layer beneath
    # it, so the request that reaches ``handler`` is the real one.
    client = provider._http()
    client._transport = httpx.MockTransport(handler)
    return provider


# A 2.5 answer with thinking in the response: one part flagged `thought`, one
# part carrying the visible answer.
_THINKING_ANSWER = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "text": "The user wants the revenue definition. I should check units.",
                        "thought": True,
                    },
                    {"text": "Revenue is the sum of order_value."},
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 812,
        "candidatesTokenCount": 44,
        "thoughtsTokenCount": 130,
    },
    "modelVersion": "gemini-2.5-flash",
}

_TOOL_REQUEST = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": "get_kpi_definition",
                            "args": {"name": "Gross Margin"},
                        }
                    }
                ],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 12},
    "modelVersion": "gemini-2.5-flash",
}

KPI_TOOL = LLMToolSpec(
    name="get_kpi_definition",
    description="Return the governed definition of a KPI by name.",
    parameters={
        # The shape the governed registry publishes, including the keys Gemini
        # refuses.
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "description": "KPI name"},
            "as_of": {"type": ["string", "null"], "description": "ISO date"},
            "columns": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["name"],
    },
)


@pytest.mark.anyio
async def test_gemini_thinking_parts_never_reach_the_response():
    """The answer survives; the deliberation next to it does not."""
    provider = _gemini_provider(lambda request: httpx.Response(200, json=_THINKING_ANSWER))
    try:
        reply = await provider.generate([LLMMessage.user("Define revenue.")])
    finally:
        await provider.aclose()

    assert reply.text == "Revenue is the sum of order_value."
    assert "should check units" not in reply.text
    assert "thought" not in reply.text.lower()
    # Thinking is billed even though it is not shown, so the cost estimate counts
    # it rather than under-reporting the call.
    assert reply.usage.prompt_tokens == 812
    assert reply.usage.completion_tokens == 44 + 130
    assert reply.model == "gemini-2.5-flash"
    assert reply.finish_reason == "STOP"
    assert reply.tool_calls == ()


@pytest.mark.anyio
async def test_the_api_key_travels_in_a_header_and_not_in_the_url():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key", "")
        return httpx.Response(200, json=_THINKING_ANSWER)

    provider = _gemini_provider(handler)
    try:
        await provider.generate([LLMMessage.user("Define revenue.")])
    finally:
        await provider.aclose()

    assert seen["key"] == TEST_KEY
    assert TEST_KEY not in seen["url"]
    # The model is part of the path in this API, and the method is the one that
    # returns a whole answer rather than a stream.
    assert seen["url"].endswith(f"/models/{TEST_MODEL}:generateContent")


@pytest.mark.anyio
async def test_the_model_configuration_is_not_in_the_body_the_way_openai_wants_it():
    """System text becomes an instruction, and the roles are Gemini's own."""
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=_THINKING_ANSWER)

    provider = _gemini_provider(handler)
    try:
        await provider.generate(
            [
                LLMMessage.system("You answer only from governed evidence."),
                LLMMessage.system("Never invent a KPI."),
                LLMMessage.user("Define revenue."),
                LLMMessage.assistant("Which entity?"),
                LLMMessage.user("The company total."),
            ]
        )
    finally:
        await provider.aclose()

    # Both system messages, joined, in the field Gemini reads them from.
    instruction = sent["systemInstruction"]["parts"][0]["text"]  # type: ignore[index]
    assert "governed evidence" in instruction
    assert "Never invent a KPI." in instruction
    assert "messages" not in sent

    contents = sent["contents"]
    assert [entry["role"] for entry in contents] == ["user", "model", "user"]  # type: ignore[index]
    assert contents[0]["parts"] == [{"text": "Define revenue."}]  # type: ignore[index]
    generation = sent["generationConfig"]
    assert generation["maxOutputTokens"] == settings.llm_max_output_tokens  # type: ignore[index]


@pytest.mark.anyio
async def test_a_published_tool_schema_is_translated_into_the_dialect_gemini_accepts():
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=_TOOL_REQUEST)

    provider = _gemini_provider(handler)
    try:
        asked = await provider.generate([LLMMessage.user("Define Gross Margin.")], tools=[KPI_TOOL])
    finally:
        await provider.aclose()

    declaration = sent["tools"][0]["functionDeclarations"][0]  # type: ignore[index]
    assert declaration["name"] == "get_kpi_definition"
    parameters = declaration["parameters"]
    # The keys that would make the whole request a 400 are gone.
    wire = json.dumps(parameters)
    assert "$schema" not in wire
    assert "additionalProperties" not in wire
    # The governed shape survives the translation.
    assert parameters["type"] == "OBJECT"
    assert parameters["required"] == ["name"]
    assert parameters["properties"]["name"]["type"] == "STRING"
    assert parameters["properties"]["columns"]["type"] == "ARRAY"
    assert parameters["properties"]["columns"]["items"]["type"] == "STRING"
    # A JSON Schema null union is Gemini's `nullable`, not a second type.
    assert parameters["properties"]["as_of"]["type"] == "STRING"
    assert parameters["properties"]["as_of"]["nullable"] is True
    # AUTO, never ANY: a question answerable from evidence should be answered.
    assert sent["toolConfig"] == {"functionCallingConfig": {"mode": "AUTO"}}  # type: ignore[index]

    assert asked.wants_tools
    call = asked.tool_calls[0]
    assert call.name == "get_kpi_definition"
    assert call.arguments == {"name": "Gross Margin"}
    assert call.argument_error is None


@pytest.mark.anyio
async def test_a_tool_result_goes_back_under_the_function_name():
    """Gemini pairs a result with its request by name, so the name must be sent."""
    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=_THINKING_ANSWER)

    provider = _gemini_provider(handler)
    try:
        await provider.generate(
            [
                LLMMessage.user("Define Gross Margin."),
                LLMMessage.assistant(None, [asked_call()]),
                LLMMessage.tool_result(
                    call_id="call_0",
                    name="get_kpi_definition",
                    payload={"ok": True, "result": {"formula": "(revenue - cogs) / revenue"}},
                ),
            ],
            tools=[KPI_TOOL],
        )
    finally:
        await provider.aclose()

    contents = sent["contents"]
    assert [entry["role"] for entry in contents] == ["user", "model", "user"]  # type: ignore[index]
    request_part = contents[1]["parts"][0]  # type: ignore[index]
    assert request_part["functionCall"]["name"] == "get_kpi_definition"
    response_part = contents[2]["parts"][0]  # type: ignore[index]
    assert response_part["functionResponse"]["name"] == "get_kpi_definition"
    # The governed tool's own JSON object, not a re-encoded string.
    assert response_part["functionResponse"]["response"]["ok"] is True
    assert (
        response_part["functionResponse"]["response"]["result"]["formula"]
        == "(revenue - cogs) / revenue"
    )
    # No id is invented on the wire: this API has no field for one.
    assert "tool_call_id" not in json.dumps(contents)


def asked_call():
    from app.llm.provider import LLMToolCall

    return LLMToolCall(call_id="call_0", name="get_kpi_definition", arguments={"name": "Gross Margin"})


@pytest.mark.anyio
async def test_the_streaming_path_assembles_one_answer_and_drops_thinking():
    frames = [
        {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "First I check", "thought": True}]}}
            ]
        },
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Revenue is "}]}}]},
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "the sum of order_value."}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 700, "candidatesTokenCount": 30},
            "modelVersion": "gemini-2.5-flash",
        },
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    provider = _gemini_provider(handler)
    try:
        reply = await provider.generate([LLMMessage.user("Define revenue.")], stream=True)
    finally:
        await provider.aclose()

    assert seen["url"].endswith(f"/models/{TEST_MODEL}:streamGenerateContent?alt=sse")
    assert reply.text == "Revenue is the sum of order_value."
    assert "First I check" not in reply.text
    assert reply.usage.prompt_tokens == 700
    assert reply.finish_reason == "STOP"


@pytest.mark.anyio
async def test_a_streamed_tool_request_survives_reassembly():
    frames = [
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "get_kpi_definition",
                                    "args": {"name": "Net Revenue Retention"},
                                }
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    provider = _gemini_provider(
        lambda request: httpx.Response(
            200, text=body, headers={"Content-Type": "text/event-stream"}
        )
    )
    try:
        reply = await provider.generate([LLMMessage.user("Look it up.")], tools=[KPI_TOOL], stream=True)
    finally:
        await provider.aclose()

    assert reply.wants_tools
    assert reply.tool_calls[0].arguments == {"name": "Net Revenue Retention"}
    assert reply.tool_calls[0].argument_error is None


@pytest.mark.anyio
async def test_a_failed_call_reports_the_host_and_not_the_request():
    """An error message may not carry the prompt, the URL or the key."""
    error_body = {
        "error": {
            "code": 400,
            "message": "Invalid JSON payload received. Unknown name \"additionalProperties\".",
            "status": "INVALID_ARGUMENT",
        }
    }
    provider = _gemini_provider(lambda request: httpx.Response(400, json=error_body))
    try:
        with pytest.raises(LLMProviderError) as raised:
            await provider.generate([LLMMessage.user("A prompt with SECRETTEXT in it.")])
    finally:
        await provider.aclose()

    message = str(raised.value)
    assert "generativelanguage.googleapis.com" in message
    assert "SECRETTEXT" not in message
    assert TEST_KEY not in message
    assert "Unknown name" in message


@pytest.mark.anyio
async def test_an_unauthorised_call_names_the_setting_to_check():
    provider = _gemini_provider(
        lambda request: httpx.Response(403, json={"error": {"message": "API key not valid"}})
    )
    try:
        with pytest.raises(LLMProviderError) as raised:
            await provider.generate([LLMMessage.user("Define revenue.")])
    finally:
        await provider.aclose()

    assert "GEMINI_API_KEY" in str(raised.value)
    assert TEST_KEY not in str(raised.value)


@pytest.mark.anyio
async def test_a_blocked_prompt_is_reported_rather_than_answered_empty():
    """No candidate is a refusal, and a refusal must not look like a blank answer."""
    provider = _gemini_provider(
        lambda request: httpx.Response(
            200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )
    )
    try:
        with pytest.raises(LLMProviderError) as raised:
            await provider.generate([LLMMessage.user("Define revenue.")])
    finally:
        await provider.aclose()

    assert "SAFETY" in str(raised.value)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_gemini_configuration_is_resolved_from_its_own_settings(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", TEST_KEY)
    monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-pro")
    # Deliberately wrong for Gemini: these belong to the local endpoint and must
    # not be picked up, or a switch of provider would send a Qwen model name to
    # Google and get a 404.
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")

    config = llm_config_from_settings(settings)
    assert config.provider == "gemini"
    assert config.model == "gemini-2.5-pro"
    assert config.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert config.is_available is True

    described = config.describe()
    assert described["endpoint_host"] == "generativelanguage.googleapis.com"
    # The one thing that must never be in a response body, log line or audit row.
    assert TEST_KEY not in json.dumps(described)
    assert "api_key" not in described


def test_a_missing_key_is_a_configuration_message_not_a_401(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "   ")
    monkeypatch.setattr(settings, "gemini_model", "")

    config = llm_config_from_settings(settings)
    # Blank GEMINI_MODEL still resolves to a usable default, so the only thing
    # missing is the key.
    assert config.model == "gemini-2.5-flash"
    assert config.is_available is False
    reason = config.unavailable_reason or ""
    assert "GEMINI_API_KEY" in reason
    assert "browser" in reason


def test_the_provider_factory_builds_the_gemini_transport(monkeypatch):
    from app.llm.gemini import GeminiProvider
    from app.llm.provider import build_provider

    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", TEST_KEY)
    monkeypatch.setattr(settings, "gemini_model", TEST_MODEL)

    provider = build_provider(llm_config_from_settings(settings))
    assert isinstance(provider, GeminiProvider)
    assert provider.name == "gemini"


def test_switching_provider_does_not_disturb_the_local_endpoint_settings(monkeypatch):
    """The abstraction, asserted: one environment value selects the transport."""
    from app.llm.openai_compatible import OpenAICompatibleProvider
    from app.llm.provider import build_provider

    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", TEST_KEY)
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:11434/v1")
    monkeypatch.setattr(settings, "llm_model", "qwen3:8b")

    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    local = build_provider(llm_config_from_settings(settings))
    assert isinstance(local, OpenAICompatibleProvider)
    assert local.config.model == "qwen3:8b"

    monkeypatch.setattr(settings, "llm_provider", "gemini")
    hosted = build_provider(llm_config_from_settings(settings))
    assert hosted.name == "gemini"
    # The local configuration is untouched and ready to switch back to.
    assert settings.llm_model == "qwen3:8b"
    assert settings.llm_base_url == "http://localhost:11434/v1"

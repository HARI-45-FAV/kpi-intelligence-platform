"""End-to-end verification of the Copilot against Gemini.

The Gemini counterpart to ``verify_ollama.py``, and deliberately smaller: the
governed layer above the transport is the same one that script already exercises
and that the test suite pins offline. What is unproven for a hosted provider is
the transport itself — that the key authenticates, that ``generateContent``
returns text in the shape ``app.llm.gemini`` expects, that function calling comes
back in a form the tool registry can execute, and that Gemini's thinking never
reaches a response body.

Run outside pytest deliberately: the suite pins ``LLM_ENABLED=false`` because its
subject is the governed machinery with the model scripted. Here the model is real.

Requires:  GEMINI_API_KEY set, LLM_ENABLED=true, LLM_PROVIDER=gemini
Run:       python verify_gemini.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

os.environ.setdefault("SECRET_KEY", "verify-gemini-secret-not-for-production-0123456789")
os.environ.setdefault("ENVIRONMENT", "test")

from app.llm import build_provider, get_llm_config  # noqa: E402
from app.llm.provider import LLMMessage, LLMToolSpec, NullProvider  # noqa: E402

#: Substrings that would mean hidden deliberation escaped into a response body.
LEAK_MARKERS = ("<think", "</think", "<reasoning", "</reasoning", "scratchpad")

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(f"{label} {detail}".strip())


def leaks(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [marker for marker in LEAK_MARKERS if marker in lowered]


# ---------------------------------------------------------------------------
print("\n[1] Configuration resolves to Gemini")
# ---------------------------------------------------------------------------
config = get_llm_config()
described = config.describe()
print("      " + json.dumps(described))
check("LLM_ENABLED is true", config.enabled is True)
check("provider is gemini", config.provider == "gemini", config.provider)
check("a model is resolved", bool(config.model), config.model)
check(
    "config reports itself available",
    config.unavailable_reason is None,
    str(config.unavailable_reason),
)
# The one thing a hosted provider must never get wrong. ``describe()`` is what
# reaches the API, the audit trail and telemetry.
check(
    "the API key never appears in describe()",
    bool(config.api_key) and config.api_key not in json.dumps(described),
)

provider = build_provider(config)
check(
    "a real transport was built, not the null provider",
    not isinstance(provider, NullProvider),
    type(provider).__name__,
)

if failures:
    print("\nConfiguration is wrong; not contacting the model.")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)


# ---------------------------------------------------------------------------
print("\n[2] The Gemini endpoint answers, and tool calling round-trips")
# ---------------------------------------------------------------------------
KPI_TOOL = LLMToolSpec(
    name="get_kpi_definition",
    description="Return the governed definition of a KPI by name.",
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "KPI name"}},
        "required": ["name"],
    },
)


async def probe_transport() -> None:
    reply = await provider.generate(
        [
            LLMMessage.system("You are terse. Answer with digits only."),
            LLMMessage.user("What is 17 * 3?"),
        ]
    )
    check("a request returned text", bool(reply.text.strip()), repr(reply.text))
    check("the answer is correct", "51" in reply.text, repr(reply.text))
    check("the response names the model", bool(reply.model), reply.model)
    check("token usage was reported", reply.usage.total_tokens > 0, str(reply.usage))
    check("no reasoning in the text", not leaks(reply.text), str(leaks(reply.text)))

    streamed = await provider.generate(
        [LLMMessage.user("Name three primary colours, comma separated.")], stream=True
    )
    check("the streaming path assembles text", bool(streamed.text.strip()), repr(streamed.text))
    check("no reasoning in streamed text", not leaks(streamed.text))

    asked = await provider.generate(
        [
            LLMMessage.system("Use the provided tools when the user asks about a KPI."),
            LLMMessage.user("What is the definition of the KPI named Gross Margin?"),
        ],
        tools=[KPI_TOOL],
    )
    check("the model requested a tool", asked.wants_tools, str(asked.finish_reason))
    if asked.wants_tools:
        call = asked.tool_calls[0]
        check("the requested tool is the one offered", call.name == "get_kpi_definition", call.name)
        check("arguments parsed as JSON", call.argument_error is None, str(call.argument_error))
        check(
            "arguments carry the KPI name",
            call.arguments.get("name") == "Gross Margin",
            str(call.arguments),
        )

        # The shape the orchestrator's loop uses: Gemini's functionResponse part
        # differs from the OpenAI tool message, and this is where that shows.
        followed = await provider.generate(
            [
                LLMMessage.system("Answer strictly from tool results."),
                LLMMessage.user("What is the definition of the KPI named Gross Margin?"),
                LLMMessage.assistant(asked.text or None, asked.tool_calls),
                LLMMessage.tool_result(
                    call_id=call.call_id,
                    name=call.name,
                    payload={
                        "ok": True,
                        "result": {
                            "name": "Gross Margin",
                            "formula": "(revenue - cogs) / revenue",
                            "owner": "Finance",
                        },
                    },
                ),
            ],
            tools=[KPI_TOOL],
        )
        check("a tool result produces prose", bool(followed.text.strip()), repr(followed.text[:120]))
        check("the answer used the tool result", "revenue" in followed.text.lower())
        check("no reasoning after a tool round trip", not leaks(followed.text))

    await provider.aclose()


try:
    asyncio.run(probe_transport())
except Exception as exc:  # noqa: BLE001
    check(f"the Gemini endpoint is reachable ({type(exc).__name__}: {exc})", False)


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("All Gemini transport checks passed.")

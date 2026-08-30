"""End-to-end verification of the Copilot against a local Ollama model.

The counterpart to ``verify_no_llm.py``: that script proves the platform runs
with the model layer switched off, this one proves the same governed layer works
when it is switched on and pointed at a real endpoint.

Run outside pytest deliberately -- the test suite pins ``LLM_ENABLED=false``
because its subject is the governed machinery, with the model scripted. Here the
model is real, so what is checked is the integration: the transport reaches
Ollama, qwen3:8b answers, tool calls come back in a shape the registry can
execute, and no reasoning survives the trip.

Requires:  ollama serve  +  ollama pull qwen3:8b
Run:       python verify_ollama.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

TMP = Path(__file__).resolve().parent / "tests" / "_tmp"
TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "verify_ollama.db"
if DB.exists():
    DB.unlink()

# Its own database, so this never touches development or test data. The model
# configuration is read from .env like a real deployment; only the storage and
# secret are overridden.
os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["DOCUMENT_STORAGE_DIR"] = str(TMP / "verify_ollama_documents")
os.environ["SECRET_KEY"] = "verify-ollama-secret-not-for-production-0123456789"
os.environ["ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.llm import build_provider, get_llm_config  # noqa: E402
from app.llm.provider import LLMMessage, LLMToolSpec, NullProvider  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.bootstrap import sync_reference_data  # noqa: E402
from tests.fixture_source import build_fixture_database  # noqa: E402

API = "/api/v1"

# Substrings that would mean chain-of-thought escaped into a response body.
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
print("\n[1] Configuration resolves to the Ollama endpoint")
# ---------------------------------------------------------------------------
config = get_llm_config()
described = config.describe()
print("      " + json.dumps(described))
check("LLM_ENABLED is true", config.enabled is True)
check("provider is openai_compatible", config.provider == "openai_compatible")
check("base URL is Ollama's /v1", config.base_url == "http://localhost:11434/v1", config.base_url)
check("model is qwen3:8b", config.model == "qwen3:8b", config.model)
check("config reports itself available", config.is_available is True, str(config.unavailable_reason))

provider = build_provider(config)
check(
    "a real transport was built, not the null provider",
    not isinstance(provider, NullProvider),
    type(provider).__name__,
)
check("the API key never appears in describe()", "EMPTY" not in json.dumps(described))

if failures:
    print("\nConfiguration is wrong; not contacting the model.")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)


# ---------------------------------------------------------------------------
print("\n[2] The model endpoint answers a direct request")
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
    check("a non-streaming request returned text", bool(reply.text.strip()), repr(reply.text))
    check("the answer is correct", "51" in reply.text, repr(reply.text))
    check("the response names the model", reply.model == "qwen3:8b", reply.model)
    check("token usage was reported", reply.usage.total_tokens > 0, str(reply.usage))
    check("no reasoning in the text", not leaks(reply.text), str(leaks(reply.text)))

    streamed = await provider.generate(
        [LLMMessage.user("Name three primary colours, comma separated.")], stream=True
    )
    check("the streaming path assembles text", bool(streamed.text.strip()), repr(streamed.text))
    check("no reasoning in streamed text", not leaks(streamed.text))

    # -- tool calling, transport level ----------------------------------
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
        check("the requested tool name is the one offered", call.name == "get_kpi_definition", call.name)
        check("arguments parsed as JSON", call.argument_error is None, str(call.argument_error))
        check("arguments carry the KPI name", call.arguments.get("name") == "Gross Margin", str(call.arguments))

        # Feed a result back: this is the shape the orchestrator's loop uses.
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
        check("a tool result produces a prose answer", bool(followed.text.strip()), repr(followed.text[:120]))
        check("the answer used the tool result", "revenue" in followed.text.lower())
        check("no reasoning after a tool round trip", not leaks(followed.text))

    # -- streaming + tools ----------------------------------------------
    streamed_tools = await provider.generate(
        [
            LLMMessage.system("Use the provided tools when the user asks about a KPI."),
            LLMMessage.user("Look up the KPI named Net Revenue Retention."),
        ],
        tools=[KPI_TOOL],
        stream=True,
    )
    check("streamed tool fragments reassemble", streamed_tools.wants_tools)
    if streamed_tools.wants_tools:
        check(
            "reassembled arguments are valid JSON",
            streamed_tools.tool_calls[0].argument_error is None,
            str(streamed_tools.tool_calls[0].argument_error),
        )

    await provider.aclose()


try:
    asyncio.run(probe_transport())
except Exception as exc:  # noqa: BLE001
    check(f"the model endpoint is reachable ({type(exc).__name__}: {exc})", False)
    print("\nCould not talk to Ollama. Is `ollama serve` running?")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)


# ---------------------------------------------------------------------------
print("\n[3] A governed workspace, built through the API")
# ---------------------------------------------------------------------------
Base.metadata.create_all(bind=engine)
session = SessionLocal()
try:
    sync_reference_data(session)
    session.commit()
finally:
    session.close()

source_fixture = build_fixture_database(TMP / "verify_ollama_source.db")
app = create_app()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


with TestClient(app) as client:
    registered = client.post(
        f"{API}/auth/register",
        json={
            "email": "ada@alphaworks-ollama.com",
            "password": "Alpha-Ollama-2026",
            "full_name": "Ada Alpha",
        },
    )
    check("a user can register", registered.status_code == 201, registered.text[:200])
    alpha = bearer(registered.json()["access_token"])

    company = client.post(
        f"{API}/companies",
        headers=alpha,
        json={"company_name": "AlphaWorks Ollama", "currency": "INR", "timezone": "Asia/Kolkata"},
    )
    check("a company can be created", company.status_code == 201, company.text[:200])
    company_id = company.json()["id"]
    base = f"{API}/companies/{company_id}"

    source = client.post(
        f"{base}/data-sources",
        headers=alpha,
        json={
            "name": "AlphaWorks Warehouse",
            "source_type": "SQLITE",
            "path": source_fixture["path"],
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    check("a data source registers", source.status_code == 201, source.text[:200])
    source_id = source.json()["id"]
    check(
        "discovery succeeds",
        client.post(f"{base}/data-sources/{source_id}/discover", headers=alpha).status_code == 200,
    )

    tables = {t["table_name"]: t for t in client.get(f"{base}/tables", headers=alpha).json()}
    check(
        "the orders table is in scope",
        client.put(
            f"{base}/data-scope",
            headers=alpha,
            json={
                "replace": True,
                "tables": [{"source_table_id": tables["orders"]["id"], "enabled": True}],
            },
        ).status_code
        == 200,
    )
    check(
        "the table profiles",
        client.post(f"{base}/tables/{tables['orders']['id']}/profile", headers=alpha).status_code == 200,
    )

    imported = client.post(
        f"{base}/kpi-source-definitions/import", headers=alpha, json={"kpi_keys": ["revenue"]}
    )
    check("a KPI imports from the registry", imported.status_code == 201, imported.text[:200])
    kpi = imported.json()["imported"][0]
    version_id = kpi["versions"][0]["id"]

    check(
        "the KPI version validates",
        client.post(f"{base}/kpi-versions/{version_id}/validate", headers=alpha).status_code == 200,
    )
    check(
        "the KPI version is approved",
        client.post(
            f"{base}/kpi-versions/{version_id}/approve",
            headers=alpha,
            json={"reason": "Matches the company KPI registry."},
        ).status_code
        == 200,
    )

    client.post(
        f"{base}/documents",
        headers=alpha,
        data={
            "metadata": json.dumps(
                {
                    "title": "AlphaWorks Revenue Handbook",
                    "document_type": "KPI_HANDBOOK",
                    "access_scope": ["ADMIN", "ANALYST", "EXECUTIVE"],
                    "inline_content": (
                        "Revenue is the sum of order_value across all orders in the "
                        "period, recognised on the order date. Cancelled orders are "
                        "excluded before summation."
                    ),
                }
            )
        },
    )

    # -----------------------------------------------------------------
    print("\n[4] The Copilot status endpoint reports a live model")
    # -----------------------------------------------------------------
    status = client.get(f"{base}/copilot/status", headers=alpha)
    check("status returns 200", status.status_code == 200, status.text[:200])
    body = status.json()
    print("      " + json.dumps({k: body[k] for k in ("enabled", "available", "provider", "model", "endpoint_host")}))
    check("status says enabled", body["enabled"] is True)
    check("status says available", body["available"] is True, str(body["unavailable_reason"]))
    check("status names qwen3:8b", body["model"] == "qwen3:8b", str(body["model"]))
    check("status names the Ollama host", body["endpoint_host"] == "localhost:11434", str(body["endpoint_host"]))
    check("no credential in the status body", "EMPTY" not in status.text)
    check("governed tools are offered", len(body["tools_available"]) > 0, str(body["tools_available"]))
    print(f"      tools available to this caller: {len(body['tools_available'])}")

    # -----------------------------------------------------------------
    print("\n[5] End-to-end Copilot turns against qwen3:8b")
    # -----------------------------------------------------------------
    def ask(message: str, **context) -> dict:
        response = client.post(
            f"{base}/copilot/chat",
            headers=alpha,
            json={"message": message, "context": context},
        )
        assert response.status_code == 200, response.text
        return response.json()

    questions = (
        ("a KPI definition question", "What does the revenue KPI mean and how is it calculated?"),
        ("a KPI inventory question", "Which KPIs are active in this company right now?"),
        ("a validation question", "Has the revenue KPI been validated, and what was the result?"),
    )

    any_tool_called = False
    for label, question in questions:
        print(f"\n   -> {label}: {question!r}")
        answer = ask(question)
        raw = json.dumps(answer)

        check(f"[{label}] the model was available", answer["llm_available"] is True, str(answer.get("unavailable_reason")))
        check(f"[{label}] an answer came back", bool(answer["answer"].strip()))
        check(f"[{label}] the answer names the model", answer["model"] == "qwen3:8b", str(answer["model"]))
        check(f"[{label}] evidence is attached", len(answer["evidence"]) > 0, str(len(answer["evidence"])))
        check(f"[{label}] no reasoning in the response body", not leaks(raw), str(leaks(raw)))
        check(f"[{label}] no reasoning field exists at all",
              not any(k in raw for k in ('"reasoning"', '"reasoning_content"', '"thinking"')))
        check(f"[{label}] no credential in the response", "EMPTY" not in raw)

        called = [c["tool"] for c in answer["tool_calls"]]
        if called:
            any_tool_called = True
        print(f"      tools called : {called or '(answered from retrieved evidence)'}")
        print(f"      evidence     : {len(answer['evidence'])} item(s)")
        print(f"      iterations   : {answer['iterations']}  truncated={answer['truncated']}")
        print(f"      answer       : {answer['answer'][:220].replace(chr(10), ' ')}...")

        for call in answer["tool_calls"]:
            check(
                f"[{label}] tool {call['tool']} executed without an argument error",
                call["ok"] or "not valid JSON" not in (call["error"] or ""),
                str(call["error"]),
            )

    check("at least one governed tool was called across the turns", any_tool_called)

    # -----------------------------------------------------------------
    print("\n[6] A dashboard-figure question still discloses the placeholder")
    # -----------------------------------------------------------------
    figure = ask("Why did today's revenue value drop?", selected_date="2026-01-15")
    check("the placeholder caveat is present",
          any("placeholder" in c.lower() for c in figure["caveats"]), str(figure["caveats"]))
    check("no reasoning leaked on the figure question", not leaks(json.dumps(figure)))
    print(f"      answer: {figure['answer'][:220].replace(chr(10), ' ')}...")

    # -----------------------------------------------------------------
    print("\n[7] Company isolation still holds with a live model")
    # -----------------------------------------------------------------
    other = client.post(
        f"{API}/auth/register",
        json={
            "email": "ben@betacorp-ollama.com",
            "password": "Beta-Ollama-2026",
            "full_name": "Ben Beta",
        },
    )
    beta = bearer(other.json()["access_token"])
    intrusion = client.post(
        f"{base}/copilot/chat",
        headers=beta,
        json={"message": "List every KPI in this company.", "context": {}},
    )
    check(
        "a non-member is refused the Copilot",
        intrusion.status_code in (403, 404),
        f"got {intrusion.status_code}",
    )

    unauthenticated = client.post(
        f"{base}/copilot/chat", json={"message": "List every KPI.", "context": {}}
    )
    check(
        "an anonymous caller is refused",
        unauthenticated.status_code in (401, 403),
        f"got {unauthenticated.status_code}",
    )


# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
if failures:
    print(f"FAILED  {len(failures)} check(s)")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("All Ollama integration checks passed.")
print("=" * 70)

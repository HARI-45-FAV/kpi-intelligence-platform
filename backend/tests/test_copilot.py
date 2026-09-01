"""The AI Copilot layer, tested for the properties that make it safe to ship.

The Copilot is an *optional* explanation layer over a deterministic, governed BI
platform. That framing produces the whole test list here, because each of these
is a way the layer could quietly betray it:

* it could answer when no model is configured, or fail instead of saying so;
* it could read a neighbouring tenant's KPIs, documents or profiles;
* it could let the model name a company, write SQL, or reach a connection;
* it could narrate a figure no detection run produced as though it were measured;
* it could put a prompt, a question or an API key into a log or a response;
* it could invent the investigation and recommendation engines that do not exist
  in this version, or deny the detection engine that does.

None of those are caught by asserting that an answer "looks right", so nothing
here asserts on model prose. The model is scripted, and what is checked is the
governed machinery around it: what reached it, what it was allowed to call, what
came back, and what was recorded.

The existing suite stays the authority on the deterministic platform; these tests
add the Copilot without touching it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.api.v1 import copilot as copilot_api
from app.copilot.evidence import EvidenceBundle
from app.copilot.retrieval import build_corpus
from app.copilot.tools import PLANNED_TOOL_NAMES, REGISTRY
from app.copilot.tools.base import FORBIDDEN_PARAMETERS, ToolRegistry, ToolSpec, refuse
from app.core.config import settings
from app.llm.config import get_llm_config, llm_config_from_settings
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
)
from tests.conftest import API, ApiActor, register

# Nonsense words, chosen so a hit can only be a real cross-tenant leak. The
# ranker does prefix matching to catch plurals, so anything sharing a stem with
# platform vocabulary ("revenue", "margin", "order") would make a false positive
# indistinguishable from a true one.
ALPHA_MARKER = "zylthorix"
BETA_MARKER = "quovandelmar"

# The scripted model's identity, and a credential that must never leave config.
TEST_MODEL = "test/Scripted-Instruct-1"
SENTINEL_KEY = "sk-copilot-test-must-never-appear-6f2a"


# ---------------------------------------------------------------------------
# A model whose turns the test decides
# ---------------------------------------------------------------------------
class ScriptedModel(LLMProvider):
    """A provider that returns pre-arranged turns and records what it was sent.

    Standing in for the transport rather than for the HTTP endpoint keeps these
    tests about the governed layer. It also lets a test assert the far stronger
    property: not "the answer omitted the other company" but "the other
    company's material never reached the model at all".
    """

    name = "scripted"

    def __init__(self, config) -> None:
        super().__init__(config)
        self.turns: list[object] = []
        self.prompts: list[list] = []
        self.offered_tools: list[tuple[str, ...]] = []
        self.closes = 0

    def script(self, *turns: object) -> ScriptedModel:
        self.turns = list(turns)
        return self

    async def generate(self, messages, tools=None, stream=False) -> LLMResponse:
        self.prompts.append(list(messages))
        self.offered_tools.append(tuple(spec.name for spec in (tools or ())))
        turn = self.turns.pop(0) if self.turns else says("No further turns were scripted.")
        if isinstance(turn, BaseException):
            raise turn
        return turn  # type: ignore[return-value]

    async def aclose(self) -> None:
        self.closes += 1

    # -- what the model actually saw -------------------------------------
    @property
    def sent_text(self) -> str:
        """Every character of every message handed to the model, across turns."""
        return "\n".join(
            message.content or "" for prompt in self.prompts for message in prompt
        )

    @property
    def system_text(self) -> str:
        return "\n".join(
            message.content or ""
            for prompt in self.prompts
            for message in prompt
            if message.role == "system"
        )


def says(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, model=TEST_MODEL, usage=LLMUsage(prompt_tokens=64, completion_tokens=25)
    )


def asks_for(*calls: tuple[str, dict]) -> LLMResponse:
    """A turn in which the model requests tools instead of answering."""
    return LLMResponse(
        tool_calls=tuple(
            LLMToolCall(call_id=f"call_{index}", name=name, arguments=arguments)
            for index, (name, arguments) in enumerate(calls, start=1)
        ),
        model=TEST_MODEL,
        usage=LLMUsage(prompt_tokens=48, completion_tokens=12),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def bare(request, client) -> tuple[ApiActor, str]:
    """A company with nothing in it. Enough to test configuration and access."""
    slug = request.node.name.replace("test_", "")[:28]
    admin = register(client, f"bare.{slug}@alphaworks-hq.com", "Copilot-Tests-2026", "Bo Bare")
    company = admin.post(f"{API}/companies", json={"company_name": f"Bare {slug}"})
    assert company.status_code == 201, company.text
    return admin, f"{API}/companies/{company.json()['id']}"


@pytest.fixture
def tenants(request, client, source_fixture) -> dict:
    """Two companies that must never be able to see each other.

    Alpha is a full governed workspace -- connected source, approved scope, a
    profiled table, an ACTIVE KPI imported from the company's own registry, and a
    reference document. Beta is deliberately thin: one document. What matters is
    that each has material carrying an unmistakable marker word.
    """
    slug = request.node.name.replace("test_", "")[:24]

    # ---- Alpha ---------------------------------------------------------
    alpha = register(
        client, f"alpha.{slug}@alphaworks-hq.com", "Alpha-Copilot-2026", "Ada Alpha"
    )
    created = alpha.post(
        f"{API}/companies",
        json={"company_name": f"AlphaWorks {slug}", "currency": "INR", "timezone": "Asia/Kolkata"},
    )
    assert created.status_code == 201, created.text
    alpha_id = created.json()["id"]
    alpha_base = f"{API}/companies/{alpha_id}"

    source = alpha.post(
        f"{alpha_base}/data-sources",
        json={
            "name": "AlphaWorks Warehouse",
            "source_type": "SQLITE",
            "path": source_fixture["path"],
            "refresh_frequency": "DAILY",
            "timezone": "Asia/Kolkata",
        },
    )
    assert source.status_code == 201, source.text
    assert alpha.post(f"{alpha_base}/data-sources/{source.json()['id']}/discover").status_code == 200

    tables = {t["table_name"]: t for t in alpha.get(f"{alpha_base}/tables").json()}
    # Only `orders` is needed: the KPI under test is SUM(orders.order_value), and
    # a narrower scope keeps the fixture cheap without weakening any assertion.
    assert alpha.put(
        f"{alpha_base}/data-scope",
        json={"replace": True, "tables": [{"source_table_id": tables["orders"]["id"], "enabled": True}]},
    ).status_code == 200
    assert alpha.post(f"{alpha_base}/tables/{tables['orders']['id']}/profile").status_code == 200

    imported = alpha.post(
        f"{alpha_base}/kpi-source-definitions/import", json={"kpi_keys": ["revenue"]}
    )
    assert imported.status_code == 201, imported.text
    kpi = imported.json()["imported"][0]
    version_id = kpi["versions"][0]["id"]

    validation = alpha.post(f"{alpha_base}/kpi-versions/{version_id}/validate")
    assert validation.status_code == 200, validation.text
    approved = alpha.post(
        f"{alpha_base}/kpi-versions/{version_id}/approve",
        json={"reason": "Matches the company KPI registry."},
    )
    assert approved.status_code == 200, approved.text

    document = alpha.post(
        f"{alpha_base}/documents",
        data={
            "metadata": json.dumps(
                {
                    "title": "AlphaWorks Revenue Handbook",
                    "document_type": "KPI_HANDBOOK",
                    "access_scope": ["ADMIN", "ANALYST", "EXECUTIVE"],
                    "inline_content": (
                        f"Internal codename {ALPHA_MARKER}. Revenue is the sum of "
                        "order_value across all orders in the period, recognised on "
                        "the order date."
                    ),
                }
            )
        },
    )
    assert document.status_code == 201, document.text

    # ---- Beta ----------------------------------------------------------
    beta = register(client, f"beta.{slug}@betacorp-hq.com", "Beta-Copilot-2026", "Ben Beta")
    other = beta.post(f"{API}/companies", json={"company_name": f"BetaCorp {slug}"})
    assert other.status_code == 201, other.text
    beta_base = f"{API}/companies/{other.json()['id']}"

    beta_document = beta.post(
        f"{beta_base}/documents",
        data={
            "metadata": json.dumps(
                {
                    "title": "BetaCorp Margin Policy",
                    "document_type": "FINANCE_POLICY",
                    "access_scope": ["ADMIN", "ANALYST", "EXECUTIVE"],
                    "inline_content": (
                        f"Internal codename {BETA_MARKER}. Contribution margin excludes "
                        "freight recovery and is reviewed quarterly by the board."
                    ),
                }
            )
        },
    )
    assert beta_document.status_code == 201, beta_document.text

    return {
        "alpha": alpha,
        "alpha_base": alpha_base,
        "alpha_id": alpha_id,
        "kpi_id": kpi["id"],
        "kpi_key": kpi["kpi_key"],
        "kpi_name": kpi["name"],
        "version_id": version_id,
        "beta": beta,
        "beta_base": beta_base,
    }


@pytest.fixture
def scripted(monkeypatch) -> ScriptedModel:
    """Enable the Copilot with a scripted model in place of a real endpoint.

    Configuration is changed the way an operator would -- through settings -- so
    the real ``LLMConfig`` resolution is exercised rather than bypassed.
    """
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_model", TEST_MODEL)
    monkeypatch.setattr(settings, "llm_api_key", SENTINEL_KEY)
    monkeypatch.setattr(settings, "llm_base_url", "http://model.internal:8000/v1")
    monkeypatch.setattr(settings, "llm_tool_calling_enabled", True)

    model = ScriptedModel(get_llm_config())

    def _build(config=None) -> ScriptedModel:
        model.config = config or get_llm_config()
        return model

    monkeypatch.setattr("app.copilot.orchestrator.build_provider", _build)
    return model


@pytest.fixture
def probe_corpus(monkeypatch) -> list[list]:
    """Capture the retrieval corpus assembled inside each Copilot request.

    Built while the request's session is still open, so the captured passages can
    be inspected for the company stamp that makes cross-tenant retrieval
    impossible rather than merely unlikely.
    """
    captured: list[list] = []
    original = copilot_api.build_context

    def _capturing(*args, **kwargs):
        context = original(*args, **kwargs)
        captured.append(build_corpus(context))
        return context

    monkeypatch.setattr(copilot_api, "build_context", _capturing)
    return captured


def ask(actor: ApiActor, base: str, message: str, **context) -> dict:
    response = actor.post(f"{base}/copilot/chat", json={"message": message, "context": context})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. The platform runs, and answers honestly, with no model configured
# ---------------------------------------------------------------------------
def test_status_is_honest_when_no_model_is_configured(bare):
    """The default deployment has no model, and that is a state, not a failure."""
    admin, base = bare
    response = admin.get(f"{base}/copilot/status")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["enabled"] is False
    assert body["available"] is False
    assert "LLM_ENABLED=false" in body["unavailable_reason"]
    # The reason has to tell the user the platform still works, or the UI will
    # render this as an outage.
    assert "continue to work" in body["unavailable_reason"]

    # Nothing about a model that is not there, and never a credential.
    assert body["model"] is None
    assert body["endpoint_host"] is None
    assert SENTINEL_KEY not in response.text
    assert "api_key" not in response.text.lower()


def test_status_reports_the_reach_this_caller_actually_has(bare):
    """``tools_available`` is the caller's own entitlement, not a catalogue.

    This also pins the status endpoint against a regression that made it raise:
    the permission filter takes an ``AccessContext``, which is all it needs, and
    is reachable without opening a Copilot turn.
    """
    admin, base = bare
    body = admin.get(f"{base}/copilot/status").json()

    assert body["tools_available"], "an admin must be offered the governed tools"
    assert set(body["tools_available"]) == set(REGISTRY.names)
    assert sorted(body["tools_available"]) == body["tools_available"], "listed in a stable order"

    assert body["knowledge_sources"], "an admin can read governed metadata and documents"
    assert any("KPI contracts" in source for source in body["knowledge_sources"])

    # Nothing SQL-shaped is ever advertised.
    for name in body["tools_available"]:
        assert not any(word in name for word in ("sql", "query", "connect", "credential"))


def test_planned_capabilities_are_named_but_never_implemented():
    """The monitoring engine does not exist, and no stub pretends otherwise.

    The Copilot needs to be able to say "that will be answerable when monitoring
    ships". It must not be able to *answer* it -- a tool returning a plausible
    expected value or anomaly verdict is the exact failure this platform is built
    to avoid.
    """
    assert PLANNED_TOOL_NAMES, "the planned services should be documented"
    for name in PLANNED_TOOL_NAMES:
        assert name not in REGISTRY, f"{name} is listed as planned but is registered"

    for name in REGISTRY.names:
        assert not any(
            word in name
            for word in ("anomaly", "expected", "baseline", "forecast", "monitoring")
        ), f"{name} implies analytical machinery this version does not have"


def test_chat_answers_from_governed_evidence_with_no_model_configured(tenants):
    """A disabled Copilot still returns the platform's own retrieved evidence."""
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "What does the revenue KPI mean?",
        kpi_id=tenants["kpi_id"],
    )

    assert body["llm_available"] is False
    assert body["unavailable_reason"], "the response must say why it could not answer"
    assert body["model"] is None
    assert body["usage"] == {}
    assert body["tool_calls"] == []

    # Retrieval is the platform's work, not the model's, so it still happened.
    assert body["evidence"], "governed retrieval must work without a model"
    assert any(item["source_type"] == "kpi_contract" for item in body["evidence"])

    # Context was resolved server-side and echoed back.
    assert body["context"]["company_id"] == tenants["alpha_id"]
    assert body["context"]["kpi_key"] == tenants["kpi_key"]


def test_a_disabled_copilot_records_no_model_usage(tenants):
    """``llm_calls`` must stay empty when nothing contacted a model."""
    ask(tenants["alpha"], tenants["alpha_base"], "Explain the revenue definition.")

    telemetry = tenants["alpha"].get(f"{tenants['alpha_base']}/telemetry")
    assert telemetry.status_code == 200, telemetry.text
    rows = [row for row in telemetry.json() if row["operation"].endswith("/copilot/chat")]
    assert rows, "the Copilot request should be in the execution log"

    for row in rows:
        assert row["service"] == "copilot"
        assert row["llm_model"] is None
        assert row["llm_calls"] is None
        assert row["prompt_tokens"] is None
        assert row["estimated_cost_usd"] is None


def test_build_provider_returns_the_null_provider_when_disabled():
    """No transport, and therefore no AI dependency, is imported when disabled."""
    config = llm_config_from_settings(settings)
    assert config.enabled is False
    provider = build_provider(config)
    assert isinstance(provider, NullProvider)


@pytest.mark.anyio
async def test_the_null_provider_refuses_rather_than_improvising():
    """Asking a non-existent model for an answer raises, never returns prose."""
    provider = NullProvider(llm_config_from_settings(settings))
    with pytest.raises(LLMUnavailable):
        await provider.generate([])


# ---------------------------------------------------------------------------
# 2. Authorisation: the URL's company_id is a claim, never a permission
# ---------------------------------------------------------------------------
def test_copilot_requires_authentication(bare, client):
    _admin, base = bare
    assert client.get(f"{base}/copilot/status").status_code == 401
    assert client.post(f"{base}/copilot/chat", json={"message": "hello"}).status_code == 401


def test_copilot_refuses_another_companys_admin(tenants):
    """Beta's admin is an admin -- of Beta. Alpha's Copilot is closed to them."""
    beta, alpha_base = tenants["beta"], tenants["alpha_base"]

    assert beta.get(f"{alpha_base}/copilot/status").status_code == 403
    denied = beta.post(f"{alpha_base}/copilot/chat", json={"message": "Explain revenue."})
    assert denied.status_code == 403
    # The refusal must not confirm what is inside Alpha.
    assert tenants["kpi_name"] not in denied.text
    assert ALPHA_MARKER not in denied.text


def test_the_request_body_cannot_carry_a_company_or_sql(tenants):
    """The schema forbids the fields that would make the client authoritative."""
    alpha, base = tenants["alpha"], tenants["alpha_base"]
    for smuggled in ("company_id", "sql", "tool", "system_prompt", "permissions"):
        response = alpha.post(
            f"{base}/copilot/chat",
            json={"message": "Explain revenue.", "context": {smuggled: "x"}},
        )
        assert response.status_code == 422, f"{smuggled} was accepted: {response.text}"


# ---------------------------------------------------------------------------
# 3. Company isolation: retrieval, the prompt, and the tools
# ---------------------------------------------------------------------------
def test_retrieval_cannot_reach_another_company(tenants, scripted, probe_corpus):
    """Alpha asking Beta's private codename retrieves nothing of Beta's.

    Checked three ways, because only the third is structural: the answer, the
    evidence, and the corpus that retrieval assembled inside the request.
    """
    scripted.script(says("I have no material matching that term."))
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        f"What is {BETA_MARKER} and what margin policy does it describe?",
    )

    blob = json.dumps(body)
    assert BETA_MARKER not in blob
    assert "freight recovery" not in blob
    assert "reviewed quarterly by the board" not in blob

    # Nothing of Beta's reached the model either. The marker word itself is in
    # the prompt because the user typed it into the question -- what matters is
    # that none of Beta's material came back attached to it.
    assert "freight recovery" not in scripted.sent_text
    assert "reviewed quarterly by the board" not in scripted.sent_text
    assert "BetaCorp" not in scripted.sent_text

    # Structural: every passage retrieval considered is stamped with Alpha.
    assert probe_corpus, "the corpus probe did not run"
    corpus = probe_corpus[0]
    assert corpus, "Alpha's own corpus should not be empty"
    assert {passage.company_id for passage in corpus} == {tenants["alpha_id"]}
    assert not any(BETA_MARKER in passage.content for passage in corpus)
    # Alpha's own marker *is* there, which is what proves the search would have
    # found Beta's had isolation failed.
    assert any(ALPHA_MARKER in passage.content for passage in corpus)


def test_retrieval_is_isolated_in_both_directions(tenants, scripted, probe_corpus):
    """The thin tenant cannot see the rich one either."""
    scripted.script(says("Nothing in this company's material matches that."))
    body = ask(
        tenants["beta"],
        tenants["beta_base"],
        f"Explain {ALPHA_MARKER} and the revenue handbook it belongs to.",
    )

    blob = json.dumps(body)
    # Beta's own material does not match the question, so nothing is retrieved
    # and the answer says so -- quoting the question back, which is why the
    # marker the user typed appears here. Alpha's material does not.
    assert body["evidence"] == []
    assert "order_value" not in blob
    assert "AlphaWorks" not in blob
    assert "Revenue Handbook" not in blob
    assert ALPHA_MARKER not in scripted.sent_text

    corpus = probe_corpus[0]
    assert corpus, "Beta's own document should be retrievable"
    assert not any(ALPHA_MARKER in passage.content for passage in corpus)
    assert any(BETA_MARKER in passage.content for passage in corpus)


def test_a_kpi_id_from_another_company_resolves_to_nothing(tenants, scripted):
    """A client hint is re-resolved in the caller's company, or reported missing."""
    scripted.script(says("That reference was not found in this company."))
    body = ask(
        tenants["beta"],
        tenants["beta_base"],
        "Explain the KPI on my screen.",
        kpi_id=tenants["kpi_id"],
    )

    assert body["context"]["kpi_definition_id"] is None
    assert body["context"]["kpi_name"] is None
    assert any("not found in this company" in note for note in body["context"]["notes"])
    # A neighbouring tenant's id must not be confirmed as existing.
    assert tenants["kpi_name"] not in json.dumps(body)


def test_a_tool_call_naming_another_companys_kpi_is_refused(tenants, scripted):
    """The model asking for Alpha's KPI as Beta gets a refusal, not a query."""
    scripted.script(
        asks_for(("get_kpi_definition", {"kpi": tenants["kpi_id"]})),
        says("That KPI is not available in this company."),
    )
    # The question has to match something of Beta's, or retrieval comes back
    # empty and the turn ends before the model is ever offered a tool.
    body = ask(
        tenants["beta"],
        tenants["beta_base"],
        "Explain the margin policy and the KPI contract behind it.",
    )

    assert len(body["tool_calls"]) == 1
    call = body["tool_calls"][0]
    assert call["tool"] == "get_kpi_definition"
    assert call["ok"] is False
    assert call["error"]
    # The refusal itself must not leak what lives in the other company.
    assert tenants["kpi_name"] not in json.dumps(body)
    assert ALPHA_MARKER not in scripted.sent_text


def test_the_model_cannot_supply_a_company_argument(tenants, scripted):
    """Even a well-formed tool call is rejected if it names a tenant."""
    scripted.script(
        asks_for(
            (
                "get_kpi_definition",
                {"kpi": tenants["kpi_key"], "company_id": tenants["alpha_id"]},
            )
        ),
        says("I cannot select a company."),
    )
    body = ask(
        tenants["beta"],
        tenants["beta_base"],
        "Explain the margin policy and the KPI contract behind it.",
    )

    call = body["tool_calls"][0]
    assert call["ok"] is False
    assert "company_id" in call["error"]
    assert "Unknown parameter" in call["error"]


def test_evidence_from_another_company_cannot_enter_a_bundle():
    """The last line of defence, and it is one line rather than a convention."""
    bundle = EvidenceBundle(company_id="company-alpha")
    bundle.add(source_type="kpi_contract", title="Revenue", content="SUM(orders.order_value)")
    assert len(bundle) == 1

    with pytest.raises(ValueError, match="another company"):
        bundle.add(
            source_type="kpi_contract",
            title="Contribution Margin",
            content=BETA_MARKER,
            company_id="company-beta",
        )
    assert len(bundle) == 1
    assert BETA_MARKER not in bundle.as_prompt()


# ---------------------------------------------------------------------------
# 4. The model cannot reach a database, a connection or a credential
# ---------------------------------------------------------------------------
def test_no_tool_exposes_sql_a_connection_or_a_credential():
    """Checked over the registry, so a future tool cannot slip past review."""
    assert len(REGISTRY) > 0

    for name in REGISTRY.names:
        spec = REGISTRY.get(name)
        assert spec is not None
        declared = set(spec.parameters.get("properties") or {})
        assert not (declared & FORBIDDEN_PARAMETERS), (
            f"{name} declares a forbidden parameter: {sorted(declared & FORBIDDEN_PARAMETERS)}"
        )
        # The JSON Schema is what the model reads; it must not even hint at SQL.
        schema = json.dumps(spec.parameters).lower()
        for word in ('"sql"', "connection_string", "credential", "password", "secret"):
            assert word not in schema, f"{name}'s schema mentions {word}"

    banned = ("execute_sql", "run_sql", "run_query", "raw_query", "database_connection",
              "get_connection", "get_credentials", "read_rows", "select_rows")
    for name in banned:
        assert name not in REGISTRY, f"{name} must never be a tool"


def test_a_tool_declaring_a_forbidden_parameter_cannot_be_registered():
    """The guarantee is structural: registration fails, at import time."""
    registry = ToolRegistry()

    for bad in ("company_id", "sql", "connection_string", "api_key"):
        spec = ToolSpec(
            name=f"tool_with_{bad}",
            description="A tool that should never exist.",
            permissions=("kpi.read",),
            parameters={"type": "object", "properties": {bad: {"type": "string"}}, "required": []},
            handler=lambda context, arguments: refuse("unreachable"),
        )
        with pytest.raises(ValueError, match="forbidden parameter"):
            registry.register(spec)

    assert len(registry) == 0


def test_the_model_cannot_invent_a_tool(tenants, scripted):
    """A call to a tool that does not exist comes back as a readable refusal."""
    scripted.script(
        asks_for(("execute_sql", {"statement": "select * from orders"})),
        says("I have no way to run a query."),
    )
    body = ask(tenants["alpha"], tenants["alpha_base"], "Explain the revenue contract.")

    call = body["tool_calls"][0]
    assert call["tool"] == "execute_sql"
    assert call["ok"] is False
    assert "no tool named" in call["error"]
    # A refusal keeps the turn alive rather than 500-ing.
    assert body["answer"]


def test_the_orchestration_layer_never_names_a_model_or_provider():
    """Provider choice is an environment change, not a code change.

    Asserted over the source, because a single ``if model == "Qwen"`` outside the
    transport is what turns a swappable provider into a hardcoded one.
    """
    roots = (Path("app/copilot"), Path("app/api/v1"), Path("app/services"), Path("app/models"))
    forbidden = ("qwen", "vllm", "openai", "anthropic", "llama", "gpt-", "ollama", "gemini")

    checked = 0
    for root in roots:
        assert root.is_dir(), f"{root} not found -- run pytest from backend/"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            checked += 1
            for word in forbidden:
                assert word not in text, f"{path} names a model or provider: {word}"
    assert checked > 10, "the source scan did not find the modules it was meant to check"


# ---------------------------------------------------------------------------
# 5. Explanation: the governed contract, and the recorded validation run
# ---------------------------------------------------------------------------
def test_a_kpi_definition_is_explained_from_the_governed_contract(tenants, scripted):
    """The KPI's meaning comes from the contract, retrieved and cited."""
    scripted.script(
        asks_for(("get_kpi_definition", {"kpi": tenants["kpi_key"]})),
        says("Revenue is the sum of order_value across the period [E1]."),
    )
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "What exactly does this KPI measure?",
        kpi_id=tenants["kpi_id"],
    )

    assert body["llm_available"] is True
    assert body["model"] == TEST_MODEL
    assert body["iterations"] == 2
    assert body["truncated"] is False

    call = body["tool_calls"][0]
    assert call["tool"] == "get_kpi_definition"
    assert call["ok"] is True, call["error"]

    contracts = [item for item in body["evidence"] if item["source_type"] == "kpi_contract"]
    assert contracts, "the governed contract should be in the evidence"
    assert any("SUM(orders.order_value)" in item["content"] for item in contracts)

    # The model was offered the caller's tools, and evidence was in the prompt.
    assert "get_kpi_definition" in scripted.offered_tools[0]
    assert "SUM(orders.order_value)" in scripted.sent_text

    # Usage was accounted for on this request.
    assert body["usage"]["calls"] == 2
    assert body["usage"]["prompt_tokens"] > 0


def test_validation_state_is_explained_from_the_recorded_run(tenants, scripted):
    """The Copilot reads the stored validation result; it runs nothing."""
    scripted.script(
        asks_for(("get_kpi_validation_summary", {"kpi": tenants["kpi_key"]})),
        says("The last validation run passed every blocking check [E1]."),
    )
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "Is this KPI ready for approval, and what did validation find?",
        kpi_id=tenants["kpi_id"],
    )

    call = body["tool_calls"][0]
    assert call["tool"] == "get_kpi_validation_summary"
    assert call["ok"] is True, call["error"]

    validations = [item for item in body["evidence"] if item["source_type"] == "kpi_validation"]
    assert validations, "the recorded validation run should be cited"
    assert any(
        "FORMULA_PARSES" in item["content"] or "ready" in item["content"].lower()
        for item in validations
    )


def test_the_copilot_cannot_change_governance(tenants, scripted):
    """Every tool is a read. Approving, activating and editing are not offered."""
    scripted.script(says("I can explain the contract but not change its state."))
    ask(tenants["alpha"], tenants["alpha_base"], "Explain this KPI.", kpi_id=tenants["kpi_id"])

    offered = set(scripted.offered_tools[0])
    assert offered, "the model should have been offered tools"
    for verb in ("approve", "activate", "reject", "deprecate", "submit", "create",
                 "update", "delete", "import", "validate_kpi", "write"):
        assert not any(verb in name for name in offered), f"a tool implies {verb}"
    assert all(name.startswith("get_") for name in offered)

    # The KPI is exactly as governance left it.
    kpi = tenants["alpha"].get(f"{tenants['alpha_base']}/kpis/{tenants['kpi_id']}").json()
    assert kpi["definition"]["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# 6. Missing evidence, and the figure behind a displayed number
# ---------------------------------------------------------------------------
def test_a_question_with_no_matching_evidence_never_reaches_the_model(tenants, scripted):
    """Nothing retrieved means nothing to answer from -- so no model call.

    Sending an unanswerable question to a model is how a governed platform
    acquires a hallucination.
    """
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "vorpalglint thrummedy skewbald prattlewick",
    )

    assert body["llm_available"] is True
    assert body["evidence"] == []
    assert body["tool_calls"] == []
    assert scripted.prompts == [], "the model must not be asked to fill the gap"
    assert body["iterations"] == 0

    lowered = body["answer"].lower()
    # The answer has to say plainly that it found nothing, and say what it *can*
    # answer from -- not offer a guess dressed up as a hedge.
    assert "could not find anything" in lowered
    assert "i can only answer from what this platform records" in lowered


def test_a_stored_detection_result_is_the_evidence_behind_the_figure(
    tenants, scripted, source_fixture
):
    """A figure on screen is a detection run, and the Copilot answers from it.

    This is the property that makes the layer useful rather than merely safe. The
    engine evaluated the KPI at its registered source, so the actual, expected,
    deviation and status are measurements: they must reach the model as evidence,
    unmarked, carrying the same numbers the detection API returned. Anything less
    and a question about a real result gets answered with "no figure available".
    """
    target = date.fromisoformat(source_fixture["reference_date"]) - timedelta(days=7)
    executed = tenants["alpha"].post(
        f"{tenants['alpha_base']}/run-detection",
        json={"kpi_id": tenants["kpi_id"], "target_date": target.isoformat()},
    )
    assert executed.status_code == 200, executed.text
    run = executed.json()
    assert run["persisted"] is True
    result = run["result"]
    assert result["actual"] is not None, (
        "the fixture source holds orders on that date, so an actual must be measured"
    )

    scripted.script(says(f"{tenants['kpi_name']} was measured at that level [E1]."))
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "What was revenue on the selected date?",
        kpi_id=tenants["kpi_id"],
        selected_date=target.isoformat(),
    )

    measured = [item for item in body["evidence"] if item["source_type"] == "detection_run"]
    assert measured, "the stored detection result must be attached as evidence"
    item = measured[0]
    assert item["is_placeholder"] is False, "a measurement must not be marked as absent"
    assert item["source_id"] == run["run_id"]
    assert item["metadata"]["status"] == result["status"]
    assert item["metadata"]["target_date"] == target.isoformat()
    # The figure in the evidence is the figure the engine reported, formatted.
    assert f"{result['actual']:,.2f}" in item["content"]
    assert result["status"] in item["content"]

    # Nothing tells the model to withhold a number it genuinely has, and no
    # caveat claims the date was never evaluated.
    assert "NOT A MEASUREMENT" not in scripted.sent_text
    assert not any("No detection run is stored" in caveat for caveat in body["caveats"])

    # The approver's statistics stay on the detection API: a business explanation
    # does not carry the median, the MAD or the z-score.
    for technical in ("modified z", "median", "MAD"):
        assert technical.lower() not in item["content"].lower()


def test_a_figure_with_no_stored_detection_run_is_disclosed_as_unmeasured(tenants, scripted):
    """A question about a number nothing was evaluated for gets the disclosure.

    Detection results exist only for dates the agent was actually run on. Letting
    a model narrate a figure for any other date -- or infer an anomaly against a
    baseline that was never computed -- is the single most damaging thing this
    feature could do, so the absence is evidence in its own right rather than a
    prompt instruction the model may ignore.
    """
    scripted.script(says("No result has been stored for that date [E1]."))
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "Why did revenue drop today?",
        kpi_id=tenants["kpi_id"],
        selected_date="2019-04-15",
    )

    notices = [item for item in body["evidence"] if item["is_placeholder"]]
    assert notices, "a question about a displayed figure must carry the disclosure"
    notice = notices[0]
    assert notice["source_type"] == "detection_run"
    assert notice["source_id"] is None
    for phrase in ("no stored detection run", "Do not state or estimate any figure"):
        assert phrase in notice["content"]

    # The caveat is attached regardless of what the model wrote.
    assert any("No detection run is stored" in caveat for caveat in body["caveats"])

    # And the model was told, in the evidence block, that it is not a measurement.
    assert "NOT A MEASUREMENT" in scripted.sent_text


def test_the_unmeasured_notice_is_added_even_without_a_selected_date(tenants, scripted):
    scripted.script(says("No expected value has been computed for that."))
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "Is this figure anomalous compared with the expected value?",
        kpi_id=tenants["kpi_id"],
    )
    assert any(item["is_placeholder"] for item in body["evidence"])


# ---------------------------------------------------------------------------
# 7. Prompt handling, bounds, and failure
# ---------------------------------------------------------------------------
def test_retrieved_text_is_never_part_of_the_system_prompt(tenants, scripted):
    """Document text is data, not instruction.

    Evidence goes in the user turn. A policy document that happens to contain
    "ignore your previous instructions" is then quoted material rather than a
    second system prompt.
    """
    scripted.script(says("Understood."))
    ask(tenants["alpha"], tenants["alpha_base"], "Summarise the revenue handbook.")

    assert ALPHA_MARKER in scripted.sent_text, "the document should have been retrieved"
    assert ALPHA_MARKER not in scripted.system_text
    # The system turn carries rules, and says so.
    assert "GOVERNANCE" in scripted.system_text


def test_a_model_that_keeps_asking_for_tools_is_bounded(tenants, scripted):
    """The loop is bounded, and a truncated answer is labelled as one."""
    limit = get_llm_config().max_tool_iterations
    scripted.script(*[asks_for(("get_active_kpis", {})) for _ in range(limit)])

    body = ask(tenants["alpha"], tenants["alpha_base"], "List and explain every KPI.")

    assert body["truncated"] is True
    assert body["iterations"] == limit + 1
    assert len(scripted.prompts) == limit + 1
    # The last turn was asked to answer with no tools available.
    assert scripted.offered_tools[-1] == ()
    assert any("tool-call limit" in caveat for caveat in body["caveats"])


def test_a_failing_model_endpoint_still_returns_governed_evidence(tenants, scripted):
    """The endpoint is down; the platform's own retrieval is not."""
    scripted.script(LLMProviderError("Could not reach the model endpoint at model.internal:8000."))
    body = ask(
        tenants["alpha"],
        tenants["alpha_base"],
        "What does revenue mean?",
        kpi_id=tenants["kpi_id"],
    )

    assert body["evidence"], "retrieved evidence survives a model failure"
    assert "could not be reached" in body["answer"]
    assert any("Model request failed" in caveat for caveat in body["caveats"])
    # Reported without the credential or the full URL.
    assert SENTINEL_KEY not in json.dumps(body)


def test_an_unavailable_model_mid_turn_is_reported_not_raised(tenants, scripted):
    scripted.script(LLMUnavailable("The model was unloaded."))
    body = ask(tenants["alpha"], tenants["alpha_base"], "What does revenue mean?")

    assert body["llm_available"] is False
    assert body["unavailable_reason"] == "The model was unloaded."
    assert body["evidence"]


# ---------------------------------------------------------------------------
# 8. What is recorded, and what is deliberately not
# ---------------------------------------------------------------------------
def test_model_usage_is_recorded_on_the_execution_log(tenants, scripted):
    scripted.script(
        asks_for(("get_active_kpis", {})),
        says("There is one active KPI [E1]."),
    )
    ask(tenants["alpha"], tenants["alpha_base"], "Which KPIs are active?")

    telemetry = tenants["alpha"].get(f"{tenants['alpha_base']}/telemetry").json()
    row = next(r for r in telemetry if r["operation"].endswith("/copilot/chat"))

    assert row["llm_model"] == TEST_MODEL
    assert row["llm_calls"] == 2
    assert row["prompt_tokens"] == 48 + 64
    assert row["completion_tokens"] == 12 + 25
    # Self-hosted and zero-rated, recorded as such rather than estimated.
    assert row["estimated_cost_usd"] is None
    assert row["status"] == "OK"

    # Nothing in the log resembles a prompt or a credential.
    blob = json.dumps(row)
    assert SENTINEL_KEY not in blob
    assert "Which KPIs are active?" not in blob


def test_the_audit_trail_records_the_question_without_its_text(tenants, scripted):
    """That a question was asked is auditable. Its free text is not stored.

    A user's question can quote business figures, and the audit trail is not the
    place for them. What an auditor needs is the context and the tools that ran.
    """
    nonce = "GLURP4457"
    scripted.script(
        asks_for(("get_kpi_definition", {"kpi": tenants["kpi_key"]})),
        says("Revenue is defined by the governed contract [E1]."),
    )
    ask(
        tenants["alpha"],
        tenants["alpha_base"],
        f"{nonce} explain the revenue contract please",
        kpi_id=tenants["kpi_id"],
        page="dashboard",
    )

    audit = tenants["alpha"].get(f"{tenants['alpha_base']}/audit")
    assert audit.status_code == 200, audit.text
    entries = audit.json()
    entries = entries["entries"] if isinstance(entries, dict) else entries

    asked = [e for e in entries if e["action"] == copilot_api.COPILOT_QUESTION_ASKED]
    assert asked, "asking the Copilot must be auditable"
    entry = asked[0]
    assert entry["resource_type"] == "copilot"
    assert entry["details"]["page"] == "dashboard"
    assert entry["details"]["tools_called"] == ["get_kpi_definition"]
    assert entry["details"]["evidence_count"] > 0
    assert entry["details"]["message_chars"] == len(f"{nonce} explain the revenue contract please")

    # The question text itself is nowhere in the trail.
    assert nonce not in audit.text
    assert SENTINEL_KEY not in audit.text


def test_the_api_key_never_leaves_configuration(tenants, scripted):
    """No response body, log or audit entry may carry the model credential."""
    config = get_llm_config()
    assert config.api_key == SENTINEL_KEY
    described = config.describe()
    assert "api_key" not in described
    assert SENTINEL_KEY not in json.dumps(described)
    # The host is reportable; the URL, which can carry a token, is not.
    assert described["endpoint_host"] == "model.internal:8000"

    scripted.script(says("Revenue is defined by its contract."))
    alpha, base = tenants["alpha"], tenants["alpha_base"]

    status = alpha.get(f"{base}/copilot/status")
    chat = alpha.post(f"{base}/copilot/chat", json={"message": "What does revenue mean?"})
    assert chat.status_code == 200, chat.text

    for response in (status, chat, alpha.get(f"{base}/audit"), alpha.get(f"{base}/telemetry")):
        assert SENTINEL_KEY not in response.text
        assert "http://model.internal:8000/v1" not in response.text


def test_no_hidden_reasoning_reaches_the_response(tenants, scripted):
    """Chain-of-thought is stripped by the transport contract, not by the UI."""
    from app.llm.provider import strip_reasoning

    assert strip_reasoning("<think>secret plan</think>Answer.") == "Answer."
    # A truncated generation can open a block and never close it.
    assert strip_reasoning("<think>unfinished") == ""
    assert strip_reasoning("Answer.</think>") == "Answer."

    scripted.script(says("Revenue is the sum of order_value."))
    body = ask(tenants["alpha"], tenants["alpha_base"], "What does revenue mean?")
    assert "<think>" not in json.dumps(body)


# ---------------------------------------------------------------------------
# 12. The OpenAI-compatible transport against a real server's wire shape
# ---------------------------------------------------------------------------
# The tests above script the *provider*, which is right for asserting things
# about the governed layer but leaves the transport itself uncovered. These
# replay payloads recorded from Ollama 0.33 serving qwen3:8b -- a reasoning model
# behind an OpenAI-compatible endpoint -- through the real transport over a mock
# HTTP layer. No model or network is needed, so the integration that
# ``verify_ollama.py`` checks live stays pinned in CI.
#
# The shape that matters: Ollama returns deliberation in a separate
# ``reasoning`` field rather than in an inline ``<think>`` block, on both the
# non-streaming message and every streaming delta. A transport that only knew
# how to strip ``<think>`` would pass its own tests and still leak.
def _ollama_provider(handler) -> "OpenAICompatibleProvider":
    """A real transport whose HTTP layer is a recorded-response mock."""
    import dataclasses

    import httpx

    from app.llm.openai_compatible import OpenAICompatibleProvider

    config = dataclasses.replace(
        llm_config_from_settings(settings),
        enabled=True,
        provider="openai_compatible",
        base_url="http://localhost:11434/v1",
        api_key="EMPTY",
        model="qwen3:8b",
    )
    provider = OpenAICompatibleProvider(config)
    # Let the provider build its own client -- base URL, auth header and timeout
    # are part of what is under test -- then swap only the network layer beneath
    # it, so the request that reaches ``handler`` is the real one.
    client = provider._http()
    client._transport = httpx.MockTransport(handler)
    return provider


# Recorded from Ollama: thinking in `reasoning`, the answer in `content`.
_OLLAMA_PLAIN = {
    "id": "chatcmpl-473",
    "object": "chat.completion",
    "model": "qwen3:8b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Revenue is the sum of order_value.",
                "reasoning": (
                    "Okay, the user is asking what revenue means. Let me check the "
                    "evidence. The handbook says SECRET_DELIBERATION here."
                ),
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 16, "completion_tokens": 138, "total_tokens": 154},
}

# Recorded from Ollama: a tool request. Content is empty, arguments are a JSON
# *string*, and the call carries both an `id` and an `index`.
_OLLAMA_TOOL_CALL = {
    "model": "qwen3:8b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "",
                "reasoning": "The user wants a KPI definition. SECRET_DELIBERATION.",
                "tool_calls": [
                    {
                        "id": "call_ot90tyn8",
                        "index": 0,
                        "type": "function",
                        "function": {
                            "name": "get_kpi_definition",
                            "arguments": '{"name":"Gross Margin"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 168, "completion_tokens": 116, "total_tokens": 284},
}


@pytest.mark.anyio
async def test_ollamas_reasoning_field_never_reaches_the_response():
    """qwen3 puts thinking in `reasoning`, not `<think>`. It must still be dropped."""
    import httpx

    provider = _ollama_provider(lambda request: httpx.Response(200, json=_OLLAMA_PLAIN))
    try:
        response = await provider.generate([LLMMessage.user("What does revenue mean?")])
    finally:
        await provider.aclose()

    assert response.text == "Revenue is the sum of order_value."
    # The field is read only so that it can be discarded; nothing carries it up.
    assert "SECRET_DELIBERATION" not in response.text
    # Stronger than checking `text`: no field of the response holds it, and there
    # is no reasoning attribute for a future caller to reach for.
    import dataclasses

    assert "SECRET_DELIBERATION" not in json.dumps(dataclasses.asdict(response), default=str)
    assert not hasattr(response, "reasoning")
    assert not hasattr(response, "reasoning_content")
    assert response.model == "qwen3:8b"
    assert response.usage.prompt_tokens == 16
    assert response.usage.completion_tokens == 138
    assert response.finish_reason == "stop"
    assert response.tool_calls == ()


@pytest.mark.anyio
async def test_ollamas_tool_call_shape_is_understood_by_the_transport():
    """A qwen3 tool request must arrive as an executable, well-typed call."""
    import httpx

    provider = _ollama_provider(lambda request: httpx.Response(200, json=_OLLAMA_TOOL_CALL))
    try:
        response = await provider.generate(
            [LLMMessage.user("Define Gross Margin.")],
            tools=[
                LLMToolSpec(
                    name="get_kpi_definition",
                    description="Return a KPI definition.",
                    parameters={"type": "object", "properties": {"name": {"type": "string"}}},
                )
            ],
        )
    finally:
        await provider.aclose()

    assert response.wants_tools is True
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "get_kpi_definition"
    # The arguments arrive as a JSON string and must be decoded, not passed on.
    assert call.arguments == {"name": "Gross Margin"}
    assert call.argument_error is None
    assert call.call_id == "call_ot90tyn8"
    # An empty content field alongside a tool call is normal, not an error.
    assert response.text == ""
    assert "SECRET_DELIBERATION" not in response.text
    assert response.finish_reason == "tool_calls"


@pytest.mark.anyio
async def test_the_request_sent_to_an_openai_compatible_server_is_well_formed():
    """What the transport puts on the wire, checked once against a real shape."""
    import httpx

    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        sent["_path"] = request.url.path
        sent["_auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_OLLAMA_PLAIN)

    provider = _ollama_provider(handler)
    try:
        await provider.generate(
            [
                LLMMessage.system("You are governed."),
                LLMMessage.user("What does revenue mean?"),
                LLMMessage.assistant(None, [LLMToolCall(call_id="c1", name="get_active_kpis")]),
                LLMMessage.tool_result(call_id="c1", name="get_active_kpis", payload={"ok": True}),
            ],
            tools=[
                LLMToolSpec(
                    name="get_active_kpis", description="List active KPIs.", parameters={"type": "object"}
                )
            ],
        )
    finally:
        await provider.aclose()

    assert sent["_path"] == "/v1/chat/completions"
    assert sent["_auth"] == "Bearer EMPTY"
    assert sent["model"] == "qwen3:8b"
    assert sent["stream"] is False
    # Local Qwen3 may spend the entire token budget in hidden thinking. The
    # optional control is forwarded only when configured.
    assert sent["reasoning_effort"] == "none"
    # "auto", never "required": an answerable question should be answered.
    assert sent["tool_choice"] == "auto"
    assert sent["tools"][0]["type"] == "function"
    assert sent["tools"][0]["function"]["name"] == "get_active_kpis"

    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    # A tool-only assistant turn sends "" rather than null: servers differ on
    # which of the two they reject, and every one of them accepts "".
    assert sent["messages"][2]["content"] == ""
    assert sent["messages"][2]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert sent["messages"][3]["tool_call_id"] == "c1"
    assert sent["messages"][3]["name"] == "get_active_kpis"


@pytest.mark.anyio
async def test_reasoning_is_dropped_from_every_streaming_delta():
    """Ollama repeats `reasoning` on each delta while `content` stays empty."""
    import httpx

    # Recorded shape: reasoning streams first with content "", then the answer
    # arrives in content, then a final usage-only frame.
    frames = [
        {"model": "qwen3:8b", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "", "reasoning": "Okay, SECRET"}}]},
        {"model": "qwen3:8b", "choices": [{"index": 0, "delta": {"content": "", "reasoning": "_DELIBERATION continues"}}]},
        {"model": "qwen3:8b", "choices": [{"index": 0, "delta": {"content": "Revenue is "}}]},
        {"model": "qwen3:8b", "choices": [{"index": 0, "delta": {"content": "the sum of order_value."}, "finish_reason": "stop"}]},
        {"model": "qwen3:8b", "choices": [], "usage": {"prompt_tokens": 18, "completion_tokens": 164}},
    ]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"

    provider = _ollama_provider(
        lambda request: httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
        )
    )
    try:
        response = await provider.generate([LLMMessage.user("What does revenue mean?")], stream=True)
    finally:
        await provider.aclose()

    assert response.text == "Revenue is the sum of order_value."
    assert "SECRET" not in response.text
    assert "DELIBERATION" not in response.text
    assert response.usage.prompt_tokens == 18
    assert response.usage.completion_tokens == 164
    assert response.finish_reason == "stop"


@pytest.mark.anyio
async def test_a_think_block_split_across_streaming_deltas_is_still_stripped():
    """A server that inlines reasoning can split the tag pair across frames.

    Stripping per delta would miss it, which is why the transport joins first
    and scrubs the assembled text.
    """
    import httpx

    frames = [
        {"choices": [{"delta": {"content": "<thi"}}]},
        {"choices": [{"delta": {"content": "nk>SECRET plan"}}]},
        {"choices": [{"delta": {"content": "</think>Revenue is governed."}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"

    provider = _ollama_provider(
        lambda request: httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
        )
    )
    try:
        response = await provider.generate([LLMMessage.user("What does revenue mean?")], stream=True)
    finally:
        await provider.aclose()

    assert response.text == "Revenue is governed."
    assert "SECRET" not in response.text


@pytest.mark.anyio
async def test_streamed_tool_call_fragments_are_reassembled():
    """Arguments arrive character by character and must rejoin into valid JSON."""
    import httpx

    frames = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_x1", "type": "function", "function": {"name": "get_kpi_definition", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"na'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'me":"Gross'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' Margin"}'}}]}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"

    provider = _ollama_provider(
        lambda request: httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
        )
    )
    try:
        response = await provider.generate([LLMMessage.user("Define Gross Margin.")], stream=True)
    finally:
        await provider.aclose()

    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.call_id == "call_x1"
    assert call.name == "get_kpi_definition"
    assert call.arguments == {"name": "Gross Margin"}
    assert call.argument_error is None


@pytest.mark.anyio
async def test_a_model_that_returns_only_reasoning_yields_an_honest_empty_answer():
    """Thinking can consume the whole token budget, leaving no content.

    An 8B reasoning model with a tight ``max_tokens`` does this in practice. The
    transport must return empty text rather than fall back to the reasoning.
    """
    import httpx

    payload = {
        "model": "qwen3:8b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "", "reasoning": "SECRET, unfinished"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 1200},
    }
    provider = _ollama_provider(lambda request: httpx.Response(200, json=payload))
    try:
        response = await provider.generate([LLMMessage.user("Explain revenue.")])
    finally:
        await provider.aclose()

    assert response.text == ""
    assert "SECRET" not in response.text
    assert response.finish_reason == "length"


@pytest.mark.anyio
async def test_an_unreachable_endpoint_reports_the_host_and_never_the_url():
    """A base URL can carry a credential, so failures name the host only."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _ollama_provider(handler)
    try:
        with pytest.raises(LLMProviderError) as raised:
            await provider.generate([LLMMessage.user("Anything.")])
    finally:
        await provider.aclose()

    message = str(raised.value)
    assert "localhost:11434" in message
    assert "/v1" not in message
    assert "EMPTY" not in message


@pytest.mark.anyio
async def test_a_missing_model_is_reported_as_a_configuration_problem():
    """Ollama answers 404 for a model that was never pulled."""
    import httpx

    provider = _ollama_provider(
        lambda request: httpx.Response(
            404, json={"error": {"message": 'model "qwen3:8b" not found, try pulling it first'}}
        )
    )
    try:
        with pytest.raises(LLMProviderError) as raised:
            await provider.generate([LLMMessage.user("Anything.")])
    finally:
        await provider.aclose()

    message = str(raised.value)
    assert "LLM_BASE_URL" in message
    assert "qwen3:8b" in message
    assert "EMPTY" not in message

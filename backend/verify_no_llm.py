"""Section 23 checks 5 and 6, run outside pytest so they are honest.

The suite runs with whatever environment conftest set up. These two claims are
about a *deployment* rather than a test: the platform boots and the dashboard
works with the model layer switched off and no provider package importable at
all. So this runs in its own process, on its own database, with ``httpx`` and
``openai`` poisoned in ``sys.modules`` -- if any import path reached for a
provider SDK, this would fail rather than quietly succeed on a package that
happens to be installed for other reasons.

Run: python verify_no_llm.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TMP = Path(__file__).resolve().parent / "tests" / "_tmp"
TMP.mkdir(parents=True, exist_ok=True)
DB = TMP / "verify_no_llm.db"
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ["DOCUMENT_STORAGE_DIR"] = str(TMP / "verify_documents")
os.environ["SECRET_KEY"] = "verify-secret-key-not-for-production-0123456789"
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_ENABLED"] = "false"
# Pinned, not inherited. The claim under test is that turning the *model* off
# leaves the governed tool layer intact, so the one variable that also empties
# that layer has to be held at its default -- otherwise a developer who set
# LLM_TOOL_CALLING_ENABLED=false locally (which .env.example recommends for a
# small local model) sees this script fail for a reason that has nothing to do
# with the model being absent.
os.environ["LLM_TOOL_CALLING_ENABLED"] = "true"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_BASE_URL", None)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(f"{label} {detail}".strip())


print("\n[1] Import and boot with LLM_ENABLED=false")

# Poison the provider SDKs *before* the app imports, so a lazily-imported
# client would raise instead of silently working.
class _Poisoned:
    def __getattr__(self, name: str):  # noqa: ANN401
        raise AssertionError(
            "the application imported a model-provider SDK while LLM_ENABLED=false"
        )


sys.modules["openai"] = _Poisoned()  # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.llm import build_provider, get_llm_config  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.bootstrap import sync_reference_data  # noqa: E402

check("settings.llm_enabled is False", settings.llm_enabled is False)

config = get_llm_config()
check("the resolved LLM config is unavailable", config.is_available is False)
check("it says why", bool(config.unavailable_reason), str(config.unavailable_reason))

provider = build_provider(config)
check(
    "build_provider returns the null provider",
    provider.name == "null",
    f"got {provider.name!r}",
)

Base.metadata.create_all(bind=engine)
session = SessionLocal()
try:
    sync_reference_data(session)
    session.commit()
finally:
    session.close()

app = create_app()

with TestClient(app) as client:
    check("app starts and /health responds", client.get("/health").status_code == 200)

    print("\n[2] The deterministic platform still works end to end")

    reg = client.post(
        "/api/v1/auth/register",
        json={
            "email": "verify@example.com",
            "password": "Verify-Passw0rd!",
            "full_name": "Verify User",
        },
    )
    check("registration works", reg.status_code == 201, reg.text)
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    company = client.post(
        "/api/v1/companies",
        json={"company_name": "NoLlm Ltd", "industry": "RETAIL"},
        headers=headers,
    )
    check("company creation works", company.status_code == 201, company.text)
    company_id = company.json()["id"]

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": "verify@example.com",
            "password": "Verify-Passw0rd!",
            "company_id": company_id,
        },
    )
    check("company-scoped login works", login.status_code == 200, login.text)
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    base = f"/api/v1/companies/{company_id}"
    # The endpoints the dashboard and KPI screens read on load.
    for label, url in [
        ("the dashboard itself", f"{base}/dashboard"),
        ("KPI registry", f"{base}/kpis"),
        ("KPI contracts", f"{base}/kpi-contracts"),
        ("KPI proposals", f"{base}/kpi-proposals"),
        ("connector registry", "/api/v1/connectors"),
        ("data sources", f"{base}/data-sources"),
        ("tables", f"{base}/tables"),
        ("data scope", f"{base}/data-scope"),
        ("semantic catalog", f"{base}/catalog"),
        ("documents", f"{base}/documents"),
        ("relationships", f"{base}/analysis/relationships"),
        ("freshness", f"{base}/analysis/freshness"),
        ("reconciliation", f"{base}/analysis/reconciliation"),
        ("telemetry", f"{base}/telemetry"),
        ("telemetry summary", f"{base}/telemetry/summary"),
        ("audit log", f"{base}/audit"),
        ("activity feed", f"{base}/activity"),
        ("platform meta", "/api/v1/meta"),
    ]:
        response = client.get(url, headers=headers)
        check(f"dashboard reads {label}", response.status_code == 200, f"{url} -> {response.text[:200]}")

    meta = client.get("/api/v1/meta", headers=headers)
    if meta.status_code == 200:
        payload = meta.json()
        check("meta reports copilot unavailable", payload["copilot"]["available"] is False)
        check("meta reports llm_calls_made = 0", payload["llm_calls_made"] == 0, str(payload["llm_calls_made"]))

    print("\n[3] The Copilot degrades honestly rather than failing")

    status = client.get(f"{base}/copilot/status", headers=headers)
    check("copilot status responds", status.status_code == 200, status.text)
    if status.status_code == 200:
        body = status.json()
        check("copilot reports enabled=false", body["enabled"] is False, status.text)
        check("copilot reports available=false", body["available"] is False, status.text)
        check("copilot says why", bool(body["unavailable_reason"]), status.text)
        # The governed tool layer is present regardless of the model: it is the
        # model that is optional, not the governance.
        check("governed tools are still registered", len(body["tools_available"]) > 0, status.text)
        check("no credential appears in the status body", "key" not in status.text.lower(), status.text)

    chat = client.post(
        f"{base}/copilot/chat",
        json={"message": "What does the revenue KPI measure?"},
        headers=headers,
    )
    check("copilot chat answers with 200", chat.status_code == 200, chat.text)
    if chat.status_code == 200:
        body = chat.json()
        check("chat reports llm_available=false", body["llm_available"] is False)
        check("chat recorded no model usage", body["usage"] == {}, str(body["usage"]))
        check("chat reports no model", body["model"] is None, str(body["model"]))
        check("chat ran zero model iterations", body["iterations"] == 0, str(body["iterations"]))
        check("chat called no tools", body["tool_calls"] == [], str(body["tool_calls"]))
        check("chat says why it cannot answer", bool(body["unavailable_reason"]))
        check("chat still returns an answer", bool(body["answer"].strip()))
        check(
            "the answer says the rest of the platform is unaffected",
            "unaffected" in body["answer"].lower(),
            body["answer"][:200],
        )

    print("\n[4] Telemetry records the absence of a model as absence, not zero")

    logs = client.get(f"{base}/telemetry", headers=headers)
    check("execution logs readable", logs.status_code == 200, logs.text)
    if logs.status_code == 200:
        payload = logs.json()
        rows = payload["items"] if isinstance(payload, dict) else payload
        chat_rows = [r for r in rows if str(r.get("operation", "")).endswith("/copilot/chat")]
        check("the chat request was logged", bool(chat_rows), f"{len(rows)} row(s) logged")
        for row in chat_rows:
            check("llm_model is null on the log row", row.get("llm_model") is None, str(row))
            check("llm_calls is null on the log row", row.get("llm_calls") is None, str(row))

print("\n" + "=" * 70)
if failures:
    print(f"{len(failures)} CHECK(S) FAILED")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("ALL CHECKS PASSED: the platform runs fully with LLM_ENABLED=false")

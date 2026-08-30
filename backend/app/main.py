"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.errors import PlatformError
from app.core.telemetry import TelemetryMiddleware
from app.llm.config import get_llm_config
from app.seed.bootstrap import sync_reference_data
from app.services.credential_migration import migrate_legacy_source_credentials

logger = logging.getLogger("bi.ai")

DESCRIPTION = """
Governed KPI intelligence platform: every number on the surface is computed by a
deterministic service from an approved KPI contract, and the AI layer explains
those numbers without ever producing one.

**Foundation and KPI governance** — multi-tenant isolation with row/column/domain
entitlements, data source registry (Supabase / PostgreSQL), discovery, explicit
analytical scope, access-aware profiling with SQL pushdown, quality, grain,
relationship and join-safety detection, freshness, cross-source reconciliation, a
versioned semantic catalog, a versioned document store, and governed KPI contracts
with nine validation checks and human approval.

**Detection** — an expected value per KPI from its company's approved comparison
buckets, robust dispersion (median/MAD with a guarded mean-absolute-deviation
fallback), and a classification of NORMAL, ABNORMAL or LOW_CONFIDENCE. The
threshold is scale-aware and KPI-specific: significance is a modified z-score
against the KPI's own history, and materiality is a share of the KPI's own
expected level, so a KPI measured in units and a KPI measured in millions are
each judged against themselves. Results are persisted per agent run and read back
unchanged.

**Contribution analysis** — on request, apportions a KPI's *measured* movement
across one dimension approved for that KPI contract, ranks the contributors and
drills into the next approved dimension on selection. Shares are measured against
the whole movement and re-checked against the caller's row scope. A large share is
reported as a share, never as a cause, and never as an anomaly: KPI detection runs
continuously, entity-level analysis only when someone asks for it.

**The Copilot** is an optional retrieval-and-explanation layer, disabled by default
(`LLM_ENABLED=false`) and provider-independent. It reads governed knowledge through
narrow permission-checked tools inside one company, and it is anchored to results
the platform already computed and stored. It cannot run SQL, reach tenant business
rows, see connector credentials, or change anything under KPI governance. Every
number stays deterministic.

**Deliberately absent:** forecasting, causal attribution, recommendations,
alerting and notification, and feedback learning. No engine exists behind any of
those, and the Copilot is instructed to say so rather than approximate one.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tables are created here for local development. In deployment, Alembic owns
    # the schema and this is a no-op against an already-migrated database.
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        result = sync_reference_data(session)
        migrated_credentials = migrate_legacy_source_credentials(session)
        session.commit()
        logger.info(
            "Reference data ready: %s roles, %s permissions; %s legacy source credential(s) migrated.",
            result["roles_total"],
            result["permissions_total"],
            migrated_credentials,
        )
    except Exception:
        session.rollback()
        logger.exception("Reference data sync failed.")
        raise
    finally:
        session.close()
    yield


def create_app() -> FastAPI:
    # Interactive docs enumerate every route and body shape, which is exactly what
    # you want locally and exactly what you would not hand an attacker for free.
    # Development keeps them; anything else has to ask for them explicitly.
    #
    # ``openapi_url`` goes with them. /docs and /redoc are only renderers of it, so
    # withholding the two pages while still serving the schema they read would be
    # theatre -- one GET of /openapi.json returns the same enumeration.
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )

    app.add_middleware(TelemetryMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )

    # -- Error handling -------------------------------------------------
    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                **exc.to_payload(),
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "request_invalid",
                "message": "The request body or parameters are invalid.",
                "details": [
                    {
                        "field": ".".join(str(part) for part in error["loc"][1:]) or "body",
                        "problem": error["msg"],
                    }
                    for error in exc.errors()
                ],
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # The detail is logged, not returned: an internal message can disclose
        # schema or credential shape.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "An unexpected error occurred.",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    # -- Meta routes ----------------------------------------------------
    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "environment": settings.environment, "version": "0.1.0"}

    @app.get("/api/v1/meta", tags=["meta"])
    def meta() -> dict:
        """What this sprint does and does not do, machine-readable."""
        llm = get_llm_config()
        return {
            "app": settings.app_name,
            "version": "0.1.0",
            "sprint": 2,
            "sprint_name": "Detection, Governed Copilot + Contribution Analysis",
            "capabilities": [
                "multi_tenant_companies",
                "roles_permissions_row_column_scope",
                "data_source_registry",
                "table_discovery",
                "explicit_analytical_scope",
                "access_aware_profiling",
                "data_quality",
                "grain_detection",
                "relationship_detection",
                "join_safety_analysis",
                "calendar_governance",
                "freshness_tracking",
                "cross_source_reconciliation",
                "sensitivity_classification",
                "document_store_versioned",
                "semantic_catalog_versioned",
                "kpi_governance_contracts",
                "kpi_discovery_proposals",
                "kpi_validation_nine_checks",
                "kpi_approval_versioning",
                "kpi_lineage",
                "kpi_access_policies",
                "audit_trail",
                "runtime_telemetry",
                "kpi_anomaly_detection_three_state",
                "robust_statistics_median_mad",
                # The materiality floor is a ratio of the KPI's own expected level,
                # so two KPIs of different magnitude are each judged against their
                # own history rather than against each other.
                "scale_aware_kpi_specific_thresholds",
                "governed_bucket_configuration",
                "expected_value_and_baseline",
                "additive_contribution_analysis",
                "dimension_investigation_top_k",
                # Declaring a KPI dimension authorises a breakdown on request. It
                # does not schedule per-entity monitoring; see architectural_rule.
                "selective_entity_analysis",
                # Present as code on every deployment; usable only when a model is
                # configured. `copilot.available` below is the operative flag.
                "governed_copilot_retrieval",
                "governed_copilot_tool_layer",
            ],
            "not_in_this_sprint": [
                "forecasting",
                "causal_inference",
                # Retrieval is lexical over governed metadata. No embedding model,
                # no vector store, and no tenant business rows are indexed.
                "vector_embeddings",
                "narratives_over_computed_results",
                "action_recommendations",
                "automated_alerts",
                "feedback_learning",
                "autonomous_investigation",
                # Detection runs continuously at the KPI level only.
                "continuous_entity_monitoring",
            ],
            "copilot": {
                "enabled": llm.enabled,
                "available": llm.is_available,
                "provider": llm.provider,
                "model": llm.model if llm.enabled else None,
                "unavailable_reason": llm.unavailable_reason,
                "scope": (
                    "Company-scoped retrieval and explanation of governed knowledge "
                    "through permission-checked read-only tools. No SQL execution, no "
                    "tenant business rows, no governance mutations."
                ),
            },
            # Platform-wide model calls are zero when no model is configured, which
            # is a fact about the deployment. When one is configured, the honest
            # count is per company and lives in execution telemetry -- so this
            # reports null rather than a number it cannot know here.
            "llm_calls_made": 0 if not llm.is_available else None,
            "llm_calls_source": (
                "GET /api/v1/companies/{company_id}/telemetry/summary reports recorded "
                "model calls, tokens and cost for a company."
            ),
            "architectural_rule": (
                "Detect at the KPI level. Investigate dimensions. Analyse entities "
                "selectively. Declaring a KPI dimension authorises a breakdown; it "
                "does not schedule per-entity monitoring."
            ),
        }

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()

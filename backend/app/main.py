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
from app.seed.bootstrap import sync_reference_data
from app.services.credential_migration import migrate_legacy_source_credentials

logger = logging.getLogger("bi.ai")

DESCRIPTION = """
Governed KPI intelligence platform — **Sprint 1: foundation and KPI governance**.

Sprint 1 answers one question per company:

> *What does this KPI mean, exactly where does its data come from, who may see
> it, and can we reliably calculate it?*

**Delivered:** multi-tenant foundation with row/column/domain entitlements,
data source registry (Supabase / PostgreSQL), discovery, explicit analytical
scope, access-aware profiling with SQL pushdown, quality, grain, relationship
and join-safety detection, freshness, cross-source reconciliation, a versioned
semantic catalog, a versioned document store, and governed KPI contracts with
nine validation checks and human approval.

**Deliberately absent:** anomaly detection, forecasting, contribution analysis,
investigation, document embeddings, LLM reasoning, narratives, recommendations
and alerts. Sprint 1 makes zero model calls.
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
    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
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
        return {
            "app": settings.app_name,
            "version": "0.1.0",
            "sprint": 1,
            "sprint_name": "Foundation + KPI Governance",
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
            ],
            "not_in_this_sprint": [
                "anomaly_detection",
                "forecasting",
                "expected_value_monitoring",
                "contribution_analysis",
                "root_cause_investigation",
                "causal_inference",
                "document_embeddings_and_rag",
                "llm_reasoning_and_narratives",
                "action_recommendations",
                "automated_alerts",
            ],
            "llm_calls_made": 0,
            "architectural_rule": (
                "Detect at the KPI level. Investigate dimensions. Analyse entities "
                "selectively. Declaring a KPI dimension authorises a breakdown; it "
                "does not schedule per-entity monitoring."
            ),
        }

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()

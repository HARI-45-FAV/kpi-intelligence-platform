"""API router assembly."""

from fastapi import APIRouter

from app.api.v1 import (
    analysis,
    auth,
    catalog,
    companies,
    copilot,
    detection,
    documents,
    investigation,
    kpis,
    monitoring,
    observability,
    recommendations,
    sources,
    uploads,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(companies.router)
api_router.include_router(sources.router)
api_router.include_router(uploads.router)
api_router.include_router(analysis.router)
api_router.include_router(documents.router)
api_router.include_router(catalog.router)
api_router.include_router(kpis.router)
api_router.include_router(detection.router)
api_router.include_router(investigation.router)
api_router.include_router(recommendations.router)
api_router.include_router(monitoring.router)
api_router.include_router(observability.router)
api_router.include_router(copilot.router)

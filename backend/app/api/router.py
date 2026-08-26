"""API router assembly."""

from fastapi import APIRouter

from app.api.v1 import (
    analysis,
    auth,
    catalog,
    companies,
    documents,
    kpis,
    observability,
    sources,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(companies.router)
api_router.include_router(sources.router)
api_router.include_router(analysis.router)
api_router.include_router(documents.router)
api_router.include_router(catalog.router)
api_router.include_router(kpis.router)
api_router.include_router(observability.router)

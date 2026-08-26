"""Report what the running app is actually configured to use. No secrets printed."""

from urllib.parse import urlparse

from sqlalchemy import func, inspect, select

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.models.kpi import KpiDefinition, KpiVersion
from app.models.source import DataSource, SourceTable
from app.models.tenant import Company, Permission, Role, User

url = urlparse(settings.database_url)
print(f"PLATFORM DB dialect : {engine.dialect.name}")
print(f"PLATFORM DB host    : {url.hostname or '(local file)'}")
print(f"PLATFORM DB name    : {(url.path or '').lstrip('/') or '(none)'}")
print(f"tables present      : {len(inspect(engine).get_table_names())}")

session = SessionLocal()
try:
    for label, model in [
        ("users", User),
        ("companies", Company),
        ("roles", Role),
        ("permissions", Permission),
        ("data_sources", DataSource),
        ("source_tables", SourceTable),
        ("kpi_definitions", KpiDefinition),
        ("kpi_versions", KpiVersion),
    ]:
        print(f"  {label:<18} {session.scalar(select(func.count()).select_from(model))}")
    for source in session.scalars(select(DataSource)):
        print(f"  source -> {source.name} [{source.source_type}] {source.connection_status}")
finally:
    session.close()

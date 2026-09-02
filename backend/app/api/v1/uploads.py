"""Turning an uploaded spreadsheet into a governed data source.

Two endpoints, and the split between them is the whole design.

``POST .../uploads/preview`` parses the file and reports what it found — the tables
it would create, the type it inferred for every column, and every assumption it had
to make to get there. It writes nothing. A reader can see that their "Order Date"
column was read day-before-month, or that 41 rows were skipped for having the wrong
number of fields, *before* any of it sits underneath a KPI.

``POST .../uploads`` does it for real: loads the rows into a SQLite database the
platform owns, registers a source of type ``UPLOAD``, and then hands off to the
existing pipeline — ``discover_source`` reflects the tables, ``profile_table``
profiles the ones the caller selected. Nothing about detection, grain analysis, KPI
evaluation or investigation is re-implemented here; from the moment the rows land,
an upload is a SQL source like any other.

Both require ``source.manage``, the same permission as registering a database, since
that is what this is.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy import select

from app.connectors.registry import build_connector
from app.core.clock import utcnow
from app.core.config import settings
from app.core.deps import AccessContext, SessionDep, load_scoped, require_permissions
from app.core.errors import Conflict, ValidationFailure
from app.models.base import ConnectionStatus, DataSourceType, RefreshFrequency
from app.models.source import DataSource, SelectedTable, SourceTable
from app.services import audit
from app.services.discovery import discover_source
from app.services.profiling import profile_table
from app.services.tabular import WorkbookPlan, load_into_sqlite, plan_workbook

router = APIRouter(tags=["uploads"])

# What a browser is told it may send. Checked again server-side against the bytes
# actually received, because a content-type header is a claim, not a fact.
ACCEPTED_SUFFIXES = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm")


async def _read_upload(file: UploadFile) -> bytes:
    """The file's bytes, bounded.

    Read in chunks and abandoned the moment the limit is passed, so an oversized
    upload costs the server one chunk of memory rather than all of it. The limit is
    reported with the actual size so the message is actionable.
    """

    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValidationFailure(
                f"That file is larger than the {limit // (1024 * 1024)} MB upload limit. "
                "Split it, or filter it to the rows and columns the KPI needs."
            )
        chunks.append(chunk)
    if not total:
        raise ValidationFailure("The uploaded file is empty.")
    return b"".join(chunks)


def _validated_filename(filename: str | None) -> str:
    """The filename, checked for shape and stripped of any path.

    A browser can send ``../../etc/passwd`` as a filename. Only the base name is ever
    used, and even that never reaches the filesystem — the stored database is named
    after the source's own id. The name is kept purely so the audit trail can say
    which file a load came from.
    """

    name = Path(filename or "").name.strip()
    if not name:
        raise ValidationFailure("The upload has no filename, so its format cannot be determined.")
    suffix = Path(name).suffix.lower()
    if suffix == ".xls":
        # Named on its own because it is the common mistake and the generic message
        # would leave a reader with a spreadsheet and no idea what to do with it.
        raise ValidationFailure(
            f"'{name}' is in the older .xls format, which cannot be read. Open it and "
            "save as .xlsx or .csv, then upload that."
        )
    if suffix not in ACCEPTED_SUFFIXES:
        raise ValidationFailure(
            f"'{name}' is not a supported format. Upload a .csv, .tsv or .xlsx file."
        )
    return name


def _plan_payload(plan: WorkbookPlan, *, filename: str, data: bytes) -> dict:
    """The plan as JSON: everything inferred, and everything assumed."""

    return {
        "filename": filename,
        "file_format": plan.file_format,
        "size_bytes": len(data),
        "checksum_sha256": hashlib.sha256(data).hexdigest(),
        "total_rows": plan.total_rows,
        "notes": plan.notes,
        "tables": [
            {
                "table_name": sheet.table_name,
                "source_name": sheet.source_name,
                "row_count": sheet.row_count,
                # Reported beside the row count, never folded into it: "1,200 rows
                # loaded" and "1,241 rows in the file" are different numbers, and a
                # reader reconciling the upload against their export needs both.
                "skipped_rows": sheet.skipped_rows,
                "notes": sheet.notes,
                "sample_rows": sheet.sample_rows,
                "columns": [
                    {
                        key: value
                        for key, value in asdict(column).items()
                        # `rows` is not on a column; this drops nothing today and
                        # keeps the payload to what the screen actually reads.
                        if key != "rows"
                    }
                    | {"blank_pct": column.blank_pct}
                    for column in sheet.columns
                ],
            }
            for sheet in plan.sheets
        ],
    }


@router.post("/companies/{company_id}/uploads/preview")
async def preview_upload(
    file: UploadFile = File(...),
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> dict:
    """What this file would become, without creating anything.

    The point of a separate step is consent: type inference is a series of decisions
    about someone else's data — that a column of digits is a measure rather than a
    code, that ``31/01`` is the 31st of January — and a reader who has not seen those
    decisions cannot be said to have agreed to them. Nothing is written, so a
    surprising preview costs a re-export rather than a source to delete.
    """

    filename = _validated_filename(file.filename)
    data = await _read_upload(file)
    plan = plan_workbook(filename, data, max_rows=settings.upload_max_rows)
    payload = _plan_payload(plan, filename=filename, data=data)
    payload["row_limit"] = settings.upload_max_rows
    return {"preview": payload}


def _storage_path(company_id: str, source_id: str) -> Path:
    """Where an upload's loaded data lives.

    Named after ids the platform issued, never after anything the caller sent, and
    always under the configured directory — so no upload can name a file outside it.
    One directory per company keeps a tenant's data physically separate, which is
    worth having even though every read is already scoped by company.
    """

    return Path(settings.upload_storage_dir) / company_id / f"{source_id}.db"


@router.post(
    "/companies/{company_id}/uploads",
    status_code=status.HTTP_201_CREATED,
)
async def create_upload(
    session: SessionDep,
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(..., description="What to call this source"),
    description: str | None = Form(default=None),
    profile: bool = Form(default=True, description="Profile the loaded tables immediately"),
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> dict:
    """Load a spreadsheet and register it as a source.

    The order matters. The source row is created first so the stored database can be
    named after its id; the rows are loaded next; discovery runs last, against the
    file that now exists. If parsing fails, it fails before anything is written.
    """

    filename = _validated_filename(file.filename)
    data = await _read_upload(file)
    label = (name or "").strip()
    if not label:
        raise ValidationFailure("Give this source a name so it can be found later.")

    existing = session.scalar(
        select(DataSource).where(
            DataSource.company_id == access.company.id, DataSource.name == label
        )
    )
    if existing is not None:
        raise Conflict(
            f"A data source named '{label}' already exists. Re-upload into it to replace "
            "its rows, or choose a different name."
        )

    # Parsed before the source exists, so a file the platform cannot read never
    # leaves a half-registered source behind.
    plan = plan_workbook(filename, data, max_rows=settings.upload_max_rows)
    checksum = hashlib.sha256(data).hexdigest()

    source = DataSource(
        company_id=access.company.id,
        name=label,
        source_type=DataSourceType.UPLOAD,
        description=description,
        schema_name="main",
        connection_reference=filename,
        options={},
        connection_status=ConnectionStatus.UNTESTED,
        # A file does not refresh on its own. Saying MANUAL is the honest answer and
        # keeps the freshness screens from expecting an arrival that will never come.
        refresh_frequency=RefreshFrequency.MANUAL,
        timezone=access.company.timezone,
        known_limitations=(
            "A point-in-time upload. It does not refresh on its own, so its coverage "
            "ends at the last row of the file that was loaded."
        ),
    )
    session.add(source)
    session.flush()

    result = _load(session, source, plan, filename=filename, data=data, checksum=checksum)
    outcome = _discover_and_profile(session, source, access, profile=profile)

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.FILE_UPLOADED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=(
            f"Loaded {result['total_rows']:,} row(s) from '{filename}' into "
            f"{len(result['tables'])} table(s)."
        ),
        details={
            "filename": filename,
            "file_format": plan.file_format,
            "checksum_sha256": checksum,
            "size_bytes": len(data),
            "tables": [item["table_name"] for item in result["tables"]],
            "rows_loaded": result["total_rows"],
            "rows_skipped": _skipped(plan),
            "assumptions": _assumptions(plan),
        },
        request=request,
    )
    audit.event(
        session,
        company_id=access.company.id,
        category="SOURCE",
        title="Spreadsheet uploaded",
        message=f"{source.name} — {result['total_rows']:,} rows from {filename}",
    )

    return {
        "source_id": source.id,
        "name": source.name,
        "load": result,
        "discovery": outcome,
        "preview": _plan_payload(plan, filename=filename, data=data),
    }


@router.post("/companies/{company_id}/uploads/{source_id}/reload")
async def reload_upload(
    source_id: str,
    session: SessionDep,
    request: Request,
    file: UploadFile = File(...),
    profile: bool = Form(default=True),
    access: AccessContext = Depends(require_permissions("source.manage")),
) -> dict:
    """Replace an upload's rows with a newer export of the same thing.

    Replacing rather than appending, because a re-exported file is a new statement of
    the same facts and appending would double every figure in it. What is *not*
    replaced is the record: every load stays in the audit trail with its own filename,
    checksum and row count, so "which export is this KPI's history built on" remains
    answerable after the rows themselves have been overwritten.

    An identical file is refused rather than reloaded — the same discipline the Agent
    Run applies to a date it has already analysed. Re-loading unchanged rows would
    write a new load record implying new data arrived.
    """

    source: DataSource = load_scoped(session, DataSource, source_id, access)
    if source.source_type != DataSourceType.UPLOAD:
        raise ValidationFailure(
            f"'{source.name}' is a {source.source_type} source, not an upload. Edit its "
            "connection instead."
        )

    filename = _validated_filename(file.filename)
    data = await _read_upload(file)
    checksum = hashlib.sha256(data).hexdigest()
    if (source.options or {}).get("checksum_sha256") == checksum:
        raise Conflict(
            f"That is byte-for-byte the file already loaded into '{source.name}' "
            f"({(source.options or {}).get('loaded_rows', 0):,} rows). Nothing was "
            "reloaded, so the existing data and its history are untouched."
        )

    plan = plan_workbook(filename, data, max_rows=settings.upload_max_rows)
    previous_tables = [
        table.table_name
        for table in session.scalars(
            select(SourceTable).where(SourceTable.data_source_id == source.id)
        )
    ]
    new_tables = [sheet.table_name for sheet in plan.sheets]
    lost = sorted(set(previous_tables) - set(new_tables))

    result = _load(session, source, plan, filename=filename, data=data, checksum=checksum)
    outcome = _discover_and_profile(session, source, access, profile=profile)

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.FILE_UPLOADED,
        resource_type="data_source",
        resource_id=source.id,
        resource_label=source.name,
        summary=(
            f"Reloaded '{source.name}' from '{filename}': {result['total_rows']:,} row(s) "
            f"replaced the previous load."
        ),
        details={
            "filename": filename,
            "checksum_sha256": checksum,
            "size_bytes": len(data),
            "rows_loaded": result["total_rows"],
            "rows_skipped": _skipped(plan),
            "tables": new_tables,
            # Named rather than merely counted: a table that vanished between exports
            # is what silently breaks a KPI built on it, and the trail should say so.
            "tables_no_longer_present": lost,
            "assumptions": _assumptions(plan),
        },
        request=request,
    )

    return {
        "source_id": source.id,
        "load": result,
        "discovery": outcome,
        "tables_no_longer_present": lost,
        "preview": _plan_payload(plan, filename=filename, data=data),
    }


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------
def _assumptions(plan: WorkbookPlan) -> list[str]:
    """Every note the parser attached, flattened for the trail.

    Written into the audit entry rather than only shown on screen: an assumption
    about how a date was read is part of how a KPI's numbers came to be, and it has
    to survive the browser tab that displayed it.
    """

    lines = list(plan.notes)
    for sheet in plan.sheets:
        lines.extend(f"{sheet.table_name}: {note}" for note in sheet.notes)
        for column in sheet.columns:
            lines.extend(f"{sheet.table_name}.{column.name}: {note}" for note in column.notes)
    return lines


def _skipped(plan: WorkbookPlan) -> int:
    """Rows the file contained that the load did not.

    Recorded as its own number because it is the one figure that makes a row count
    checkable: a total that silently excludes malformed rows looks like a complete
    load of a smaller file.
    """

    return sum(sheet.skipped_rows for sheet in plan.sheets)


def _load(
    session,
    source: DataSource,
    plan: WorkbookPlan,
    *,
    filename: str,
    data: bytes,
    checksum: str,
) -> dict:
    """Write the rows and record on the source what was loaded."""

    path = _storage_path(source.company_id, source.id)
    report = load_into_sqlite(plan, path)

    options = dict(source.options or {})
    options.update(
        {
            # Never accepted from a caller; see `registry._build_upload`.
            "storage_path": str(path),
            "original_filename": filename,
            "file_format": plan.file_format,
            "checksum_sha256": checksum,
            "size_bytes": len(data),
            "loaded_rows": report.total_rows,
            "skipped_rows": _skipped(plan),
            "loaded_at": utcnow().isoformat(),
            "loaded_tables": [item["table_name"] for item in report.tables],
            "assumptions": _assumptions(plan),
        }
    )
    source.options = options
    source.database_name = path.name
    source.connection_reference = filename
    source.connection_status = ConnectionStatus.CONNECTED
    source.last_tested_at = utcnow()
    source.last_test_error = None
    session.flush()

    return {"path_recorded": True, "tables": report.tables, "total_rows": report.total_rows}


def _discover_and_profile(
    session, source: DataSource, access: AccessContext, *, profile: bool
) -> dict:
    """Hand the loaded file to the pipeline that already exists.

    This is the payoff for loading into SQL rather than holding the file in memory:
    discovery, selection and profiling are the platform's own, unchanged, so an
    uploaded sheet arrives in the catalog with the same grain, quality and freshness
    metadata as a table in a warehouse. Selecting every discovered table is right
    here specifically because the user just uploaded it — they have already said
    which data they mean.
    """

    connector = build_connector(source)
    try:
        discovery = discover_source(session, source, connector, schema="main")
        session.flush()

        tables = list(
            session.scalars(select(SourceTable).where(SourceTable.data_source_id == source.id))
        )
        for table in tables:
            selected = session.scalar(
                select(SelectedTable).where(SelectedTable.source_table_id == table.id)
            )
            if selected is None:
                session.add(
                    SelectedTable(
                        company_id=source.company_id,
                        data_source_id=source.id,
                        source_table_id=table.id,
                        enabled=True,
                        selected_by=access.user.id,
                    )
                )
            else:
                selected.enabled = True
        session.flush()

        profiled: list[str] = []
        failures: list[dict] = []
        if profile:
            for table in tables:
                try:
                    profile_table(session, table, connector, access)
                    profiled.append(table.table_name)
                except Exception as exc:
                    # One unprofilable table must not lose the whole upload. The
                    # failure is reported rather than swallowed, and the table is
                    # still registered — an unprofiled table is visibly unprofiled.
                    failures.append({"table": table.table_name, "error": str(exc)})
            session.flush()

        return {
            "tables_found": discovery.tables_found,
            "tables_created": discovery.tables_created,
            "columns_created": discovery.columns_created,
            "tables_selected": len(tables),
            "tables_profiled": profiled,
            "profiling_failures": failures,
        }
    finally:
        connector.close()

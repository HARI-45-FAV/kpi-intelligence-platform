"""Structured detection logging: one traceable line per KPI per run.

The requirement this satisfies is narrow and concrete: after one "Run Agent"
execution, the backend logs must show, for each KPI, *which source was read,
which formula was used, which comparison bucket was selected, which reference
dates and values it produced, and how actual, expected, deviation and status
followed from them.* Without that, a wrong number is unarguable -- nobody can say
whether the formula, the calendar policy or the data was at fault.

Two rules govern what may appear here, and both are enforced by construction
rather than by reviewer discipline:

**No credentials.** Nothing in this module reads a connection string, a key, a
password or a header. The source is identified by the fields the KPI registration
already exposes -- schema, table, time field, data source id -- and the query is
described by the connector's own credential-free descriptor (a SQL statement with
bound parameters, or a REST path and query string). Neither ever contains a
secret: the SQL path binds its values, and the REST path is built from the
project URL's path only, with the key living in a header the descriptor does not
include.

**No business content beyond the KPI's own numbers.** A detection result *is* the
KPI's value on a set of dates, so those are the payload -- and they are what the
requirement asks to see. What does not appear is anything wider: no rows, no
customer, product or region identifiers, no dimension values, no free text from
the company's documents. The reference values are logged because "expected =
median of these" is unverifiable without them, and they are the same aggregates
already stored on the run and shown to entitled callers.

The log line is emitted as JSON on a named logger, so a deployment can route
``bi.ai.detection`` to its own sink, or silence it, without touching this code and
without affecting anything else the application logs.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from app.services.detection import DetectionOutcome

#: Its own logger, so this stream can be routed or silenced independently. It is
#: a child of the application logger, so existing configuration still applies.
logger = logging.getLogger("bi.ai.detection")

#: How many reference points to spell out individually. A configuration may grant
#: 26 comparable dates plus a prior year, and a log line is read by a person: the
#: count and the median stay exact, and the enumeration is capped.
MAX_LOGGED_REFERENCES = 30


def _round(value: float | None, places: int = 6) -> float | None:
    """Keep the numbers readable without changing what they say."""

    return None if value is None else round(float(value), places)


def detection_log_record(
    outcome: DetectionOutcome, *, run_id: str | None = None
) -> dict[str, Any]:
    """The full traceable record for one KPI on one date, as a plain dict.

    Returned rather than only logged so a test can assert on its shape -- and so
    a caller that wants this in a different sink does not have to parse a log
    line to get it.
    """

    source = outcome.source or {}
    references = outcome.references
    listed = references[:MAX_LOGGED_REFERENCES]

    record: dict[str, Any] = {
        "event": "detection.kpi",
        # --- which KPI, at which governed version ---------------------------
        "company_id": outcome.company_id,
        "kpi_key": outcome.kpi_key,
        "kpi_name": outcome.kpi_name,
        "kpi_version": outcome.kpi_version,
        "kpi_version_id": outcome.kpi_version_id,
        "target_date": outcome.target_date.isoformat(),
        "run_id": run_id,
        # --- which source and which formula, from KPI registration ----------
        "source": {
            "data_source_id": source.get("data_source_id"),
            "source_type": source.get("source_type"),
            "schema": source.get("schema"),
            "table": source.get("table"),
            "time_field": source.get("time_field"),
            "execution": source.get("execution"),
            # The period the source holds on that time field. Structural, and the
            # fact that explains an excluded reference date.
            "coverage": source.get("coverage"),
        },
        "formula": source.get("formula"),
        # The exact read used for the actual: bound SQL, or a REST path and
        # query. Structural only -- no credential, no row content.
        "query": source.get("query"),
        # --- which comparison basis was selected, and why -------------------
        "bucket": {
            "applied": str(outcome.bucket_applied),
            "all_applied": [str(bucket) for bucket in outcome.buckets_applied],
            "config_key": outcome.bucket_config_key,
            "config_version": outcome.bucket_config_version,
            "config_id": outcome.bucket_config_id,
            "comparison_label": outcome.comparison_label,
            "decisions": [
                {
                    "bucket": str(decision.bucket),
                    "role": decision.role,
                    "reference_count": decision.reference_count,
                }
                for decision in outcome.bucket_decisions
            ],
        },
        # --- which historical dates it compared against, and what they gave --
        "reference": {
            "count": len(references),
            "dates": [point.day.isoformat() for point in listed],
            "values": [_round(point.value) for point in listed],
            "truncated": len(references) > len(listed),
        },
        # --- and how the verdict followed ------------------------------------
        "actual": _round(outcome.actual),
        "expected": _round(outcome.expected),
        "deviation_absolute": _round(outcome.deviation_absolute),
        "deviation_pct": _round(outcome.deviation_pct, 4),
        "status": str(outcome.status),
        "statistics": {
            "median": _round(outcome.median_value),
            "mad": _round(None if outcome.dispersion is None else outcome.dispersion.mad),
            "dispersion_basis": (
                None if outcome.dispersion is None else str(outcome.dispersion.basis)
            ),
            "modified_z_score": _round(outcome.modified_z, 4),
            "z_threshold": _round(outcome.z_threshold, 4),
            "statistically_significant": outcome.statistically_significant,
            "breached_tolerance": outcome.breached_tolerance,
            "relative_floor_pct": _round(outcome.relative_floor_pct, 4),
            "movement_is_material": outcome.movement_is_material,
            "yoy_applied": outcome.yoy_applied,
            "yoy_factor": _round(outcome.yoy_factor),
        },
        "reason": outcome.reason,
        "query_count": outcome.query_count,
        "duration_ms": outcome.duration_ms,
    }
    return record


def log_detection(outcome: DetectionOutcome, *, run_id: str | None = None) -> dict[str, Any]:
    """Emit one detection line and return the record that was emitted.

    A human-readable summary is the log *message*, so ``tail -f`` is useful on its
    own, and the full record travels as JSON in the same line so a log processor
    can read it without a parser for prose.
    """

    record = detection_log_record(outcome, run_id=run_id)
    logger.info(
        "detection %s v%s on %s: actual=%s expected=%s deviation=%s status=%s "
        "[%s.%s via %s | %s | %s | %s reference date(s)]",
        outcome.kpi_key,
        outcome.kpi_version,
        outcome.target_date.isoformat(),
        _fmt(outcome.actual),
        _fmt(outcome.expected),
        "n/a" if outcome.deviation_pct is None else f"{outcome.deviation_pct:+.2f}%",
        outcome.status,
        record["source"]["schema"],
        record["source"]["table"],
        record["source"]["execution"],
        record["formula"],
        record["bucket"]["applied"],
        record["reference"]["count"],
        extra={"detection": record},
    )
    # A second line, machine-first. Kept separate from the message above so that
    # neither format has to compromise for the other.
    logger.debug("detection.record %s", json.dumps(record, default=str, sort_keys=True))
    return record


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.4f}".rstrip("0").rstrip(".")

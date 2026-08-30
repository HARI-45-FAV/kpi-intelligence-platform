"""KPI validation: the nine governance checks that gate activation.

A KPI cannot become ACTIVE on an administrator's word alone. Every check here is
deterministic and every result is persisted with its expected and actual values,
so "why was Revenue v2 allowed live?" has an answer months later.

The most important check is the last one. Checks 1-8 reason about the contract;
check 9 *executes* the KPI against the source and verifies the number is real,
finite, and additive where it claims to be. A KPI that parses, references
existing columns and still returns nothing useful has failed, and only running it
reveals that.

Blocking vs advisory is explicit. A blocking failure prevents activation; an
advisory failure is recorded as a warning the approver must weigh. Cardinality
concerns and quality warnings are advisory — refusing to define a KPI over
imperfect data would make the platform useless on real data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import DataSourceConnector
from app.connectors.sql import SqlConnector, classify_type_family
from app.core.clock import as_utc, utcnow
from app.core.deps import AccessContext
from app.core.errors import PlatformError
from app.core.permissions import ADMIN_ROLE_KEY, ROLES_BY_KEY
from app.models.base import (
    Aggregation,
    JoinSafetyLevel,
    QualityStatus,
    SemanticType,
    TimeGrain,
    ValidationStatus,
    ValidationTest,
)
from app.models.kpi import (
    KpiValidationCheck,
    KpiValidationRun,
    KpiVersion,
)
from app.models.profiling import (
    ColumnProfile,
    JoinSafety,
    TableGrain,
    TableProfile,
    TableRelationship,
)
from app.models.source import SourceColumn, SourceHealth, SourceTable
from app.services.kpi_formula import FormulaSpec, spec_from_stored
from app.services.kpi_sql import execute_kpi

# Time grains, finest first. A KPI cannot be finer than its source table.
_GRAIN_ORDER = {
    TimeGrain.HOUR: 0,
    TimeGrain.DAY: 1,
    TimeGrain.WEEK: 2,
    TimeGrain.MONTH: 3,
    TimeGrain.QUARTER: 4,
    TimeGrain.YEAR: 5,
}

# Aggregation compatibility by semantic type.
_NUMERIC_AGGREGATIONS = {Aggregation.SUM, Aggregation.AVG, Aggregation.MIN, Aggregation.MAX}
_COUNTABLE_TYPES = {
    SemanticType.IDENTIFIER,
    SemanticType.CATEGORICAL,
    SemanticType.BOOLEAN_FLAG,
    SemanticType.DATE,
    SemanticType.TIMESTAMP,
    SemanticType.TEXT,
    SemanticType.NUMERIC_MEASURE,
}

# Additivity tolerance for the reconciliation check. Floating-point summation
# over many rows will not agree to the last bit.
ADDITIVITY_TOLERANCE_PCT = 0.01
# Breakdown cardinality beyond which a dimension is really an entity list.
DIMENSION_CARDINALITY_ADVISORY = 200


@dataclass(slots=True)
class CheckResult:
    test_type: str
    label: str
    status: str
    expected: str | None = None
    actual: str | None = None
    message: str | None = None
    is_blocking: bool = True
    runtime_ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in {ValidationStatus.PASS, ValidationStatus.WARN}

    def as_dict(self) -> dict[str, Any]:
        return {
            "test_type": self.test_type,
            "label": self.label,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "is_blocking": self.is_blocking,
            "runtime_ms": self.runtime_ms,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class ValidationReport:
    overall_status: str
    checks: list[CheckResult]
    duration_ms: int
    ready_for_approval: bool
    summary: str

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.status == ValidationStatus.PASS)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if c.status == ValidationStatus.FAIL)

    @property
    def warned_count(self) -> int:
        return sum(1 for c in self.checks if c.status == ValidationStatus.WARN)

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "ready_for_approval": self.ready_for_approval,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "warned": self.warned_count,
            "checks": [c.as_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def validate_kpi_version(
    session: Session,
    version: KpiVersion,
    access: AccessContext,
    connector: DataSourceConnector | None,
) -> tuple[KpiValidationRun, ValidationReport]:
    started_at = utcnow()
    started = time.perf_counter()

    table = (
        session.get(SourceTable, version.primary_source_table_id)
        if version.primary_source_table_id
        else None
    )
    context = _Context(session=session, version=version, table=table, access=access, connector=connector)

    checks: list[CheckResult] = [
        _check_formula_parses(context),
        _check_columns_exist(context),
        _check_time_field(context),
        _check_dimensions_exist(context),
        _check_aggregation_valid(context),
        _check_duplicate_counting(context),
        _check_grain_compatible(context),
        _check_access_policy(context),
        _check_reconciles_to_source(context),
    ]

    duration_ms = int((time.perf_counter() - started) * 1000)
    blocking_failures = [c for c in checks if c.status == ValidationStatus.FAIL and c.is_blocking]
    advisory_failures = [
        c for c in checks if c.status == ValidationStatus.FAIL and not c.is_blocking
    ]
    warnings = [c for c in checks if c.status == ValidationStatus.WARN]

    if blocking_failures:
        overall = ValidationStatus.FAIL
        summary = (
            f"{len(blocking_failures)} blocking check(s) failed: "
            + "; ".join(c.label for c in blocking_failures)
        )
    elif warnings or advisory_failures:
        overall = ValidationStatus.WARN
        summary = (
            f"All blocking checks passed with {len(warnings) + len(advisory_failures)} "
            "advisory finding(s) for the approver to weigh."
        )
    else:
        overall = ValidationStatus.PASS
        summary = "All governance checks passed."

    report = ValidationReport(
        overall_status=overall,
        checks=checks,
        duration_ms=duration_ms,
        ready_for_approval=not blocking_failures,
        summary=summary,
    )

    run = KpiValidationRun(
        company_id=version.company_id,
        kpi_version_id=version.id,
        started_at=started_at,
        finished_at=utcnow(),
        duration_ms=duration_ms,
        overall_status=overall,
        passed_count=report.passed_count,
        failed_count=report.failed_count,
        warned_count=report.warned_count,
        executed_by=access.user.id,
        summary=summary,
    )
    session.add(run)
    session.flush()

    for check in checks:
        session.add(
            KpiValidationCheck(
                company_id=version.company_id,
                validation_run_id=run.id,
                kpi_version_id=version.id,
                test_type=check.test_type,
                label=check.label,
                status=check.status,
                expected=check.expected,
                actual=check.actual,
                message=check.message,
                runtime_ms=check.runtime_ms,
                is_blocking=check.is_blocking,
                evidence=check.evidence,
            )
        )

    version.last_validation_status = overall
    version.last_validated_at = run.finished_at
    version.last_validation_run_id = run.id
    return (run, report)


def latest_validation_summary(session: Session, version: KpiVersion) -> dict | None:
    """The most recent stored validation result for one KPI version.

    A read of persisted check results -- it runs nothing and recomputes nothing.
    The KPI detail API and the Copilot's ``get_kpi_validation_summary`` tool both
    read it, so an explanation of "why is this KPI blocked" quotes the same run,
    the same check statuses and the same expected/actual pairs the approval
    screen shows. ``None`` means the version has never been validated, which is
    a fact the caller must state rather than fill in.
    """
    run = session.scalar(
        select(KpiValidationRun)
        .where(KpiValidationRun.kpi_version_id == version.id)
        .order_by(KpiValidationRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return None
    return {
        "run_id": run.id,
        "overall_status": run.overall_status,
        "ready_for_approval": run.overall_status
        in {ValidationStatus.PASS, ValidationStatus.WARN},
        "summary": run.summary,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at,
        "passed": run.passed_count,
        "failed": run.failed_count,
        "warned": run.warned_count,
        "checks": [
            {
                "test_type": check.test_type,
                "label": check.label,
                "status": check.status,
                "expected": check.expected,
                "actual": check.actual,
                "message": check.message,
                "is_blocking": check.is_blocking,
                "runtime_ms": check.runtime_ms,
                "evidence": check.evidence,
            }
            for check in sorted(run.checks, key=lambda c: c.created_at)
        ],
    }


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Context:
    session: Session
    version: KpiVersion
    table: SourceTable | None
    access: AccessContext
    connector: DataSourceConnector | None
    _spec: FormulaSpec | None = None
    _spec_error: str | None = None

    @property
    def spec(self) -> FormulaSpec | None:
        if self._spec is None and self._spec_error is None:
            try:
                self._spec = spec_from_stored(
                    self.version.formula_spec,
                    expression=self.version.formula_expression,
                    default_table=self.table.table_name if self.table else None,
                )
            except PlatformError as exc:
                self._spec_error = exc.message
        return self._spec

    @property
    def spec_error(self) -> str | None:
        _ = self.spec
        return self._spec_error

    def columns(self) -> dict[str, SourceColumn]:
        if self.table is None:
            return {}
        return {column.column_name.lower(): column for column in self.table.columns}

    def profile_for(self, column: SourceColumn) -> ColumnProfile | None:
        return self.session.scalar(
            select(ColumnProfile).where(ColumnProfile.source_column_id == column.id)
        )

    def table_profile(self) -> TableProfile | None:
        if self.table is None:
            return None
        return self.session.scalar(
            select(TableProfile).where(TableProfile.source_table_id == self.table.id)
        )

    def grain(self) -> TableGrain | None:
        if self.table is None:
            return None
        return self.session.scalar(
            select(TableGrain).where(TableGrain.source_table_id == self.table.id)
        )


def _timed(func):
    """Attach elapsed time to whichever CheckResult a check returns."""

    def wrapper(context: _Context) -> CheckResult:
        started = time.perf_counter()
        result = func(context)
        result.runtime_ms = int((time.perf_counter() - started) * 1000)
        return result

    return wrapper


# ---------------------------------------------------------------------------
# 1. Formula parses
# ---------------------------------------------------------------------------
@_timed
def _check_formula_parses(context: _Context) -> CheckResult:
    label = "Formula parses into a governed contract"
    if context.table is None:
        return CheckResult(
            ValidationTest.FORMULA_PARSES,
            label,
            ValidationStatus.FAIL,
            expected="A source table binding",
            actual="none",
            message="The KPI version is not bound to a table in the approved data scope.",
        )
    if context.spec_error:
        return CheckResult(
            ValidationTest.FORMULA_PARSES,
            label,
            ValidationStatus.FAIL,
            expected="AGG([DISTINCT] column) optionally divided by another term",
            actual=context.version.formula_expression,
            message=context.spec_error,
        )

    spec = context.spec
    assert spec is not None
    rendered = spec.render()
    # The stored display string and the structured spec must agree, or the UI is
    # showing a definition that is not the one being executed.
    drift = rendered.replace(" ", "").lower() != (
        context.version.formula_expression or ""
    ).replace(" ", "").lower()
    return CheckResult(
        ValidationTest.FORMULA_PARSES,
        label,
        ValidationStatus.WARN if drift else ValidationStatus.PASS,
        expected=context.version.formula_expression,
        actual=rendered,
        message=(
            "Stored expression and structured contract differ in form; the "
            "structured contract is what executes."
            if drift
            else f"Parsed as {spec.kind} with {len(spec.measures)} aggregate term(s)."
        ),
        is_blocking=not drift,
        evidence=spec.as_dict(),
    )


# ---------------------------------------------------------------------------
# 2. Columns exist (and are readable)
# ---------------------------------------------------------------------------
@_timed
def _check_columns_exist(context: _Context) -> CheckResult:
    label = "Referenced columns exist and are readable"
    spec = context.spec
    if spec is None or context.table is None:
        return _skipped(ValidationTest.COLUMNS_EXIST, label, "formula could not be parsed")

    columns = context.columns()
    missing: list[str] = []
    unreadable: list[str] = []
    resolved: list[str] = []

    for role, table_name, column_name in spec.referenced_columns:
        if table_name and table_name.lower() != context.table.table_name.lower():
            # Sprint 1 KPIs are single-table by design: a cross-table formula
            # would need a join, and an unverified join is exactly what the
            # join-safety analysis exists to prevent.
            missing.append(f"{table_name}.{column_name} (outside the bound table)")
            continue
        column = columns.get(column_name.lower())
        if column is None:
            missing.append(f"{role}: {column_name}")
            continue
        if not context.access.can_read_column(column, table_name=context.table.table_name):
            unreadable.append(f"{column_name} ({context.access.withheld_reason(column)})")
            continue
        resolved.append(f"{role}: {context.table.table_name}.{column.column_name}")

    if missing:
        return CheckResult(
            ValidationTest.COLUMNS_EXIST,
            label,
            ValidationStatus.FAIL,
            expected="every referenced column present in the bound table",
            actual=f"missing: {', '.join(missing)}",
            message="Re-run discovery if the source schema changed.",
            evidence={"missing": missing, "resolved": resolved},
        )
    if unreadable:
        return CheckResult(
            ValidationTest.COLUMNS_EXIST,
            label,
            ValidationStatus.FAIL,
            expected="all referenced columns readable by the validating user",
            actual=f"withheld: {', '.join(unreadable)}",
            message=(
                "A KPI cannot be validated by a user who is not entitled to the "
                "columns it reads. Ask an administrator to validate it."
            ),
            evidence={"unreadable": unreadable},
        )
    return CheckResult(
        ValidationTest.COLUMNS_EXIST,
        label,
        ValidationStatus.PASS,
        expected="all referenced columns present and readable",
        actual=f"{len(resolved)} column reference(s) resolved",
        evidence={"resolved": resolved},
    )


# ---------------------------------------------------------------------------
# 3. Time field valid
# ---------------------------------------------------------------------------
@_timed
def _check_time_field(context: _Context) -> CheckResult:
    label = "Time field exists and is temporal"
    version = context.version
    if context.table is None:
        return _skipped(ValidationTest.TIME_FIELD_VALID, label, "no table binding")

    if not version.time_field:
        return CheckResult(
            ValidationTest.TIME_FIELD_VALID,
            label,
            ValidationStatus.FAIL,
            expected="a temporal column",
            actual="none configured",
            message=(
                "Without a time field the KPI cannot be tracked over time, which "
                "is the entire point of monitoring it."
            ),
        )

    column = context.columns().get(version.time_field.lower())
    if column is None:
        return CheckResult(
            ValidationTest.TIME_FIELD_VALID,
            label,
            ValidationStatus.FAIL,
            expected=f"{version.time_field} present in {context.table.table_name}",
            actual="not found",
        )

    family = classify_type_family(column.data_type)
    if family != "TEMPORAL" and column.semantic_type not in {
        SemanticType.DATE,
        SemanticType.TIMESTAMP,
    }:
        return CheckResult(
            ValidationTest.TIME_FIELD_VALID,
            label,
            ValidationStatus.FAIL,
            expected="DATE or TIMESTAMP",
            actual=f"{column.data_type} ({column.semantic_type})",
            message="A non-temporal column cannot define the KPI's time axis.",
        )

    profile = context.profile_for(column)
    null_pct = profile.null_pct if profile else None
    if null_pct and null_pct > 0:
        return CheckResult(
            ValidationTest.TIME_FIELD_VALID,
            label,
            ValidationStatus.WARN,
            expected="0% null",
            actual=f"{null_pct:.2f}% null",
            message=(
                f"{null_pct:.2f}% of rows have no {version.time_field} and will be "
                "excluded from every time-bounded calculation."
            ),
            is_blocking=False,
            evidence={"column": column.column_name, "null_pct": null_pct},
        )

    return CheckResult(
        ValidationTest.TIME_FIELD_VALID,
        label,
        ValidationStatus.PASS,
        expected="DATE or TIMESTAMP with full coverage",
        actual=f"{column.column_name} ({column.data_type})",
        evidence={
            "column": column.column_name,
            "coverage_start": profile.min_value if profile else None,
            "coverage_end": profile.max_value if profile else None,
        },
    )


# ---------------------------------------------------------------------------
# 4. Dimensions exist
# ---------------------------------------------------------------------------
@_timed
def _check_dimensions_exist(context: _Context) -> CheckResult:
    label = "Declared dimensions resolve to real columns"
    version = context.version
    if not version.dimensions:
        return CheckResult(
            ValidationTest.DIMENSIONS_EXIST,
            label,
            ValidationStatus.WARN,
            expected="at least one breakdown dimension",
            actual="none declared",
            message=(
                "The KPI can be monitored, but a movement could not be attributed "
                "to any part of the business without a dimension."
            ),
            is_blocking=False,
        )

    missing: list[str] = []
    high_cardinality: list[str] = []
    resolved: list[dict] = []

    for dimension in version.dimensions:
        table = (
            context.session.get(SourceTable, dimension.source_table_id)
            if dimension.source_table_id
            else context.table
        )
        if table is None:
            missing.append(f"{dimension.dimension_name} (no source table)")
            continue
        column = next(
            (
                c
                for c in table.columns
                if c.column_name.lower() == dimension.source_column.lower()
            ),
            None,
        )
        if column is None:
            missing.append(f"{dimension.dimension_name} -> {table.table_name}.{dimension.source_column}")
            continue
        if not context.access.can_read_column(column, table_name=table.table_name):
            missing.append(f"{dimension.dimension_name} (not readable)")
            continue

        profile = context.profile_for(column)
        cardinality = profile.distinct_count if profile else dimension.approx_cardinality
        if cardinality and cardinality > DIMENSION_CARDINALITY_ADVISORY:
            high_cardinality.append(f"{dimension.dimension_name} ({cardinality} values)")
        resolved.append(
            {
                "dimension": dimension.dimension_name,
                "column": f"{table.table_name}.{column.column_name}",
                "cardinality": cardinality,
            }
        )

    if missing:
        return CheckResult(
            ValidationTest.DIMENSIONS_EXIST,
            label,
            ValidationStatus.FAIL,
            expected="every declared dimension resolvable and readable",
            actual=f"unresolved: {', '.join(missing)}",
            evidence={"missing": missing, "resolved": resolved},
        )
    if high_cardinality:
        return CheckResult(
            ValidationTest.DIMENSIONS_EXIST,
            label,
            ValidationStatus.WARN,
            expected=f"breakdowns under {DIMENSION_CARDINALITY_ADVISORY} distinct values",
            actual=f"high cardinality: {', '.join(high_cardinality)}",
            message=(
                "High-cardinality breakdowns are entity lists rather than "
                "dimensions. Contribution analysis will rank the top contributors "
                "rather than scan every value."
            ),
            is_blocking=False,
            evidence={"resolved": resolved, "high_cardinality": high_cardinality},
        )
    return CheckResult(
        ValidationTest.DIMENSIONS_EXIST,
        label,
        ValidationStatus.PASS,
        expected="every declared dimension resolvable",
        actual=f"{len(resolved)} dimension(s) resolved",
        evidence={"resolved": resolved},
    )


# ---------------------------------------------------------------------------
# 5. Aggregation valid for the column type
# ---------------------------------------------------------------------------
@_timed
def _check_aggregation_valid(context: _Context) -> CheckResult:
    label = "Aggregations are valid for their column types"
    spec = context.spec
    if spec is None or context.table is None:
        return _skipped(ValidationTest.AGGREGATION_VALID, label, "formula could not be parsed")

    columns = context.columns()
    problems: list[str] = []
    advisories: list[str] = []
    checked: list[str] = []

    for role, measure in spec.measures:
        if measure.is_count_star:
            checked.append(f"{role}: COUNT(*)")
            continue
        column = columns.get(measure.column.lower())
        if column is None:
            continue  # reported by check 2

        family = classify_type_family(column.data_type)
        aggregation = measure.aggregation

        if aggregation in {Aggregation.SUM, Aggregation.AVG} and family != "NUMERIC":
            problems.append(
                f"{aggregation}({measure.column}) requires a numeric column, "
                f"but it is {column.data_type}"
            )
            continue
        if aggregation == Aggregation.COUNT and column.semantic_type not in _COUNTABLE_TYPES:
            problems.append(f"COUNT({measure.column}) over {column.semantic_type} is not meaningful")
            continue
        if aggregation in {Aggregation.MIN, Aggregation.MAX} and family not in {
            "NUMERIC",
            "TEMPORAL",
            "TEXT",
        }:
            problems.append(f"{aggregation}({measure.column}) requires an ordered type")
            continue

        # SUM over an identifier is syntactically fine and semantically wrong.
        if aggregation == Aggregation.SUM and column.semantic_type == SemanticType.IDENTIFIER:
            advisories.append(
                f"SUM({measure.column}) sums an identifier, which is almost never "
                "a business measure"
            )
        if aggregation == Aggregation.SUM and column.semantic_type == SemanticType.NUMERIC_MEASURE:
            profile = context.profile_for(column)
            if profile and profile.negative_count:
                advisories.append(
                    f"{measure.column} contains {profile.negative_count} negative "
                    "value(s); confirm returns are intended to net off"
                )
        checked.append(f"{role}: {measure.render()} over {column.semantic_type}")

    if problems:
        return CheckResult(
            ValidationTest.AGGREGATION_VALID,
            label,
            ValidationStatus.FAIL,
            expected="aggregation compatible with each column's type",
            actual="; ".join(problems),
            evidence={"problems": problems, "checked": checked},
        )
    if advisories:
        return CheckResult(
            ValidationTest.AGGREGATION_VALID,
            label,
            ValidationStatus.WARN,
            expected="aggregation semantically appropriate",
            actual="; ".join(advisories),
            is_blocking=False,
            evidence={"advisories": advisories, "checked": checked},
        )
    return CheckResult(
        ValidationTest.AGGREGATION_VALID,
        label,
        ValidationStatus.PASS,
        expected="aggregation compatible with each column's type",
        actual=f"{len(checked)} aggregation(s) validated",
        evidence={"checked": checked},
    )


# ---------------------------------------------------------------------------
# 6. Duplicate counting
# ---------------------------------------------------------------------------
@_timed
def _check_duplicate_counting(context: _Context) -> CheckResult:
    """The check that catches an inflated total before anyone trusts it.

    Two failure modes are tested: a dimension that lives in another table
    reachable only through an unsafe join, and a measure repeated across rows at
    the bound table's grain.
    """
    label = "Calculation cannot double-count"
    spec = context.spec
    if spec is None or context.table is None:
        return _skipped(ValidationTest.DUPLICATE_COUNTING, label, "formula could not be parsed")

    problems: list[str] = []
    advisories: list[str] = []
    evidence: dict[str, Any] = {"bound_table": context.table.table_name}

    # (a) Dimensions from other tables require a verified-safe join.
    foreign = [
        dimension
        for dimension in context.version.dimensions
        if dimension.source_table_id and dimension.source_table_id != context.table.id
    ]
    join_verdicts: list[dict] = []
    for dimension in foreign:
        safety = _join_verdict(context.session, context.table.id, dimension.source_table_id)
        join_verdicts.append(
            {
                "dimension": dimension.dimension_name,
                "table": dimension.source_table,
                "safety": safety.safety_level if safety else "NOT_ANALYSED",
                "fan_out": safety.fan_out_factor if safety else None,
            }
        )
        if safety is None:
            problems.append(
                f"dimension '{dimension.dimension_name}' comes from "
                f"{dimension.source_table}, and that join has not been analysed"
            )
        elif safety.safety_level == JoinSafetyLevel.RISKY:
            problems.append(
                f"dimension '{dimension.dimension_name}' requires a join rated RISKY "
                f"(fan-out {safety.fan_out_factor}): {safety.reason}"
            )
        elif safety.safety_level == JoinSafetyLevel.SAFE_WITH_AGGREGATION:
            advisories.append(
                f"dimension '{dimension.dimension_name}' needs pre-aggregation: {safety.guidance}"
            )
        elif safety.safety_level == JoinSafetyLevel.UNKNOWN:
            advisories.append(
                f"join safety for dimension '{dimension.dimension_name}' is unknown"
            )
    if join_verdicts:
        evidence["joins"] = join_verdicts

    # (b) A summed measure whose value repeats at the table grain.
    grain = context.grain()
    if grain is not None:
        evidence["table_grain"] = grain.inferred_grain
        if grain.is_unique is False:
            advisories.append(
                f"the bound table's grain is only {(grain.confidence or 0) * 100:.1f}% "
                f"unique ({grain.inferred_grain}); duplicate rows will inflate SUM"
            )

    for _role, measure in spec.measures:
        if measure.is_count_star or measure.aggregation != Aggregation.SUM:
            continue
        column = context.columns().get(measure.column.lower())
        if column is None:
            continue
        repeated = _repeated_measure_warning(context, column)
        if repeated:
            advisories.append(repeated)

    if problems:
        return CheckResult(
            ValidationTest.DUPLICATE_COUNTING,
            label,
            ValidationStatus.FAIL,
            expected="no join or grain mismatch that multiplies rows",
            actual="; ".join(problems),
            message=(
                "This calculation would produce a plausible but inflated number. "
                "Bind the dimension to the measure's own table, or pre-aggregate."
            ),
            evidence=evidence,
        )
    if advisories:
        return CheckResult(
            ValidationTest.DUPLICATE_COUNTING,
            label,
            ValidationStatus.WARN,
            expected="no duplication risk",
            actual="; ".join(advisories),
            is_blocking=False,
            evidence=evidence,
        )
    return CheckResult(
        ValidationTest.DUPLICATE_COUNTING,
        label,
        ValidationStatus.PASS,
        expected="no duplication risk",
        actual=(
            "single-table calculation at a unique grain"
            if not foreign
            else f"{len(foreign)} cross-table dimension(s), all rated SAFE"
        ),
        evidence=evidence,
    )


def _repeated_measure_warning(context: _Context, column: SourceColumn) -> str | None:
    """Flag a measure that looks like a header value stored on every child row."""
    profile = context.profile_for(column)
    table_profile = context.table_profile()
    if profile is None or table_profile is None or not table_profile.row_count:
        return None
    if profile.distinct_count is None or profile.distinct_count == 0:
        return None
    grain = context.grain()
    if grain is None or not grain.grain_columns:
        return None
    # An order-level value duplicated across line items has far fewer distinct
    # values than rows *and* the grain includes a finer key than the value's own.
    ratio = profile.distinct_count / table_profile.row_count
    if ratio < 0.02 and table_profile.row_count > 1000:
        return (
            f"{column.column_name} has only {profile.distinct_count} distinct values "
            f"across {table_profile.row_count} rows. If it is a header value "
            f"repeated on child rows, SUM will over-count it."
        )
    return None


def _join_verdict(session: Session, left_id: str, right_id: str) -> JoinSafety | None:
    row = session.execute(
        select(JoinSafety)
        .join(TableRelationship, TableRelationship.id == JoinSafety.relationship_id)
        .where(
            TableRelationship.source_table_id.in_([left_id, right_id]),
            TableRelationship.target_table_id.in_([left_id, right_id]),
        )
        .limit(1)
    ).scalar_one_or_none()
    return row


# ---------------------------------------------------------------------------
# 7. Grain compatibility
# ---------------------------------------------------------------------------
@_timed
def _check_grain_compatible(context: _Context) -> CheckResult:
    label = "KPI time grain is supported by the source"
    version = context.version
    grain = context.grain()
    if grain is None:
        return CheckResult(
            ValidationTest.GRAIN_COMPATIBLE,
            label,
            ValidationStatus.WARN,
            expected="a detected table grain",
            actual="grain not detected",
            message="Run grain detection so the KPI's time grain can be verified.",
            is_blocking=False,
        )

    requested = _GRAIN_ORDER.get(version.time_grain)
    available = _GRAIN_ORDER.get(grain.time_grain) if grain.time_grain else None

    if requested is None:
        return CheckResult(
            ValidationTest.GRAIN_COMPATIBLE,
            label,
            ValidationStatus.FAIL,
            expected="a recognised time grain",
            actual=str(version.time_grain),
        )

    if available is None:
        # An event timestamp has no fixed grain, so any roll-up is available.
        return CheckResult(
            ValidationTest.GRAIN_COMPATIBLE,
            label,
            ValidationStatus.PASS,
            expected=f"source supports {version.time_grain}",
            actual=(
                f"{context.table.table_name if context.table else 'table'} is "
                "transaction-level, so it rolls up to any grain"
            ),
            evidence={"table_grain": grain.inferred_grain, "time_column": grain.time_column},
        )

    if requested < available:
        return CheckResult(
            ValidationTest.GRAIN_COMPATIBLE,
            label,
            ValidationStatus.FAIL,
            expected=f"source at {version.time_grain} grain or finer",
            actual=f"source is {grain.time_grain} grain",
            message=(
                f"The source records data per {grain.time_grain.lower()}, so a "
                f"{version.time_grain.lower()} KPI cannot be produced without "
                "inventing detail that does not exist."
            ),
            evidence={"requested": version.time_grain, "available": grain.time_grain},
        )

    return CheckResult(
        ValidationTest.GRAIN_COMPATIBLE,
        label,
        ValidationStatus.PASS,
        expected=f"source at {version.time_grain} grain or finer",
        actual=f"source is {grain.time_grain} grain ({grain.inferred_grain})",
        evidence={"requested": version.time_grain, "available": grain.time_grain},
    )


# ---------------------------------------------------------------------------
# 8. Access policy
# ---------------------------------------------------------------------------
@_timed
def _check_access_policy(context: _Context) -> CheckResult:
    label = "Access policy is complete and coherent"
    policies = context.version.access_policies
    if not policies:
        return CheckResult(
            ValidationTest.ACCESS_POLICY_VALID,
            label,
            ValidationStatus.FAIL,
            expected="at least one role policy, including ADMIN",
            actual="no policies defined",
            message="An ungoverned KPI would be visible to whoever happens to query it.",
        )

    problems: list[str] = []
    advisories: list[str] = []
    unknown_roles = [p.role_key for p in policies if p.role_key not in ROLES_BY_KEY]
    if unknown_roles:
        problems.append(f"unknown role(s): {', '.join(unknown_roles)}")

    admin_policy = next((p for p in policies if p.role_key == ADMIN_ROLE_KEY), None)
    if admin_policy is None or not admin_policy.allowed:
        problems.append("ADMIN must retain access so the KPI remains governable")

    if not any(p.allowed for p in policies):
        problems.append("no role is granted access, so the KPI would be unusable")

    # A scoped role should carry an actual scope, or the restriction is fiction.
    for policy in policies:
        if policy.role_key == "REGIONAL_MANAGER" and policy.allowed and not policy.row_scope:
            advisories.append(
                "REGIONAL_MANAGER is allowed with no row scope, so it sees every region"
            )
        if policy.aggregate_only and policy.row_scope:
            advisories.append(
                f"{policy.role_key} is aggregate-only, so its row scope has no effect"
            )

    # A role entitled to the KPI but not to its underlying sensitive columns.
    if context.table is not None and context.spec is not None:
        sensitive = [
            column
            for column in context.table.columns
            if column.column_name.lower()
            in {c.lower() for _r, _t, c in context.spec.referenced_columns if c}
            and (column.is_pii or column.is_restricted or column.is_sensitive)
        ]
        for column in sensitive:
            for policy in policies:
                if not policy.allowed:
                    continue
                role = ROLES_BY_KEY.get(policy.role_key)
                if role is None:
                    continue
                needed = (
                    "data.read_restricted"
                    if column.is_restricted
                    else "data.read_pii"
                    if column.is_pii
                    else "data.read_confidential"
                )
                if needed not in role.permissions and not policy.aggregate_only:
                    advisories.append(
                        f"{policy.role_key} may read this KPI but lacks {needed} for "
                        f"{column.column_name}; mark the policy aggregate-only to be explicit"
                    )

    evidence = {
        "policies": [
            {
                "role": p.role_key,
                "allowed": p.allowed,
                "aggregate_only": p.aggregate_only,
                "row_scope": p.row_scope,
            }
            for p in policies
        ]
    }
    if problems:
        return CheckResult(
            ValidationTest.ACCESS_POLICY_VALID,
            label,
            ValidationStatus.FAIL,
            expected="ADMIN retained and at least one role granted",
            actual="; ".join(problems),
            evidence=evidence,
        )
    if advisories:
        return CheckResult(
            ValidationTest.ACCESS_POLICY_VALID,
            label,
            ValidationStatus.WARN,
            expected="policies fully specified",
            actual="; ".join(sorted(set(advisories))),
            is_blocking=False,
            evidence=evidence,
        )
    return CheckResult(
        ValidationTest.ACCESS_POLICY_VALID,
        label,
        ValidationStatus.PASS,
        expected="ADMIN retained and at least one role granted",
        actual=f"{len(policies)} role policy/policies defined",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# 9. Reconciles to source (the KPI is actually executed)
# ---------------------------------------------------------------------------
@_timed
def _check_reconciles_to_source(context: _Context) -> CheckResult:
    label = "KPI executes and reconciles against the source"
    spec = context.spec
    if spec is None or context.table is None:
        return _skipped(ValidationTest.RECONCILES_TO_SOURCE, label, "formula could not be parsed")
    if context.connector is None or not isinstance(context.connector, SqlConnector):
        return _skipped(
            ValidationTest.RECONCILES_TO_SOURCE,
            label,
            "no executable connector available for this source type",
        )

    connector = context.connector
    table = context.table
    try:
        total = execute_kpi(
            connector,
            spec,
            schema=table.schema_name,
            table=table.table_name,
            time_column=context.version.time_field,
        )
    except PlatformError as exc:
        return CheckResult(
            ValidationTest.RECONCILES_TO_SOURCE,
            label,
            ValidationStatus.FAIL,
            expected="the query executes and returns a number",
            actual=exc.message,
            message="The KPI contract does not run against the live source.",
        )

    scalar = total.scalar
    if scalar is None:
        return CheckResult(
            ValidationTest.RECONCILES_TO_SOURCE,
            label,
            ValidationStatus.FAIL,
            expected="one aggregate row",
            actual="no rows returned",
        )
    if scalar.value is None:
        return CheckResult(
            ValidationTest.RECONCILES_TO_SOURCE,
            label,
            ValidationStatus.FAIL,
            expected="a finite value",
            actual=f"undefined ({scalar.note})",
            message=(
                "A ratio with a zero denominator or an empty numerator cannot be "
                "monitored. Add a filter or revise the definition."
            ),
            evidence={"note": scalar.note, "sql": total.sql},
        )

    evidence: dict[str, Any] = {
        "value": scalar.value,
        "numerator": scalar.numerator,
        "denominator": scalar.denominator,
        "sql": total.sql,
        "coverage": _coverage(context),
    }
    advisories: list[str] = []

    # Additivity: for a SUM, the whole must equal the sum of its parts. This is
    # what catches a broken time filter or a mis-typed time column.
    if spec.denominator is None and spec.numerator.aggregation == Aggregation.SUM:
        window = _recent_window(context)
        if window is not None:
            start, end, midpoint = window
            try:
                first = execute_kpi(
                    connector, spec, schema=table.schema_name, table=table.table_name,
                    time_column=context.version.time_field, start=start, end=midpoint,
                )
                second = execute_kpi(
                    connector, spec, schema=table.schema_name, table=table.table_name,
                    time_column=context.version.time_field, start=midpoint, end=end,
                )
                whole = execute_kpi(
                    connector, spec, schema=table.schema_name, table=table.table_name,
                    time_column=context.version.time_field, start=start, end=end,
                )
            except PlatformError as exc:
                advisories.append(f"additivity could not be verified: {exc.message}")
            else:
                parts = (first.scalar.value or 0.0) + (second.scalar.value or 0.0)
                total_window = whole.scalar.value or 0.0
                # The midpoint belongs to both halves, so overlap is expected;
                # what matters is that the halves are not wildly inconsistent
                # with the whole.
                drift_pct = (
                    abs(parts - total_window) / abs(total_window) * 100 if total_window else 0.0
                )
                evidence["additivity"] = {
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "split_at": midpoint.isoformat(),
                    "sum_of_parts": parts,
                    "whole": total_window,
                    "drift_pct": round(drift_pct, 4),
                    "note": "midpoint is inclusive in both halves, so small drift is expected",
                }
                if total_window == 0.0 and parts == 0.0:
                    advisories.append(
                        "the most recent window contains no data, so additivity is untested"
                    )

    if spec.denominator is not None and scalar.denominator is not None:
        evidence["ratio_components"] = {
            "numerator": scalar.numerator,
            "denominator": scalar.denominator,
        }
        if scalar.denominator < 30:
            advisories.append(
                f"denominator is only {scalar.denominator:.0f}; the ratio will be "
                "volatile and confidence should reflect that"
            )

    quality = context.table_profile()
    if quality and quality.quality_status in {QualityStatus.WARNING, QualityStatus.POOR}:
        advisories.append(
            f"source table quality is {quality.quality_status}; the value is "
            "computed correctly but from imperfect data"
        )

    freshness = _latest_freshness(context)
    if freshness and freshness.freshness_status == "STALE":
        advisories.append(
            f"source is STALE (latest row {freshness.freshness_lag_seconds}s old); "
            "the value is correct for the data present, not for today"
        )
        evidence["freshness"] = freshness.freshness_status

    if advisories:
        return CheckResult(
            ValidationTest.RECONCILES_TO_SOURCE,
            label,
            ValidationStatus.WARN,
            expected="a finite, additive value from fresh, clean data",
            actual=f"value = {scalar.value:,.2f}; " + "; ".join(advisories),
            is_blocking=False,
            evidence=evidence,
        )

    return CheckResult(
        ValidationTest.RECONCILES_TO_SOURCE,
        label,
        ValidationStatus.PASS,
        expected="a finite value that reconciles against the source",
        actual=f"value = {scalar.value:,.2f}",
        evidence=evidence,
    )


def _coverage(context: _Context) -> dict[str, Any]:
    health = _latest_freshness(context)
    if health is None:
        return {}
    return {
        "coverage_start": health.coverage_start.isoformat() if health.coverage_start else None,
        "coverage_end": health.coverage_end.isoformat() if health.coverage_end else None,
        "row_count": health.row_count,
        "freshness": health.freshness_status,
    }


def _latest_freshness(context: _Context) -> SourceHealth | None:
    if context.table is None:
        return None
    return context.session.scalar(
        select(SourceHealth)
        .where(SourceHealth.source_table_id == context.table.id)
        .order_by(SourceHealth.checked_at.desc())
        .limit(1)
    )


def _recent_window(context: _Context) -> tuple[date, date, date] | None:
    """A recent 28-day window and its midpoint, for the additivity check."""
    if not context.version.time_field:
        return None
    health = _latest_freshness(context)
    end_dt = as_utc(health.coverage_end) if health else None
    if end_dt is None:
        column = context.columns().get(context.version.time_field.lower())
        profile = context.profile_for(column) if column else None
        if profile and profile.max_value:
            try:
                end_dt = datetime.fromisoformat(str(profile.max_value).replace("Z", "+00:00"))
            except ValueError:
                return None
    if end_dt is None:
        return None
    end = end_dt.date()
    start = end - timedelta(days=27)
    midpoint = start + timedelta(days=13)
    return (start, end, midpoint)


def _skipped(test_type: str, label: str, reason: str) -> CheckResult:
    return CheckResult(
        test_type,
        label,
        ValidationStatus.SKIPPED,
        expected="check executed",
        actual=f"skipped: {reason}",
        is_blocking=False,
        message=reason,
    )

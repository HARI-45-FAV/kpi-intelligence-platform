"""Deterministic semantic and sensitivity classification.

Two deliberately unglamorous decisions:

* Classification runs in **two passes**. At discovery time only structure is
  available (declared type, key membership, name), so that is all the first pass
  uses. Cardinality-dependent judgements wait until profiling has real counts.
* Sensitivity is **name- and type-driven**, not model-driven. Sprint 1 builds the
  data model for classification; guessing PII with a language model would add a
  false-negative risk to the one place that must not have one. An administrator
  can override any classification, and the override is what the access checks
  read.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.connectors.base import ColumnMeta, ColumnStats
from app.models.base import Classification, SemanticType

# --- Sensitivity vocabulary ------------------------------------------------
_RESTRICTED_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "private_key",
    "credit_card", "card_number", "cardno", "cvv", "iban", "account_number",
    "routing_number", "security_answer",
)
_PII_DIRECT_HINTS = (
    "email", "e_mail", "phone", "mobile", "msisdn", "aadhaar", "aadhar", "pan_number",
    "passport", "ssn", "national_id", "date_of_birth", "dob", "birth_date",
    "address", "street", "postal_code", "postcode", "zipcode", "zip_code",
    "latitude", "longitude", "ip_address", "device_id",
)
# "name" alone is far too broad (product_name, campaign_name, region_name), so a
# person-context prefix is required.
_PII_NAME_HINTS = (
    "customer_name", "client_name", "contact_name", "user_name", "username",
    "first_name", "last_name", "full_name", "middle_name", "person_name",
    "employee_name", "guardian_name", "beneficiary_name",
)
_CONFIDENTIAL_HINTS = (
    "cost", "margin", "salary", "compensation", "payroll", "profit", "gross_margin",
    "commission", "bonus", "credit_limit", "internal_rating",
)

_IDENTIFIER_SUFFIXES = ("_id", "_key", "_code", "_ref", "_no", "_number", "_uuid", "_sk")
_IDENTIFIER_EXACT = ("id", "key", "uuid", "guid")

# Above this many distinct values a low-cardinality column stops being a
# practical breakdown dimension.
CATEGORICAL_MAX_DISTINCT = 200
CATEGORICAL_MAX_DISTINCT_PCT = 5.0


@dataclass(frozen=True, slots=True)
class SensitivityVerdict:
    classification: str
    is_pii: bool
    is_sensitive: bool
    is_restricted: bool
    reason: str


def classify_sensitivity(column_name: str, table_name: str = "") -> SensitivityVerdict:
    name = (column_name or "").lower()
    table = (table_name or "").lower()

    for hint in _RESTRICTED_HINTS:
        if hint in name:
            return SensitivityVerdict(
                Classification.RESTRICTED, False, True, True, f"name matches '{hint}'"
            )

    for hint in _PII_DIRECT_HINTS:
        if hint in name:
            return SensitivityVerdict(
                Classification.RESTRICTED, True, True, True, f"personal data: '{hint}'"
            )

    for hint in _PII_NAME_HINTS:
        if hint in name:
            return SensitivityVerdict(
                Classification.CONFIDENTIAL, True, True, False, f"personal name: '{hint}'"
            )
    # A bare "name" inside a person-shaped table is still a person's name.
    if name in {"name", "full_name"} and any(
        token in table for token in ("customer", "user", "employee", "contact", "person", "member")
    ):
        return SensitivityVerdict(
            Classification.CONFIDENTIAL, True, True, False, f"person name in '{table_name}'"
        )

    for hint in _CONFIDENTIAL_HINTS:
        if hint in name:
            return SensitivityVerdict(
                Classification.CONFIDENTIAL, False, True, False, f"commercially sensitive: '{hint}'"
            )

    return SensitivityVerdict(Classification.INTERNAL, False, False, False, "no sensitive signal")


def _looks_like_identifier(name: str) -> bool:
    lowered = (name or "").lower()
    return lowered in _IDENTIFIER_EXACT or lowered.endswith(_IDENTIFIER_SUFFIXES)


def classify_structural(column: ColumnMeta) -> str:
    """First pass: what can be known without reading any data."""
    if column.is_primary_key or column.is_foreign_key or _looks_like_identifier(column.column_name):
        return SemanticType.IDENTIFIER
    family = column.type_family
    if family == "BOOLEAN":
        return SemanticType.BOOLEAN_FLAG
    if family == "TEMPORAL":
        lowered = column.data_type.lower()
        return SemanticType.DATE if "date" in lowered and "time" not in lowered else SemanticType.TIMESTAMP
    if family == "NUMERIC":
        return SemanticType.NUMERIC_MEASURE
    if family == "TEXT":
        return SemanticType.TEXT
    return SemanticType.UNKNOWN


def refine_semantic_type(
    *,
    current: str,
    column_name: str,
    type_family: str,
    is_primary_key: bool,
    is_foreign_key: bool,
    stats: ColumnStats,
) -> str:
    """Second pass: sharpen the guess now that cardinality is known.

    Structural identifiers and temporal columns are left alone — profiling
    cannot make a foreign key stop being a foreign key.
    """
    if is_primary_key or is_foreign_key or _looks_like_identifier(column_name):
        return SemanticType.IDENTIFIER
    if current in {SemanticType.DATE, SemanticType.TIMESTAMP}:
        return current

    distinct = stats.distinct_count
    if distinct is None:
        return current

    # A 0/1 integer column is a flag, whatever the declared type says.
    if type_family == "NUMERIC" and distinct <= 2:
        values = {str(v) for v in ([stats.min_value, stats.max_value] if stats.min_value is not None else [])}
        if values and values <= {"0", "1", "0.0", "1.0", "True", "False"}:
            return SemanticType.BOOLEAN_FLAG

    if type_family == "BOOLEAN":
        return SemanticType.BOOLEAN_FLAG

    low_cardinality = distinct <= CATEGORICAL_MAX_DISTINCT and (
        stats.distinct_pct is None or stats.distinct_pct <= CATEGORICAL_MAX_DISTINCT_PCT or distinct <= 25
    )

    if type_family == "TEXT":
        if stats.is_unique:
            return SemanticType.IDENTIFIER
        return SemanticType.CATEGORICAL if low_cardinality else SemanticType.TEXT

    if type_family == "NUMERIC":
        # Codes stored as integers behave like categories, not measures.
        if low_cardinality and distinct <= 25:
            return SemanticType.CATEGORICAL
        return SemanticType.NUMERIC_MEASURE

    return current


def is_measure(semantic_type: str) -> bool:
    return semantic_type == SemanticType.NUMERIC_MEASURE


def is_dimension_candidate(semantic_type: str) -> bool:
    return semantic_type in {SemanticType.CATEGORICAL, SemanticType.BOOLEAN_FLAG}


def is_time_candidate(semantic_type: str) -> bool:
    return semantic_type in {SemanticType.DATE, SemanticType.TIMESTAMP}


def is_key_candidate(semantic_type: str) -> bool:
    return semantic_type in {
        SemanticType.IDENTIFIER,
        SemanticType.CATEGORICAL,
        SemanticType.DATE,
        SemanticType.TIMESTAMP,
        SemanticType.BOOLEAN_FLAG,
    }

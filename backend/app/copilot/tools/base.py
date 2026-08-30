"""The contract every governed tool obeys.

A tool is the only thing the model can actually *do*. That makes this file the
narrowest and most important part of the Copilot, so the guarantees are
structural rather than advisory:

**No tool can name a company.** ``FORBIDDEN_PARAMETERS`` is checked when a tool
is registered, not when it is called, so a tool declaring a ``company_id``,
``sql``, ``query`` or ``connection_string`` parameter fails at import time. The
company always comes from ``context.access.company.id``, which was resolved from
a membership row. The model has no vocabulary for "some other tenant".

**Permissions are checked before the handler runs.** Each tool declares the
permission keys it needs and ``ToolRegistry.invoke`` calls
``access.require(*permissions)`` first. The keys are the existing ones --
``kpi.read``, ``document.read``, ``source.read``, ``telemetry.read`` -- so a
VIEWER who cannot read documents through the REST API cannot read them through
the Copilot either. There is no ``copilot.*`` permission that would let this
layer grant itself something the rest of the platform does not.

**A refusal is a result, not a crash.** When a tool denies, misses or fails, the
turn continues with a structured ``error`` the model can read and report. That
matters: the alternative is a 500 where the honest answer is "you are not
entitled to that" or "that KPI has never been validated".

**Arguments are validated against the declared schema.** A tiny checker, not a
dependency -- the schemas are deliberately simple enough that pulling in a JSON
Schema library would be the more fragile choice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.copilot.context import CopilotContext
from app.core.deps import AccessContext
from app.core.errors import PlatformError
from app.llm.provider import LLMToolSpec

# Parameter names a governed tool may never declare. The first group would let
# the model choose its own tenant; the rest would turn a named, validated tool
# back into arbitrary database access -- exactly what this layer exists to
# prevent. Checked at registration, so a mistake is an import error.
FORBIDDEN_PARAMETERS: frozenset[str] = frozenset(
    {
        "company_id",
        "company",
        "tenant_id",
        "tenant",
        "user_id",
        "role",
        "role_key",
        "permissions",
        "sql",
        "query",
        "statement",
        "raw_sql",
        "where",
        "filter_sql",
        "connection",
        "connection_string",
        "dsn",
        "credentials",
        "password",
        "api_key",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a handler hands back: data, evidence to cite, or a refusal."""

    data: dict[str, Any] | None = None
    # Evidence payloads in ``EvidenceBundle.add`` keyword form. The registry does
    # not add them itself -- the orchestrator owns the bundle -- but carrying
    # them here keeps "what the model saw" and "what the user can check" in step.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    # Set when a tool answered but something material was withheld or missing,
    # so the answer can say so instead of implying completeness.
    caveats: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict[str, Any]:
        """The JSON the model receives. Never includes internal object state."""
        if self.error is not None:
            return {"ok": False, "error": self.error}
        payload: dict[str, Any] = {"ok": True, "result": self.data if self.data is not None else {}}
        if self.caveats:
            payload["caveats"] = self.caveats
        return payload


def refuse(message: str) -> ToolResult:
    return ToolResult(error=message)


Handler = Callable[[CopilotContext, dict[str, Any]], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    # Existing platform permission keys. Empty means "any authenticated member
    # of this company", which is only used for tools describing the platform's
    # own capabilities.
    permissions: tuple[str, ...]
    parameters: dict[str, Any]
    handler: Handler

    def as_llm_spec(self) -> LLMToolSpec:
        return LLMToolSpec(
            name=self.name, description=self.description, parameters=self.parameters
        )


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Coerce and check one tool call's arguments.

    Raises ``ValueError`` with a message written for the model: it is fed back as
    the tool result so a malformed call can be corrected on the next iteration
    rather than ending the turn.
    """
    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])

    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ValueError(
            f"Unknown parameter(s): {', '.join(unknown)}. "
            f"Accepted: {', '.join(sorted(properties)) or 'none'}."
        )

    missing = [name for name in required if arguments.get(name) in (None, "")]
    if missing:
        raise ValueError(f"Missing required parameter(s): {', '.join(missing)}.")

    cleaned: dict[str, Any] = {}
    for name, value in arguments.items():
        if value is None:
            continue
        rule = properties[name]
        expected = rule.get("type", "string")
        allowed = _TYPE_CHECKS.get(expected, (str,))

        # Models routinely send "7" for an integer. Accepting a clean numeric
        # string is not laxness; refusing it would waste an iteration on a
        # difference that carries no meaning.
        if expected in {"integer", "number"} and isinstance(value, str):
            try:
                value = int(value) if expected == "integer" else float(value)
            except ValueError as exc:
                raise ValueError(f"Parameter '{name}' must be a {expected}.") from exc
        elif expected == "boolean" and isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "false"}:
                value = lowered == "true"

        if not isinstance(value, allowed) or (expected == "integer" and isinstance(value, bool)):
            raise ValueError(f"Parameter '{name}' must be a {expected}.")

        enum = rule.get("enum")
        if enum and value not in enum:
            raise ValueError(f"Parameter '{name}' must be one of: {', '.join(map(str, enum))}.")

        if expected == "integer":
            minimum, maximum = rule.get("minimum"), rule.get("maximum")
            if minimum is not None and value < minimum:
                value = minimum
            if maximum is not None and value > maximum:
                value = maximum

        cleaned[name] = value

    return cleaned


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """The complete set of things the model may do, and the gate in front of it."""

    __slots__ = ("_tools",)

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} is already registered.")

        declared = set((spec.parameters.get("properties") or {}))
        forbidden = sorted(declared & FORBIDDEN_PARAMETERS)
        if forbidden:
            raise ValueError(
                f"Tool {spec.name!r} declares forbidden parameter(s): {', '.join(forbidden)}. "
                "Company scope comes from the authenticated request, and no tool may "
                "accept SQL, connection or credential input."
            )

        self._tools[spec.name] = spec
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def available_for(self, access: AccessContext) -> tuple[ToolSpec, ...]:
        """Only the tools this caller's role can actually use.

        Advertising a tool the caller will be denied wastes an iteration and
        invites the model to speculate about what the refused tool would have
        said. A VIEWER without ``document.read`` simply never learns that a
        document tool exists.

        Takes the ``AccessContext`` rather than the ``CopilotContext`` because
        entitlement is all this decision needs -- which lets the status endpoint
        report the caller's reach without opening a Copilot turn.
        """
        return tuple(
            spec
            for spec in (self._tools[name] for name in self.names)
            if all(access.has(permission) for permission in spec.permissions)
        )

    def llm_specs(self, context: CopilotContext) -> list[LLMToolSpec]:
        return [spec.as_llm_spec() for spec in self.available_for(context.access)]

    def invoke(self, context: CopilotContext, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Run one tool call under the caller's entitlement.

        Every failure path returns a ``ToolResult`` rather than raising: the
        model needs to be told "denied" or "not found" so it can say so.
        """
        spec = self._tools.get(name)
        if spec is None:
            return refuse(
                f"There is no tool named '{name}'. Available: {', '.join(self.names)}."
            )

        try:
            context.access.require(*spec.permissions)
        except PlatformError as exc:
            return refuse(
                f"Not permitted: {exc.message} This information cannot be included in "
                "the answer."
            )

        try:
            cleaned = validate_arguments(spec.parameters, arguments or {})
        except ValueError as exc:
            return refuse(str(exc))

        try:
            return spec.handler(context, cleaned)
        except PlatformError as exc:
            # Domain errors are the platform speaking: not found, not in scope,
            # not permitted, connector limitation. All safe to relay verbatim --
            # none of them carry credentials or business rows.
            return refuse(exc.message)
        except Exception:  # pragma: no cover - defensive
            # An unexpected failure must not leak a traceback into a prompt.
            return refuse(
                f"The '{name}' tool failed unexpectedly. Report that this information "
                "could not be retrieved."
            )

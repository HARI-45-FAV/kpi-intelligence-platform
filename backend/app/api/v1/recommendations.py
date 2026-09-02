"""The recommendation API: what a stored result suggests someone consider doing.

The last link in the chain the rest of this package implements — detect, explain,
locate, and now *suggest* — and deliberately the thinnest. Two routes:

``GET  /companies/{id}/detection-runs/{run_id}/recommendations``
    Everything the Results page shows under "what to consider doing next",
    derived on read from rows that already exist. Nothing is generated, no model
    is called, and no source is queried: the same stored evidence always yields
    the same wording, which is what makes it safe beside a governed figure.
``POST /companies/{id}/detection-runs/{run_id}/recommendation-feedback``
    Whether the advice was useful, and how far the reader's own review got. An
    upsert per reader per recommendation, so submitting again is a correction.

**Why there is no POST that generates.** A recommendation is a *view* of a
detection run and its stored breakdown, so it is a GET, and there is no
``recommendations`` table behind it. A persisted recommendation would be free to
disagree with the evidence it came from the moment somebody drilled a level
deeper — and the one thing this layer cannot afford is advice that no longer
matches the numbers printed beside it.

**Permissions, and why they differ between the two routes.** Reading needs
``analytics.read``, the same gate as the result itself. Recommendations *sharpen*
for a caller who also holds ``investigation.read``, because naming a region or a
store is naming a part of the business, and the permission that governs seeing a
breakdown governs seeing one quoted back inside advice. A caller without it gets
the same shape, scoped to the KPI, and is told why. Writing feedback needs
``investigation.read`` — the same gate investigation findings use, because both
are a person putting their name to a conclusion.

As everywhere else, ``result`` is what a business surface may render and
``evidence`` — which stored rows were read, which KPI family matched, what the
registered direction and criticality were — is returned only to callers already
entitled to read KPI definitions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.core.clock import utcnow
from app.core.deps import (
    AccessContext,
    SessionDep,
    load_scoped,
    require_permissions,
)
from app.core.errors import ValidationFailure
from app.models.base import RecommendationActionStatus, RecommendationUsefulness
from app.models.detection import DetectionRun
from app.models.recommendation import RecommendationFeedback
from app.schemas import RecommendationFeedbackIn
from app.services import audit
from app.services import recommendation as recommendation_service

router = APIRouter(tags=["recommendations"])

_USEFULNESS = tuple(str(value) for value in RecommendationUsefulness)
_ACTION_STATUSES = tuple(str(value) for value in RecommendationActionStatus)


def _feedback_out(row: RecommendationFeedback) -> dict:
    return {
        "recommendation_key": row.recommendation_key,
        "usefulness": row.usefulness,
        "action_status": row.action_status,
        "comment": row.comment,
        "lever_key": row.lever_key,
        "target_entity": row.target_entity,
        "submitted_by_email": row.created_by_email,
        "submitted_at": row.submitted_at.isoformat(),
    }


def _stored_feedback(session: SessionDep, access: AccessContext, run_id: str) -> list[dict]:
    """This company's feedback on this result, newest first.

    Company-wide rather than per-reader: a manager who marked an action taken has
    said something the next reader needs to know, and hiding it would let two
    people start the same review.
    """

    rows = session.scalars(
        select(RecommendationFeedback)
        .where(
            RecommendationFeedback.company_id == access.company.id,
            RecommendationFeedback.detection_run_id == run_id,
        )
        .order_by(RecommendationFeedback.submitted_at.desc())
        .limit(200)
    )
    return [_feedback_out(row) for row in rows]


@router.get(
    "/companies/{company_id}/detection-runs/{run_id}/recommendations",
    summary="Suggested actions derived from one stored result",
)
def get_recommendations(
    run_id: str,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("analytics.read")),
) -> dict:
    """Derive the recommendation set for one stored run, on read.

    ``load_scoped`` is what makes the run this caller's to see: the company comes
    from the resolved access context, never from the path, so a run id from another
    tenant is a 404 rather than a leak.
    """

    run: DetectionRun = load_scoped(session, DetectionRun, run_id, access)
    built = recommendation_service.build(session, access, run)

    payload: dict = {
        "result": built.business_view(),
        "run_id": run.id,
        "feedback": _stored_feedback(session, access, run.id),
        # Offered by the server rather than hardcoded in the client, so a screen can
        # never present a response the writer would reject.
        "feedback_options": {
            "usefulness": list(_USEFULNESS),
            "action_status": list(_ACTION_STATUSES),
        },
        "may_submit_feedback": access.has("investigation.read"),
    }
    if access.has("kpi.read"):
        payload["evidence"] = built.evidence()
    return payload


@router.post(
    "/companies/{company_id}/detection-runs/{run_id}/recommendation-feedback",
    summary="Record whether a recommendation was useful, and what was done about it",
)
def submit_recommendation_feedback(
    run_id: str,
    payload: RecommendationFeedbackIn,
    request: Request,
    session: SessionDep,
    access: AccessContext = Depends(require_permissions("investigation.read")),
) -> dict:
    """Upsert one reader's response to one recommendation.

    The key is validated against the recommendations this run *actually* produces
    rather than accepted as given. Feedback on advice the platform never offered
    would be an orphan row that no screen could ever show, and validating it here
    also means the lever and target area stored beside it are the engine's own,
    not the client's.
    """

    run: DetectionRun = load_scoped(session, DetectionRun, run_id, access)

    usefulness = payload.usefulness.strip().upper()
    if usefulness not in _USEFULNESS:
        raise ValidationFailure(
            f"'{payload.usefulness}' is not a recognised response. "
            f"Expected one of: {', '.join(_USEFULNESS)}."
        )
    action_status = payload.action_status.strip().upper()
    if action_status not in _ACTION_STATUSES:
        raise ValidationFailure(
            f"'{payload.action_status}' is not a recognised action status. "
            f"Expected one of: {', '.join(_ACTION_STATUSES)}."
        )

    built = recommendation_service.build(session, access, run)
    match = next(
        (item for item in built.recommendations if item.key == payload.recommendation_key),
        None,
    )
    if match is None:
        raise ValidationFailure(
            "That recommendation is not part of this result's current recommendation set. "
            "Reload the result and respond to a recommendation shown on it."
        )

    existing = session.scalars(
        select(RecommendationFeedback).where(
            RecommendationFeedback.company_id == access.company.id,
            RecommendationFeedback.detection_run_id == run.id,
            RecommendationFeedback.recommendation_key == match.key,
            RecommendationFeedback.created_by_user_id == access.user.id,
        )
    ).first()

    comment = (payload.comment or "").strip() or None
    target_entity = None if match.target is None else match.target.chain_label

    if existing is None:
        row = RecommendationFeedback(
            company_id=access.company.id,
            detection_run_id=run.id,
            kpi_key=run.kpi_key,
            recommendation_key=match.key,
            lever_key=match.lever_key,
            target_entity=target_entity,
            usefulness=usefulness,
            action_status=action_status,
            comment=comment,
            created_by_user_id=access.user.id,
            created_by_email=access.user.email,
            submitted_at=utcnow(),
        )
        session.add(row)
    else:
        row = existing
        row.usefulness = usefulness
        row.action_status = action_status
        row.comment = comment
        row.lever_key = match.lever_key
        row.target_entity = target_entity
        row.submitted_at = utcnow()
    session.flush()

    audit.record(
        session,
        access=access,
        action=audit.AuditAction.RECOMMENDATION_FEEDBACK_RECORDED,
        resource_type="recommendation_feedback",
        resource_id=row.id,
        resource_label=f"{run.kpi_name} {run.target_date.isoformat()}",
        summary=(
            f"Recommendation marked {usefulness.replace('_', ' ').lower()} "
            f"({action_status.replace('_', ' ').lower()}) on {match.lever_label}"
        ),
        details={
            "kpi_key": run.kpi_key,
            "target_date": run.target_date.isoformat(),
            "detection_run_id": run.id,
            "recommendation_key": match.key,
            "lever_key": match.lever_key,
            "usefulness": usefulness,
            "action_status": action_status,
        },
        request=request,
    )
    session.commit()
    session.refresh(row)
    return {"feedback": _feedback_out(row)}

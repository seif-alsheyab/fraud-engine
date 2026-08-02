"""Read-only reference data and the review queue."""

from uuid import UUID

from fastapi import APIRouter, Query

from fraud_engine.api.schemas import ListEntryRequest, ReviewResolveRequest
from fraud_engine.config import get_settings
from fraud_engine.db.pool import connection
from fraud_engine.domain.entities import display_hint, hash_value
from fraud_engine.lib.errors import ConflictError, NotFoundError
from fraud_engine.repositories import decision_repository as dr
from fraud_engine.repositories import entity_repository as er
from fraud_engine.repositories import reference_repository as rr

router = APIRouter(prefix="/v1", tags=["reference"])


@router.get("/features")
async def features() -> dict:
    """The feature registry.

    Exposed so a rule-authoring UI can offer only valid features instead of
    hardcoding a list that drifts out of sync with the database.
    """
    async with connection() as conn:
        return {"data": await rr.list_feature_definitions(conn)}


@router.get("/review-queue")
async def review_queue(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    """Open review cases, most urgent SLA first."""
    async with connection() as conn:
        return {"data": await dr.list_open_review_cases(conn, limit=limit)}


@router.post("/review-cases/{case_id}/resolve")
async def resolve_case(case_id: UUID, request: ReviewResolveRequest) -> dict:
    """Record an analyst's verdict on a held case."""
    async with connection() as conn:
        row = await dr.resolve_review_case(
            conn, case_id=case_id, disposition=request.disposition,
            analyst_note=request.analyst_note, assigned_to=request.assigned_to,
        )
        if row is None:
            # Either the case does not exist, or a colleague resolved it
            # first. Returning 409 rather than silently overwriting means
            # two analysts working the same queue cannot erase each other.
            raise ConflictError(
                f"Review case {case_id} is not open, or was already resolved."
            )
        return {"data": row}


@router.post("/lists", status_code=201)
async def add_to_list(request: ListEntryRequest) -> dict:
    """Add an entity to the allow, deny or watch list.

    The raw value is hashed on the way in, exactly as in the decision path,
    so the same card produces the same entity and the list actually matches.
    """
    settings = get_settings()
    async with connection() as conn:
        merchant_id = None
        if request.merchant_code:
            merchant = await rr.find_merchant_by_code(conn, request.merchant_code)
            if merchant is None:
                raise NotFoundError(f"Merchant {request.merchant_code} not found.")
            merchant_id = merchant["id"]

        value_hash = hash_value(
            request.entity_type, request.value, settings.entity_hash_salt
        )
        entity = await er.upsert_entity(
            conn, request.entity_type, value_hash,
            display_hint(request.entity_type, request.value) or None,
            __import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        row = await er.add_list_entry(
            conn, list_type=request.list_type, entity_id=entity["id"],
            merchant_id=merchant_id, reason=request.reason,
            added_by=request.added_by, expires_at=request.expires_at,
        )
        # The raw value is NOT echoed back.
        return {
            "data": {
                "id": str(row["id"]),
                "list_type": row["list_type"],
                "entity_type": request.entity_type,
                "display_hint": entity["display_hint"],
                "expires_at": row["expires_at"],
            }
        }

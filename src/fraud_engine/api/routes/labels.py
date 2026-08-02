"""Label ingestion: the truth, arriving weeks later."""

from datetime import UTC, datetime

from fastapi import APIRouter

from fraud_engine.api.schemas import LabelRequest
from fraud_engine.db.pool import connection
from fraud_engine.lib.errors import NotFoundError
from fraud_engine.repositories import decision_repository as dr
from fraud_engine.repositories import reference_repository as rr

router = APIRouter(prefix="/v1", tags=["labels"])


@router.post("/labels", status_code=201)
async def add_label(request: LabelRequest) -> dict:
    """Attach an outcome to a past transaction.

    days_to_label is computed here rather than supplied, so it cannot be
    wrong. It drives how long a period must age before its performance
    figures can be trusted -- chargebacks take 30-90 days, so last week
    always looks fraud-free.
    """
    async with connection() as conn:
        merchant = await rr.find_merchant_by_code(conn, request.merchant_code)
        if merchant is None:
            raise NotFoundError(f"Merchant {request.merchant_code} not found.")

        txn = await dr.find_transaction_by_external_id(
            conn, merchant["id"], request.external_id
        )
        if txn is None:
            raise NotFoundError(
                f"No transaction {request.external_id} for {request.merchant_code}."
            )

        labelled_at = request.labelled_at or datetime.now(UTC)
        days = max(0, (labelled_at - txn["occurred_at"]).days)

        row = await dr.insert_label(conn, {
            "transaction_id": txn["id"],
            "label": request.label,
            "source": request.source,
            "reason_code": request.reason_code,
            "amount_minor": txn["amount_minor"],
            "labelled_at": labelled_at,
            "days_to_label": days,
        })

        # None means this source already labelled this transaction. Reported
        # honestly rather than pretending a new label was created: the same
        # chargeback file gets loaded twice more often than anyone admits.
        return {
            "created": row is not None,
            "duplicate": row is None,
            "transaction_id": str(txn["id"]),
            "days_to_label": days,
        }

"""Performance reporting and backtesting."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Query

from fraud_engine.db.pool import connection
from fraud_engine.lib.errors import NotFoundError
from fraud_engine.repositories import reference_repository as rr
from fraud_engine.services.analytics_service import backtest_ruleset, performance_report

router = APIRouter(prefix="/v1", tags=["analytics"])


@router.get("/performance")
async def performance(
    merchant_code: str,
    days: int = Query(default=90, ge=1, le=730),
) -> dict:
    """Confusion matrix, per-rule lift, and label coverage for a period.

    Defaults to 90 days because a shorter window is mostly unlabelled, and
    an unlabelled period reports flatteringly good performance for the
    simple reason that no chargebacks have arrived yet.
    """
    async with connection() as conn:
        merchant = await rr.find_merchant_by_code(conn, merchant_code)
        if merchant is None:
            raise NotFoundError(f"Merchant {merchant_code} not found.")

        now = datetime.now(UTC)
        return await performance_report(
            conn, merchant_id=merchant["id"],
            since=now - timedelta(days=days), before=now,
        )


@router.get("/backtest")
async def backtest(
    merchant_code: str,
    candidate_ruleset_id: UUID,
    days: int = Query(default=90, ge=1, le=730),
) -> dict:
    """Replay real history against a candidate ruleset.

    Read-only. Uses the frozen feature snapshots, so the candidate answers
    exactly the questions the live engine faced rather than today's versions
    of them.
    """
    async with connection() as conn:
        merchant = await rr.find_merchant_by_code(conn, merchant_code)
        if merchant is None:
            raise NotFoundError(f"Merchant {merchant_code} not found.")

        now = datetime.now(UTC)
        return await backtest_ruleset(
            conn, merchant_id=merchant["id"],
            candidate_ruleset_id=candidate_ruleset_id,
            since=now - timedelta(days=days), before=now,
        )

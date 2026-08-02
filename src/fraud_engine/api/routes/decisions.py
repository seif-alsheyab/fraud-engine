"""The decision endpoint: the hot path."""

import time

from fastapi import APIRouter, Response

from fraud_engine.api.schemas import DecisionRequest, DecisionResponse
from fraud_engine.config import get_settings
from fraud_engine.db.pool import connection
from fraud_engine.lib.logging import get_logger
from fraud_engine.services.decision_service import decide_payment

router = APIRouter(prefix="/v1", tags=["decisions"])


@router.post("/decide", response_model=DecisionResponse)
async def decide(request: DecisionRequest, response: Response) -> dict:
    """Score one payment and return a decision.

    The whole call runs in ONE database transaction. A payment stored with
    no decision, or a decision whose payment vanished, are both corrupt
    states -- and a fraud engine that cannot say why it approved something
    is worse than no fraud engine.
    """
    settings = get_settings()
    started = time.perf_counter()

    async with connection() as conn:
        result = await decide_payment(conn, request.model_dump())

    total_ms = int((time.perf_counter() - started) * 1000)

    # Surfaced as a header so a caller can alert on it without parsing the
    # body, and so a load test sees it immediately.
    response.headers["X-Decision-Latency-Ms"] = str(total_ms)

    log = get_logger()
    # Note what is NOT logged: no card, no email, no IP, no device. The
    # decision and the score are operational facts; the identifiers are not.
    fields = {
        "merchant": request.merchant_code,
        "external_id": request.external_id,
        "decision": result["decision"],
        "score": result["score"],
        "latency_ms": total_ms,
        "replay": result["idempotent_replay"],
    }
    if total_ms > settings.decision_latency_budget_ms:
        # A budget breach is a defect, not a slow day: the payment gateway
        # times out and a good sale is lost.
        log.warn("decision exceeded latency budget",
                 budget_ms=settings.decision_latency_budget_ms, **fields)
    else:
        log.info("decision", **fields)

    return result

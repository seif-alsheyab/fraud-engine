"""The decision pipeline.

One payment in, one decision out, everything recorded. The order of
operations matters and is deliberate:

  1. Idempotency check FIRST. A retried payment returns the ORIGINAL
     decision. Re-deciding would produce a different answer (velocity has
     moved on) for the same payment, and the merchant would see one payment
     approved and then declined.

  2. Resolve entities BEFORE computing velocity, because velocity is
     counted per entity.

  3. Insert the transaction BEFORE deciding, so the decision has something
     to reference -- but the transaction is excluded from its own velocity
     counters via the `before` bound.

  4. Everything in ONE transaction. A stored payment with no decision, or a
     decision whose payment vanished, are both corrupt states.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from fraud_engine.config import get_settings
from fraud_engine.domain.conditions import referenced_features
from fraud_engine.domain.entities import display_hint, hash_value
from fraud_engine.domain.scoring import Rule, Thresholds, decide
from fraud_engine.lib.errors import NoActiveRulesetError, NotFoundError
from fraud_engine.repositories import decision_repository as dr
from fraud_engine.repositories import entity_repository as er
from fraud_engine.repositories import reference_repository as rr
from fraud_engine.services.feature_service import compute_features

# How long an analyst has to work a REVIEW case before it breaches SLA.
REVIEW_SLA_HOURS = 4


def _to_rules(rows: list[dict[str, Any]]) -> list[Rule]:
    return [
        Rule(
            code=r["code"],
            name=r["name"],
            condition=r["condition"],
            weight=r["weight"],
            hard_action=r["hard_action"],
            is_enabled=r["is_enabled"],
        )
        for r in rows
    ]


async def _resolve_entities(
    conn: AsyncConnection, payload: dict[str, Any], salt: str, seen_at: datetime
) -> dict[str, Any]:
    """Hash each supplied identifier and upsert its entity row.

    Raw values never leave this function. What lands in the database is a
    salted hash plus a display hint safe to show a human.
    """
    mapping = {
        "CARD": payload.get("card_number"),
        "EMAIL": payload.get("email"),
        "DEVICE": payload.get("device_fingerprint"),
        "IP": payload.get("ip_address"),
        "ACCOUNT": payload.get("account_id"),
    }
    resolved: dict[str, Any] = {}
    for entity_type, raw in mapping.items():
        if not raw:
            resolved[entity_type] = None
            continue
        entity = await er.upsert_entity(
            conn,
            entity_type,
            hash_value(entity_type, str(raw), salt),
            display_hint(entity_type, str(raw)) or None,
            seen_at,
        )
        resolved[entity_type] = entity["id"]
    return resolved


async def decide_payment(
    conn: AsyncConnection, payload: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Score one payment and record the result."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    started = time.perf_counter()

    merchant = await rr.find_merchant_by_code(conn, payload["merchant_code"])
    if merchant is None or not merchant["is_active"]:
        raise NotFoundError(f"Merchant {payload['merchant_code']} not found or inactive.")

    # 1. Idempotency. A gateway timeout means a retry, and a retry must not
    #    become a second payment with a second (different) answer.
    existing = await dr.find_transaction_by_external_id(
        conn, merchant["id"], payload["external_id"]
    )
    if existing is not None:
        prior = await dr.find_live_decision_for_transaction(conn, existing["id"])
        if prior is not None:
            return {
                "decision_id": prior["id"],
                "transaction_id": existing["id"],
                "decision": prior["decision"],
                "score": prior["score"],
                "triggered_rules": prior["triggered_rules"],
                "features": prior["features"],
                "latency_ms": prior["latency_ms"],
                "idempotent_replay": True,
            }

    # 2. The ruleset. Missing configuration is an ERROR, never a silent
    #    approve -- approving everything because nobody configured the
    #    merchant is the most expensive failure a fraud engine has.
    ruleset = await rr.find_ruleset_by_status(conn, merchant["id"], "ACTIVE")
    if ruleset is None:
        raise NoActiveRulesetError(
            f"Merchant {merchant['code']} has no ACTIVE ruleset. Refusing to decide."
        )

    rule_rows = await rr.list_rules(conn, ruleset["id"])
    rules = _to_rules(rule_rows)

    # Only compute features some rule actually asks about.
    required: set[str] = set()
    for r in rules:
        required |= referenced_features(r.condition)

    occurred_at = payload.get("occurred_at") or now
    entity_ids = await _resolve_entities(conn, payload, settings.entity_hash_salt, occurred_at)

    bin_info = None
    if payload.get("card_bin"):
        bin_info = await rr.find_card_bin(conn, payload["card_bin"])

    # 3. Features are computed with `before = occurred_at`, so this payment
    #    is excluded from its own velocity counters. Without that, every
    #    card would show a velocity of at least 1 on its first ever use.
    features = await compute_features(
        conn,
        txn={
            "amount_minor": payload["amount_minor"],
            "is_card_present": payload.get("is_card_present", False),
            "avs_match": payload.get("avs_match"),
            "cvv_match": payload.get("cvv_match"),
            "three_ds_status": payload.get("three_ds_status"),
            "ip_country": payload.get("ip_country"),
            "billing_country": payload.get("billing_country"),
            "shipping_country": payload.get("shipping_country"),
            # Categorical attributes the acquirer sends alongside the
            # authorisation. Absent means None, never a stand-in: see
            # transaction_features for why a guessed default is worse than
            # an admitted unknown.
            "product_code": payload.get("product_code"),
            "card_type": payload.get("card_type"),
            "addr_match": payload.get("addr_match"),
            "dist_from_billing": payload.get("dist_from_billing"),
            "has_identity_data": payload.get("has_identity_data"),
        },
        merchant_id=merchant["id"],
        entity_ids=entity_ids,
        bin_info=bin_info,
        required=required,
        now=occurred_at,
        # Aggregates the PROCESSOR computed. The engine cannot derive these
        # and never will; it only validates and records them.
        supplied_features=payload.get("supplied_features"),
    )

    txn = await dr.insert_transaction(conn, {
        "merchant_id": merchant["id"],
        "external_id": payload["external_id"],
        "amount_minor": payload["amount_minor"],
        # `or`, not .get(k, default). dict.get returns its default only
        # when the key is ABSENT; Pydantic emits every field, so an
        # omitted currency arrives as a PRESENT key set to None and goes
        # straight into a NOT NULL column.
        #
        # This is the defect a live smoke test found and 130 tests missed,
        # because every test payload happened to supply a currency.
        # Fields like channel and is_card_present were never at risk:
        # Pydantic gives them real defaults, so they are never None.
        "currency": payload.get("currency") or merchant["currency"],
        "card_bin": payload.get("card_bin") if bin_info else None,
        "card_last4": display_hint("CARD", str(payload["card_number"]))
        if payload.get("card_number") else None,
        "card_entity_id": entity_ids.get("CARD"),
        "email_entity_id": entity_ids.get("EMAIL"),
        "device_entity_id": entity_ids.get("DEVICE"),
        "ip_entity_id": entity_ids.get("IP"),
        "account_entity_id": entity_ids.get("ACCOUNT"),
        "ip_address": payload.get("ip_address"),
        "ip_country": payload.get("ip_country"),
        "billing_country": payload.get("billing_country"),
        "shipping_country": payload.get("shipping_country"),
        "avs_match": payload.get("avs_match"),
        "cvv_match": payload.get("cvv_match"),
        "three_ds_status": payload.get("three_ds_status"),
        "is_card_present": payload.get("is_card_present", False),
        "channel": payload.get("channel", "WEB"),
        "occurred_at": occurred_at,
    })

    thresholds = Thresholds(
        challenge_at=ruleset["challenge_at"],
        review_at=ruleset["review_at"],
        decline_at=ruleset["decline_at"],
    )
    evaluation = decide(rules, features, thresholds)

    latency_ms = int((time.perf_counter() - started) * 1000)
    decision_row = await dr.insert_decision(conn, {
        "transaction_id": txn["id"],
        "ruleset_id": ruleset["id"],
        "mode": "LIVE",
        "decision": evaluation.decision,
        "score": evaluation.score,
        "features": features,
        "triggered_rules": [h.as_dict() for h in evaluation.hits],
        "latency_ms": latency_ms,
        "exceeded_budget": latency_ms > settings.decision_latency_budget_ms,
    })

    # 4. A REVIEW decision is not finished -- somebody has to look at it, and
    #    an unreviewed order is an unshipped order.
    if evaluation.decision == "REVIEW":
        await dr.open_review_case(
            conn,
            decision_id=decision_row["id"],
            sla_due_at=now + timedelta(hours=REVIEW_SLA_HOURS),
        )

    # 5. Shadow evaluation: score the candidate ruleset on the SAME features
    #    and record what it WOULD have done. No extra queries, because the
    #    features are already computed -- so measuring a candidate costs
    #    almost nothing.
    shadow = await rr.find_ruleset_by_status(conn, merchant["id"], "SHADOW")
    if shadow is not None:
        shadow_rules = _to_rules(await rr.list_rules(conn, shadow["id"]))
        shadow_eval = decide(
            shadow_rules,
            features,
            Thresholds(shadow["challenge_at"], shadow["review_at"], shadow["decline_at"]),
        )
        await dr.insert_decision(conn, {
            "transaction_id": txn["id"],
            "ruleset_id": shadow["id"],
            "mode": "SHADOW",
            "decision": shadow_eval.decision,
            "score": shadow_eval.score,
            "features": features,
            "triggered_rules": [h.as_dict() for h in shadow_eval.hits],
            "latency_ms": 0,
            "exceeded_budget": False,
        })

    return {
        "decision_id": decision_row["id"],
        "transaction_id": txn["id"],
        "decision": evaluation.decision,
        "score": evaluation.score,
        "triggered_rules": [h.as_dict() for h in evaluation.hits],
        "features": features,
        "latency_ms": latency_ms,
        "idempotent_replay": False,
    }

"""Feature computation.

Turns a raw payment into the dict of named values the rules evaluate
against. This is where the latency budget is actually spent, so two
decisions shape it:

  1. Only compute what the ACTIVE ruleset references. A ruleset with no
     device rules should not pay for device link queries. `required`
     filters the work.

  2. Everything derived from the transaction itself is free -- no query.
     Only velocity, entity age and linking cost a round trip, and those are
     batched by entity.

Every returned value is JSON-serialisable, because the whole dict is frozen
into the decision record as the evidence of what the engine saw.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

from fraud_engine.repositories import entity_repository as er
from fraud_engine.repositories import velocity_repository as vr

# Which features need which query. Used to skip work a ruleset never asks
# for -- the single cheapest optimisation available in a decision engine.
_VELOCITY_CARD = {
    "velocity_card_1h",
    "velocity_card_24h",
    "velocity_amount_card_24h",
}
_LINK_FEATURES = {
    "accounts_per_card_30d",
    "cards_per_account_30d",
    "accounts_per_device_30d",
    "emails_per_device_30d",
}


def transaction_features(txn: dict[str, Any], bin_info: dict[str, Any] | None) -> dict[str, Any]:
    """Features readable straight off the payment. No database access."""
    billing = txn.get("billing_country")
    features: dict[str, Any] = {
        "amount_minor": txn["amount_minor"],
        "is_card_present": bool(txn.get("is_card_present", False)),
        "avs_match": txn.get("avs_match"),
        "cvv_match": txn.get("cvv_match"),
        "three_ds_status": txn.get("three_ds_status") or "NOT_USED",
        "is_prepaid_card": bool(bin_info["is_prepaid"]) if bin_info else False,
    }

    # Geography comparisons. Each returns None when the input is missing
    # rather than guessing False -- "we do not know" and "they do not match"
    # are different facts, and a rule should not fire on the first.
    ip_country = txn.get("ip_country")
    features["ip_billing_country_match"] = (
        (ip_country == billing) if (ip_country and billing) else None
    )

    issuer_country = bin_info.get("issuer_country") if bin_info else None
    features["bin_billing_country_match"] = (
        (issuer_country == billing) if (issuer_country and billing) else None
    )

    shipping = txn.get("shipping_country")
    features["shipping_billing_match"] = (
        (shipping == billing) if (shipping and billing) else None
    )

    return features


def _age_days(first_seen: datetime | None, now: datetime) -> int | None:
    if first_seen is None:
        return None
    return max(0, (now - first_seen).days)


async def compute_features(
    conn: AsyncConnection,
    *,
    txn: dict[str, Any],
    merchant_id: Any,
    entity_ids: dict[str, Any],
    bin_info: dict[str, Any] | None,
    required: set[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the full feature snapshot for one payment.

    `required` is the set of features the active ruleset actually
    references. Anything outside it is skipped, so adding an unused feature
    to the registry costs nothing at decision time.
    """
    now = now or datetime.now(UTC)
    one_hour = now - timedelta(hours=1)
    one_day = now - timedelta(days=1)
    thirty_days = now - timedelta(days=30)

    features = transaction_features(txn, bin_info)

    card_id = entity_ids.get("CARD")
    email_id = entity_ids.get("EMAIL")
    device_id = entity_ids.get("DEVICE")
    ip_id = entity_ids.get("IP")
    account_id = entity_ids.get("ACCOUNT")

    # --- velocity -------------------------------------------------------
    if card_id and (required & _VELOCITY_CARD):
        v = await vr.card_velocity_windows(
            conn, card_entity_id=card_id, now=now,
            one_hour_ago=one_hour, one_day_ago=one_day,
        )
        features["velocity_card_1h"] = v["count_1h"]
        features["velocity_card_24h"] = v["count_24h"]
        features["velocity_amount_card_24h"] = v["amount_24h"]

    if email_id and "velocity_email_24h" in required:
        v = await vr.entity_velocity(
            conn, entity_column="email_entity_id", entity_id=email_id,
            since=one_day, before=now,
        )
        features["velocity_email_24h"] = v["txn_count"]

    if device_id and "velocity_device_1h" in required:
        v = await vr.entity_velocity(
            conn, entity_column="device_entity_id", entity_id=device_id,
            since=one_hour, before=now,
        )
        features["velocity_device_1h"] = v["txn_count"]

    if ip_id and "velocity_ip_1h" in required:
        v = await vr.entity_velocity(
            conn, entity_column="ip_entity_id", entity_id=ip_id,
            since=one_hour, before=now,
        )
        features["velocity_ip_1h"] = v["txn_count"]

    if card_id and "declines_card_24h" in required:
        features["declines_card_24h"] = await vr.declines_for_card(
            conn, card_entity_id=card_id, since=one_day, before=now
        )

    # --- entity history -------------------------------------------------
    if card_id and ("card_age_days" in required or "card_seen_count" in required):
        e = await er.find_entity_by_id(conn, card_id)
        if e:
            features["card_age_days"] = _age_days(e["first_seen_at"], now)
            features["card_seen_count"] = e["seen_count"]

    if email_id and "email_age_days" in required:
        e = await er.find_entity_by_id(conn, email_id)
        if e:
            features["email_age_days"] = _age_days(e["first_seen_at"], now)

    if account_id and "account_age_days" in required:
        e = await er.find_entity_by_id(conn, account_id)
        if e:
            features["account_age_days"] = _age_days(e["first_seen_at"], now)

    # --- shared attributes ----------------------------------------------
    if required & _LINK_FEATURES:
        links = await vr.shared_attribute_counts(
            conn,
            card_entity_id=card_id if "accounts_per_card_30d" in required else None,
            account_entity_id=account_id if "cards_per_account_30d" in required else None,
            device_entity_id=device_id
            if required & {"accounts_per_device_30d", "emails_per_device_30d"}
            else None,
            since=thirty_days, before=now,
        )
        for key, value in links.items():
            if key in required:
                features[key] = value

    # --- lists ----------------------------------------------------------
    if required & {"on_deny_list", "on_allow_list", "on_watch_list"}:
        ids = [v for v in entity_ids.values() if v is not None]
        entries = await er.list_active_list_entries(conn, ids, merchant_id)
        kinds = {e["list_type"] for e in entries}
        features["on_deny_list"] = "DENY" in kinds
        features["on_allow_list"] = "ALLOW" in kinds
        features["on_watch_list"] = "WATCH" in kinds

    return features

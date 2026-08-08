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

from fraud_engine.lib.errors import ValidationError
from fraud_engine.repositories import entity_repository as er
from fraud_engine.repositories import reference_repository as rr
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
_LIST_FEATURES = {"on_deny_list", "on_allow_list", "on_watch_list"}

# Everything transaction_features() reads straight off the payment.
_TRANSACTION_FEATURES = {
    "amount_minor",
    "is_card_present",
    "avs_match",
    "cvv_match",
    "three_ds_status",
    "is_prepaid_card",
    "ip_billing_country_match",
    "bin_billing_country_match",
    "shipping_billing_match",
    "product_code",
    "card_type",
    "addr_match",
    "dist_from_billing",
    "has_identity_data",
}

_VELOCITY_ACCOUNT = {"velocity_account_1h", "velocity_account_24h"}

_ENTITY_FEATURES = {
    "card_age_days",
    "card_seen_count",
    "email_age_days",
    "account_age_days",
    "account_seen_count",
}

_VELOCITY_OTHER = {
    "velocity_email_24h",
    "velocity_device_1h",
    "velocity_ip_1h",
    "declines_card_24h",
}

# Features this engine derives for itself, from the payment or from its own
# history. Every one of these is produced by compute_features given the
# inputs it needs.
ENGINE_COMPUTED_FEATURES = frozenset(
    _TRANSACTION_FEATURES
    | _VELOCITY_CARD
    | _VELOCITY_ACCOUNT
    | _VELOCITY_OTHER
    | _ENTITY_FEATURES
    | _LINK_FEATURES
    | _LIST_FEATURES
)

# Features this engine CANNOT compute and never will: processor-supplied
# aggregates that arrive on the authorisation message already calculated.
# They are separated from ENGINE_COMPUTED_FEATURES rather than lumped in
# because the distinction is the whole reason the vesta_ prefix exists -- a
# reader of a fired rule must be able to tell what the engine counted from
# what it was handed. See migration 007 and CLAUDE.md.
SUPPLIED_ONLY_FEATURES = frozenset(
    {"vesta_c4", "vesta_c8", "vesta_c10", "vesta_c12", "vesta_d3", "vesta_d5"}
)

# The set a rule is allowed to reference. Anything outside it is a rule that
# can never fire: the condition evaluator treats a missing feature as
# no-match, so the rule scores zero forever and looks merely quiet.
COMPUTABLE_FEATURES = ENGINE_COMPUTED_FEATURES | SUPPLIED_ONLY_FEATURES


def _or_none(value: Any) -> str | None:
    """Normalise an absent categorical to None.

    Blank strings arrive from CSV sources where an empty cell is "" rather
    than NULL. Treating "" as a value creates a category that matches
    nothing and is invisible in a rule listing.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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

    # Categorical attributes the acquirer sends with the authorisation.
    #
    # Each is passed through unchanged and defaults to None, never to a
    # stand-in value. A rule testing `card_type eq "debit"` must not fire
    # because the field was missing and something guessed "credit" -- and,
    # more importantly, a rule testing for a MISMATCH must not fire on a
    # transaction where nothing was supplied to mismatch against.
    #
    # addr_match is the exception that proves the rule: its "(absent)" is a
    # real, measured category in the IEEE data rather than a missing value,
    # so it is mapped here explicitly rather than left as None. A rule must
    # be able to test for it -- as a NULL it would simply never match, which
    # is the failure this whole module has been bitten by four times.
    features["product_code"] = _or_none(txn.get("product_code"))
    features["card_type"] = _or_none(txn.get("card_type"))
    features["addr_match"] = _or_none(txn.get("addr_match")) or "(absent)"

    # A distance, so 0 is a legitimate value and must survive. `or None`
    # would turn a transaction at the billing address into "unknown".
    dist = txn.get("dist_from_billing")
    features["dist_from_billing"] = None if dist is None else float(dist)

    # Whether identity/device signals were captured AT ALL. Tri-state on
    # purpose: False means the join ran and found nothing, None means nobody
    # told us either way. Collapsing those into False would make every
    # caller that omits the field look like a transaction with no identity.
    identity = txn.get("has_identity_data")
    features["has_identity_data"] = None if identity is None else bool(identity)

    return features


def _age_days(first_seen: datetime | None, now: datetime) -> int | None:
    if first_seen is None:
        return None
    return max(0, (now - first_seen).days)


async def _merge_supplied(
    conn: AsyncConnection, features: dict[str, Any], supplied: dict[str, Any]
) -> None:
    """Merge processor-supplied values, rejecting anything unregistered.

    Validated against feature_definitions rather than against
    SUPPLIED_ONLY_FEATURES, because the registry is the single description of
    what a feature name means. A typo like `vesta_c40` must be an error at
    the edge: accepted silently it would sit in the frozen snapshot looking
    like evidence, while the rule that reads `vesta_c4` matched nothing.

    Costs one query, and only when something was actually supplied -- the
    ordinary path where no aggregates arrive pays nothing.
    """
    registered = await rr.list_feature_codes(conn)
    unknown = sorted(set(supplied) - registered)
    if unknown:
        raise ValidationError(
            f"Unregistered supplied feature(s): {', '.join(unknown)}. "
            f"Add them to feature_definitions in a migration first."
        )
    features.update(supplied)


async def compute_features(
    conn: AsyncConnection,
    *,
    txn: dict[str, Any],
    merchant_id: Any,
    entity_ids: dict[str, Any],
    bin_info: dict[str, Any] | None,
    required: set[str],
    now: datetime | None = None,
    supplied_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the full feature snapshot for one payment.

    `required` is the set of features the active ruleset actually
    references. Anything outside it is skipped, so adding an unused feature
    to the registry costs nothing at decision time.

    `supplied_features` carries aggregates the PROCESSOR computed and sent
    with the authorisation -- the vesta_ family. This engine cannot derive
    them and does not pretend to. They are merged BEFORE anything the engine
    computes for itself, so a caller can never overwrite a velocity counter
    or an entity age with a value of its own choosing.
    """
    now = now or datetime.now(UTC)
    one_hour = now - timedelta(hours=1)
    one_day = now - timedelta(days=1)
    thirty_days = now - timedelta(days=30)

    features: dict[str, Any] = {}
    if supplied_features:
        await _merge_supplied(conn, features, supplied_features)

    features.update(transaction_features(txn, bin_info))

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

    if account_id and (required & _VELOCITY_ACCOUNT):
        v = await vr.account_velocity_windows(
            conn, account_entity_id=account_id, now=now,
            one_hour_ago=one_hour, one_day_ago=one_day,
        )
        features["velocity_account_1h"] = v["count_1h"]
        features["velocity_account_24h"] = v["count_24h"]

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

    # One lookup serves both features. account_seen_count exists because
    # account_age_days cannot tell a first-ever transaction from a returning
    # account seen earlier the same day -- entities are upserted before
    # features are computed, so both read 0. seen_count reads 1 on the first
    # ever transaction and 2+ on a repeat, which is the distinguishing value.
    if account_id and required & {"account_age_days", "account_seen_count"}:
        e = await er.find_entity_by_id(conn, account_id)
        if e:
            features["account_age_days"] = _age_days(e["first_seen_at"], now)
            features["account_seen_count"] = e["seen_count"]

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

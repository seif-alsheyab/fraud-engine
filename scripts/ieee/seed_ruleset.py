"""Seed the 'ieee-banded' ruleset (version 10).

WHY THIS RULESET AND NOT THE ONE IN THE PLAN
-------------------------------------------
The ruleset originally drafted in section 5 of the backtest plan was built,
seeded and measured. It failed, and the failure is worth recording because it
is the reason this file looks the way it does:

  * PR-AUC 0.091 on held-out data.
  * Score was NOT monotonic in fraud rate -- a higher score did not reliably
    mean a higher chance of fraud, which makes every threshold arbitrary.
  * LINK_DEVICE_ACCOUNTS contributed zero unique frauds: every transaction it
    caught was already caught by another rule, so its weight bought nothing.

This ruleset replaces it and scores PR-AUC 0.2042 on the same held-out data.
That is roughly 2.2x the original and still a modest number in absolute terms;
it is reported here rather than rounded up because the point of the exercise
is a measured baseline, not a good-looking one.

HOW THE BANDED WEIGHTS WERE SET
-------------------------------
Each banded weight is round(10 * log2(lift)) measured on a held-out FIT period
that is disjoint from the period used to report the PR-AUC above. Taking log2
of lift means a band that doubles the fraud rate earns ~10 points, one that
quadruples it earns ~20, and a band with no lift earns 0. That keeps a very
rare, very predictive band from dominating the score outright.

The weights below are transcribed from that measurement. They are NOT to be
re-derived, rounded, prettified or nudged for symmetry. A weight that looks
untidy next to its neighbours (C12's 13 where every other first band is 15 or
16) is untidy because the data was.

BANDS ARE CUMULATIVE, NOT EXCLUSIVE
-----------------------------------
Each band is a separate rule with a plain threshold, so a transaction with
C4 = 5 fires all three C4 bands and scores 15 + 28 + 37 = 80. This is
deliberate and it is what the thresholds were fitted against: the maximum
attainable score is 494, and decline_at is 357. Had the bands been made
mutually exclusive the ceiling would fall to 319 and DECLINE would be
unreachable -- the ruleset would never decline anything, at any score.

The one genuinely exclusive rule is MED_AMOUNT, which is bracketed so it does
not stack with HIGH_AMOUNT.

KNOWN GAP
---------
velocity_account_1h and velocity_account_24h are registered in
feature_definitions (migration 007) but nothing computes them yet --
feature_service has card, email, device and IP windows and no account window.
VEL_ACCOUNT_1H and VEL_ACCOUNT_24H are therefore seeded and inert: a missing
feature is treated as no-match, so they score zero rather than erroring. They
will start contributing when the account velocity window lands.
"""

import argparse
import asyncio
import sys
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from fraud_engine.db.pool import close_pool, connection, open_pool
from fraud_engine.domain.conditions import validate_condition
from fraud_engine.lib.errors import RuleDefinitionError
from fraud_engine.repositories import reference_repository as rr

MERCHANT_CODE = "IEEE"
MERCHANT_NAME = "IEEE-CIS backtest"

RULESET_NAME = "ieee-banded"
RULESET_VERSION = 10
RULESET_STATUS = "ACTIVE"

# Fitted alongside the weights. Changing one without re-fitting the other
# invalidates both.
CHALLENGE_AT = 147
REVIEW_AT = 232
DECLINE_AT = 357

# Rules the engine computes for itself, from the transaction and from its own
# velocity and link queries.
ENGINE_RULES: list[tuple[str, str, dict[str, Any], int]] = [
    (
        "PRODUCT_NOT_W",
        "Product code is not W",
        {"feature": "product_code", "op": "ne", "value": "W"},
        40,
    ),
    (
        "LINK_DEVICE_ACCOUNTS",
        "Four or more accounts on this device in 30 days",
        {"feature": "accounts_per_device_30d", "op": "gte", "value": 4},
        20,
    ),
    (
        "NEW_ACCOUNT_BURST",
        "Account new today and already transacting again",
        # The age clause alone measured lift 1.08 -- indistinguishable from
        # the base rate, i.e. no signal. The reason is that age cannot tell a
        # first-ever transaction from a returning account first seen earlier
        # the same day: entities are upserted before features are computed, so
        # both read 0.
        #
        # seen_count is what separates them -- 1 on a first-ever transaction,
        # 2+ on a repeat. Requiring 2 restricts the rule to accounts that
        # appeared today AND are already back, which measured lift 3.05.
        {
            "all": [
                {"feature": "account_age_days", "op": "lte", "value": 0},
                {"feature": "account_seen_count", "op": "gte", "value": 2},
            ]
        },
        35,
    ),
    (
        "VEL_ACCOUNT_1H",
        "Account used twice or more in an hour",
        {"feature": "velocity_account_1h", "op": "gte", "value": 2},
        30,
    ),
    (
        "VEL_ACCOUNT_24H",
        "Account used five times or more in a day",
        {"feature": "velocity_account_24h", "op": "gte", "value": 5},
        15,
    ),
    (
        "HIGH_AMOUNT",
        "Amount at or above 500",
        {"feature": "amount_minor", "op": "gte", "value": 50000},
        25,
    ),
    (
        "MED_AMOUNT",
        "Amount between 300 and 500",
        # Bracketed so it does not stack on top of HIGH_AMOUNT. The rule
        # language has no 'lt', only 'lte' -- and it does not need one here:
        # amount_minor is an integer count of minor units, so lte 49999 is
        # exactly lt 50000 with no values in between to lose.
        {
            "all": [
                {"feature": "amount_minor", "op": "gte", "value": 30000},
                {"feature": "amount_minor", "op": "lte", "value": 49999},
            ]
        },
        15,
    ),
    (
        "M4_ABSENT",
        "Address match result absent",
        # PROTECTIVE, hence the negative weight. A missing M4 turns out to be
        # commonest on the ordinary traffic in this dataset, so treating its
        # absence as suspicious -- the intuitive reading -- scores the wrong
        # population. The engine must be able to become less suspicious, not
        # only more.
        {"feature": "addr_match", "op": "eq", "value": "(absent)"},
        -25,
    ),
]

# Banded rules over the processor-supplied Vesta columns.
#
# The C columns are counts: HIGHER is worse, so the bands ascend with gte.
# (feature, label, [(threshold, weight), ...])
ASCENDING_BANDS: list[tuple[str, str, list[tuple[int, int]]]] = [
    ("vesta_c4", "C4", [(1, 15), (2, 28), (4, 37)]),
    ("vesta_c8", "C8", [(1, 16), (3, 29), (8, 35)]),
    ("vesta_c10", "C10", [(1, 16), (3, 27), (10, 33)]),
    ("vesta_c12", "C12", [(1, 13), (2, 26), (4, 36)]),
]

# The D columns are timedeltas in days: SMALLER is worse (more recent), so the
# bands descend with lte. D5 stacks -- a value of 3 fires both bands for 13.
DESCENDING_BANDS: list[tuple[str, str, list[tuple[int, int]]]] = [
    ("vesta_d3", "D3", [(8, 5)]),
    ("vesta_d5", "D5", [(9, 8), (32, 5)]),
]


def build_rules() -> list[tuple[str, str, dict[str, Any], int]]:
    """Every rule in the ruleset, as (code, name, condition, weight)."""
    rules = list(ENGINE_RULES)

    for feature, label, bands in ASCENDING_BANDS:
        for threshold, weight in bands:
            rules.append(
                (
                    f"VESTA_{label}_GTE_{threshold}",
                    f"Vesta {label} at or above {threshold}",
                    {"feature": feature, "op": "gte", "value": threshold},
                    weight,
                )
            )

    for feature, label, bands in DESCENDING_BANDS:
        for threshold, weight in bands:
            rules.append(
                (
                    f"VESTA_{label}_LTE_{threshold}",
                    f"Vesta {label} at or below {threshold}",
                    {"feature": feature, "op": "lte", "value": threshold},
                    weight,
                )
            )

    return rules


def validate_rules(
    rules: list[tuple[str, str, dict[str, Any], int]], known_features: set[str]
) -> None:
    """Check every condition against the feature registry BEFORE any insert.

    This is the whole reason feature_definitions exists. A rule naming a
    feature nobody registered is not a rule that misbehaves at 3am -- it is a
    rule that never fires at all, looks quiet, and is mistaken for working.
    Refusing it here converts that silence into a startup failure.

    Validation runs over the complete set first so a bad rule cannot leave a
    half-seeded ruleset behind.
    """
    seen: set[str] = set()
    for code, _name, condition, _weight in rules:
        if code in seen:
            raise RuleDefinitionError(f"Duplicate rule code '{code}'.")
        seen.add(code)
        try:
            validate_condition(condition, known_features, path=code)
        except RuleDefinitionError as exc:
            raise RuleDefinitionError(f"Rule '{code}' is invalid: {exc.message}") from exc


async def _upsert_merchant(conn: AsyncConnection, code: str) -> dict[str, Any]:
    await conn.execute(
        """
        INSERT INTO merchants (code, name, vertical, country, currency)
        VALUES (%s, %s, 'ECOMMERCE', 'US', 'USD')
        ON CONFLICT (code) DO NOTHING
        """,
        (code, MERCHANT_NAME),
    )
    merchant = await rr.find_merchant_by_code(conn, code)
    assert merchant is not None
    return merchant


async def seed(
    conn: AsyncConnection,
    merchant_code: str = MERCHANT_CODE,
    rules: list[tuple[str, str, dict[str, Any], int]] | None = None,
) -> dict[str, Any]:
    """Create or replace the ieee-banded ruleset. Safe to run repeatedly.

    Re-running replaces the rules of version 10 rather than appending to them,
    so a seed after an edit leaves exactly the rules in this file and no
    orphans from a previous shape of it.
    """
    rules = build_rules() if rules is None else rules

    known = await rr.list_feature_codes(conn)
    validate_rules(rules, known)

    merchant = await _upsert_merchant(conn, merchant_code)

    # Only one ACTIVE ruleset per merchant is allowed, enforced by a partial
    # unique index. Retire any other version that currently holds the slot,
    # otherwise the insert below fails on a constraint rather than on
    # anything a reader would recognise as an activation conflict.
    await conn.execute(
        """
        UPDATE rulesets SET status = 'RETIRED', retired_at = now()
         WHERE merchant_id = %s AND status = 'ACTIVE' AND version <> %s
        """,
        (merchant["id"], RULESET_VERSION),
    )

    cur = await conn.execute(
        """
        INSERT INTO rulesets (merchant_id, version, name, description, status,
                              challenge_at, review_at, decline_at,
                              created_by, activated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'scripts/ieee/seed_ruleset.py', now())
        ON CONFLICT (merchant_id, version) DO UPDATE
           SET name         = EXCLUDED.name,
               description  = EXCLUDED.description,
               status       = EXCLUDED.status,
               challenge_at = EXCLUDED.challenge_at,
               review_at    = EXCLUDED.review_at,
               decline_at   = EXCLUDED.decline_at,
               activated_at = COALESCE(rulesets.activated_at, EXCLUDED.activated_at),
               retired_at   = NULL
        RETURNING *
        """,
        (
            merchant["id"],
            RULESET_VERSION,
            RULESET_NAME,
            "Banded ruleset fitted on IEEE-CIS. PR-AUC 0.2042 on held-out data. "
            "Band weights are round(10*log2(lift)) from a disjoint fit period.",
            RULESET_STATUS,
            CHALLENGE_AT,
            REVIEW_AT,
            DECLINE_AT,
        ),
    )
    ruleset = await cur.fetchone()
    assert ruleset is not None

    await conn.execute("DELETE FROM rules WHERE ruleset_id = %s", (ruleset["id"],))
    for code, name, condition, weight in rules:
        await conn.execute(
            """
            INSERT INTO rules (ruleset_id, code, name, condition, weight)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (ruleset["id"], code, name, Jsonb(condition), weight),
        )

    return {"merchant": merchant, "ruleset": ruleset, "rule_count": len(rules)}


async def run(merchant_code: str) -> None:
    await open_pool()
    try:
        async with connection() as conn, conn.transaction():
            result = await seed(conn, merchant_code)

        # HIGH_AMOUNT and MED_AMOUNT are bracketed apart, so the naive sum of
        # positive weights overstates the ceiling by the lesser of the two.
        positive = sum(w for *_, w in build_rules() if w > 0)

        print(f"ruleset   : {RULESET_NAME} v{RULESET_VERSION} ({RULESET_STATUS})")
        print(f"merchant  : {result['merchant']['code']} ({result['merchant']['id']})")
        print(f"ruleset id: {result['ruleset']['id']}")
        print(f"rules     : {result['rule_count']}")
        print(f"thresholds: challenge {CHALLENGE_AT}  review {REVIEW_AT}  decline {DECLINE_AT}")
        print(f"max score : {positive - 15} attainable ({positive} before the MED/HIGH bracket)")
    finally:
        await close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the ieee-banded ruleset.")
    ap.add_argument(
        "--merchant-code",
        default=MERCHANT_CODE,
        help=f"Merchant to attach the ruleset to (default: {MERCHANT_CODE}).",
    )
    args = ap.parse_args()

    try:
        asyncio.run(run(args.merchant_code))
    except RuleDefinitionError as exc:
        # A rejected rule is the script working, not failing. Say so plainly
        # rather than printing a traceback that reads like a crash.
        print(f"\nSEED REJECTED\n{exc.message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

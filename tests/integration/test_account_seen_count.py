"""account_seen_count, and the rule it makes expressible.

These tests go through decide_payment rather than calling compute_features
directly, and that is the point. The defect being fixed here is an ORDERING
one: decision_service upserts entities before it computes features, so on a
first-ever transaction the account entity already exists and account_age_days
reads 0 -- identical to an account first seen three hours earlier.

A test that called compute_features against a hand-built entity row could
choose its own ordering and would prove nothing about the pipeline. Going
through decide_payment means the ordering under test is the real one.
"""

from datetime import UTC, datetime, timedelta

from fraud_engine.services.decision_service import decide_payment
from scripts.ieee.seed_ruleset import build_rules
from tests.helpers.db import rollback_conn, seed_merchant, seed_rule, seed_ruleset

NOW = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)

# The condition as actually shipped, not a copy of it. If the seeder's rule is
# edited, these tests follow it rather than silently testing a stale shape.
NEW_ACCOUNT_BURST = next(
    (code, name, cond, weight)
    for code, name, cond, weight in build_rules()
    if code == "NEW_ACCOUNT_BURST"
)


async def _ruleset_with_only_that_rule(conn, merchant_id):
    """One rule, so the score is unambiguous evidence about that rule.

    With other rules present a score of 35 could come from anywhere, and the
    test would pass for the wrong reason.
    """
    rs = await seed_ruleset(conn, merchant_id, challenge_at=10, review_at=60, decline_at=80)
    code, name, condition, weight = NEW_ACCOUNT_BURST
    await seed_rule(conn, rs["id"], code=code, name=name, condition=condition, weight=weight)
    return rs


def _codes(result) -> set[str]:
    """triggered_rules holds dicts, not codes.

    Asserting `"X" not in result["triggered_rules"]` against a list of dicts
    is always true, so a negative assertion written that way passes whether
    the rule fired or not. Extract the codes explicitly.
    """
    return {r["code"] for r in result["triggered_rules"]}


def _payload(**over):
    base = {
        "merchant_code": None,
        "external_id": "acct-seen-1",
        "amount_minor": 25000,
        "currency": "USD",
        "card_number": "4111111111111111",
        "email": "buyer@example.com",
        "device_fingerprint": "device-abc",
        "ip_address": "203.0.113.9",
        "account_id": "acct-new",
        "cvv_match": True,
        "three_ds_status": "NOT_USED",
        "billing_country": "JO",
        "ip_country": "JO",
    }
    base.update(over)
    return base


class TestAccountSeenCount:
    async def test_a_first_ever_transaction_counts_one_and_does_not_fire(self):
        """The whole reason the feature exists.

        account_age_days is 0 here -- the entity was upserted moments ago with
        this transaction's own occurred_at. Age alone cannot tell this apart
        from a returning account, which is why the age-only rule measured lift
        1.08. seen_count is 1, so the rule correctly stays silent.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_only_that_rule(conn, m["id"])

            result = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="first-ever"),
                now=NOW,
            )

            assert result["features"]["account_seen_count"] == 1
            # Age is 0 on a first-ever transaction, so the age clause on its
            # own WOULD have fired. The count clause is what holds it back.
            assert result["features"]["account_age_days"] == 0
            assert "NEW_ACCOUNT_BURST" not in _codes(result)
            assert result["score"] == 0

    async def test_a_second_transaction_the_same_day_fires(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_only_that_rule(conn, m["id"])

            await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="burst-1"),
                now=NOW,
            )
            second = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="burst-2"),
                # Three hours later: same calendar day, so age is still 0 and
                # only the count separates this from the first transaction.
                now=NOW + timedelta(hours=3),
            )

            assert second["features"]["account_seen_count"] >= 2
            assert second["features"]["account_age_days"] == 0
            assert "NEW_ACCOUNT_BURST" in _codes(second)
            assert second["score"] == 35

    async def test_a_different_account_starts_over_at_one(self):
        """seen_count must be per entity, not a global counter."""
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_only_that_rule(conn, m["id"])

            await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="acct-a-1", account_id="acct-a"),
                now=NOW,
            )
            other = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="acct-b-1", account_id="acct-b"),
                now=NOW + timedelta(hours=1),
            )

            assert other["features"]["account_seen_count"] == 1
            assert "NEW_ACCOUNT_BURST" not in _codes(other)

    async def test_an_established_account_does_not_fire_however_often_it_returns(self):
        """The age clause still does work.

        seen_count alone would fire on every loyal repeat customer. Both
        clauses are load-bearing, so a change that dropped either one must
        fail a test.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_only_that_rule(conn, m["id"])

            await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="old-1", account_id="acct-old"),
                now=NOW,
            )
            later = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="old-2", account_id="acct-old"),
                # 40 days on: seen_count is 2, but the account is no longer new.
                now=NOW + timedelta(days=40),
            )

            assert later["features"]["account_seen_count"] >= 2
            assert later["features"]["account_age_days"] == 40
            assert "NEW_ACCOUNT_BURST" not in _codes(later)

    async def test_the_count_is_frozen_into_the_stored_snapshot(self):
        """A decision must remain explainable six weeks later.

        seen_count changes on every sighting, so if it were not captured at
        decision time there would be no way to show why the rule fired.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_only_that_rule(conn, m["id"])

            from fraud_engine.repositories import decision_repository as dr

            await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="frozen-1", account_id="acct-f"),
                now=NOW,
            )
            second = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="frozen-2", account_id="acct-f"),
                now=NOW + timedelta(hours=2),
            )

            stored = await dr.find_decision(conn, second["decision_id"])
            assert stored["features"]["account_seen_count"] >= 2

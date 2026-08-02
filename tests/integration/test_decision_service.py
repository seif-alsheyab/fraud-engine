from datetime import UTC, datetime, timedelta

import pytest

from fraud_engine.lib.errors import NoActiveRulesetError, NotFoundError
from fraud_engine.repositories import decision_repository as dr
from fraud_engine.repositories import entity_repository as er
from fraud_engine.services.decision_service import decide_payment
from tests.helpers.db import rollback_conn, seed_merchant, seed_rule, seed_ruleset

NOW = datetime.now(UTC)


async def _ruleset_with_standard_rules(conn, merchant_id):
    rs = await seed_ruleset(conn, merchant_id, challenge_at=40, review_at=60, decline_at=80)
    await seed_rule(conn, rs["id"], code="VEL_CARD_1H", name="Card 4+ in 1h",
                    condition={"feature": "velocity_card_1h", "op": "gte", "value": 4},
                    weight=35)
    await seed_rule(conn, rs["id"], code="NO_CVV", name="CVV mismatch",
                    condition={"feature": "cvv_match", "op": "eq", "value": False},
                    weight=25)
    await seed_rule(conn, rs["id"], code="THREE_DS", name="3DS authenticated",
                    condition={"feature": "three_ds_status", "op": "eq",
                               "value": "AUTHENTICATED"},
                    weight=-30)
    await seed_rule(conn, rs["id"], code="DENY", name="Deny list",
                    condition={"feature": "on_deny_list", "op": "eq", "value": True},
                    weight=0, hard_action="DECLINE")
    await seed_rule(conn, rs["id"], code="LINK_CARD", name="Card on many accounts",
                    condition={"feature": "accounts_per_card_30d", "op": "gte", "value": 3},
                    weight=45)
    return rs


def _payload(**over):
    base = {
        "merchant_code": None,
        "external_id": f"ord-{datetime.now(UTC).timestamp()}",
        "amount_minor": 25000,
        "currency": "USD",
        "card_number": "4111111111111111",
        "email": "buyer@example.com",
        "device_fingerprint": "device-abc",
        "ip_address": "203.0.113.9",
        "account_id": "acct-1",
        "cvv_match": True,
        "three_ds_status": "NOT_USED",
        "billing_country": "JO",
        "ip_country": "JO",
    }
    base.update(over)
    return base


class TestHappyPath:
    async def test_a_clean_payment_is_approved_and_recorded(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            result = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="ord-1"), now=NOW
            )
            assert result["decision"] == "APPROVE"
            assert result["score"] == 0
            assert result["idempotent_replay"] is False

            stored = await dr.find_decision(conn, result["decision_id"])
            assert stored["decision"] == "APPROVE"
            # The snapshot must be present and structured, not a string.
            assert stored["features"]["amount_minor"] == 25000

    async def test_the_feature_snapshot_is_frozen_into_the_decision(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            result = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="ord-2"), now=NOW
            )
            stored = await dr.find_decision(conn, result["decision_id"])
            # Six weeks from now, this is the only record of what the engine
            # actually saw. Re-running the rules today would answer a
            # different question with different velocity counters.
            assert "velocity_card_1h" in stored["features"]
            assert "on_deny_list" in stored["features"]
            assert stored["features"]["three_ds_status"] == "NOT_USED"


class TestIdempotency:
    async def test_a_retry_returns_the_original_decision(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            p = _payload(merchant_code=m["code"], external_id="ord-retry")
            first = await decide_payment(conn, p, now=NOW)
            second = await decide_payment(conn, p, now=NOW)

            assert second["idempotent_replay"] is True
            assert second["decision_id"] == first["decision_id"]
            assert second["decision"] == first["decision"]

    async def test_a_retry_does_not_create_a_second_transaction(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            p = _payload(merchant_code=m["code"], external_id="ord-once")
            await decide_payment(conn, p, now=NOW)
            await decide_payment(conn, p, now=NOW)
            cur = await conn.execute(
                "SELECT count(*)::int AS n FROM transactions WHERE external_id = %s",
                ("ord-once",),
            )
            row = await cur.fetchone()
            # A gateway timeout must not double a customer's velocity.
            assert row["n"] == 1


class TestVelocity:
    async def test_card_testing_burst_raises_the_score(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])

            # Five payments on the same card within the hour.
            for i in range(5):
                await decide_payment(
                    conn,
                    _payload(merchant_code=m["code"], external_id=f"burst-{i}",
                             cvv_match=False),
                    now=NOW - timedelta(minutes=50 - i * 5),
                )

            final = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="burst-final",
                         cvv_match=False),
                now=NOW,
            )
            codes = {r["code"] for r in final["triggered_rules"]}
            assert "VEL_CARD_1H" in codes
            assert final["features"]["velocity_card_1h"] >= 4
            assert final["decision"] in {"REVIEW", "DECLINE"}

    async def test_a_first_payment_does_not_count_itself(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            result = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="first"), now=NOW
            )
            # Without the `before` bound every card would show velocity 1 on
            # its very first use.
            assert result["features"]["velocity_card_1h"] == 0


class TestSharedAttributes:
    async def test_one_card_across_many_accounts_is_detected(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])

            # Four different accounts, four different emails, ONE card.
            # Each payment is individually unremarkable.
            for i in range(4):
                await decide_payment(
                    conn,
                    _payload(merchant_code=m["code"], external_id=f"ring-{i}",
                             account_id=f"acct-{i}", email=f"buyer{i}@example.com",
                             device_fingerprint=f"dev-{i}"),
                    now=NOW - timedelta(days=3, minutes=i),
                )

            result = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="ring-final",
                         account_id="acct-9", email="buyer9@example.com",
                         device_fingerprint="dev-9"),
                now=NOW,
            )
            codes = {r["code"] for r in result["triggered_rules"]}
            # The pattern exists only ACROSS rows: no single payment shows it.
            assert "LINK_CARD" in codes
            assert result["features"]["accounts_per_card_30d"] >= 3


class TestHardActions:
    async def test_a_deny_listed_card_is_declined_despite_3ds(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])

            # Seed the card entity by deciding once, then deny-list it.
            seed = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="pre-deny"), now=NOW
            )
            cur = await conn.execute(
                "SELECT card_entity_id FROM transactions WHERE id = %s",
                (seed["transaction_id"],),
            )
            card_id = (await cur.fetchone())["card_entity_id"]
            await er.add_list_entry(
                conn, list_type="DENY", entity_id=card_id, merchant_id=None,
                reason="confirmed fraud", added_by="test",
            )

            result = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="post-deny",
                         three_ds_status="AUTHENTICATED"),
                now=NOW,
            )
            # 3DS shifts liability and normally pulls the score down. A deny
            # entry is newer information about the same card and must win.
            assert result["decision"] == "DECLINE"
            assert result["features"]["on_deny_list"] is True


class TestReviewQueue:
    async def test_a_review_decision_opens_a_case_with_an_sla(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"], challenge_at=10, review_at=20,
                                    decline_at=90)
            await seed_rule(conn, rs["id"], code="ALWAYS", name="always",
                            condition={"feature": "amount_minor", "op": "gte", "value": 1},
                            weight=25)
            result = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="rev-1"), now=NOW
            )
            assert result["decision"] == "REVIEW"
            queue = await dr.list_open_review_cases(conn)
            # An unreviewed order is an unshipped order, so the case carries
            # its own deadline.
            assert len(queue) == 1
            assert queue[0]["sla_due_at"] > NOW


class TestShadowMode:
    async def test_a_shadow_ruleset_is_scored_but_not_applied(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])

            # Candidate: same rule, much harsher threshold.
            shadow = await seed_ruleset(conn, m["id"], version=2, status="SHADOW",
                                        challenge_at=5, review_at=10, decline_at=15)
            await seed_rule(conn, shadow["id"], code="STRICT", name="strict",
                            condition={"feature": "amount_minor", "op": "gte", "value": 1},
                            weight=50)

            result = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="shadow-1"), now=NOW
            )
            # The live answer is unaffected...
            assert result["decision"] == "APPROVE"

            cur = await conn.execute(
                "SELECT mode, decision FROM decisions WHERE transaction_id = %s "
                "ORDER BY mode",
                (result["transaction_id"],),
            )
            rows = await cur.fetchall()
            modes = {r["mode"]: r["decision"] for r in rows}
            # ...but what the candidate WOULD have done is recorded, on the
            # same features, at no extra query cost.
            assert modes["LIVE"] == "APPROVE"
            assert modes["SHADOW"] == "DECLINE"


class TestFailureModes:
    async def test_an_unknown_merchant_is_rejected(self):
        async with rollback_conn() as conn:
            with pytest.raises(NotFoundError):
                await decide_payment(conn, _payload(merchant_code="NOPE"), now=NOW)

    async def test_a_merchant_with_no_active_ruleset_errors_rather_than_approving(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await seed_ruleset(conn, m["id"], status="DRAFT")
            # Silently approving because configuration is missing is the
            # single most expensive failure mode this system can have.
            with pytest.raises(NoActiveRulesetError):
                await decide_payment(
                    conn, _payload(merchant_code=m["code"], external_id="no-rs"), now=NOW
                )


class TestPrivacy:
    async def test_the_raw_card_number_is_never_stored(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            pan = "4111111111111111"
            result = await decide_payment(
                conn,
                _payload(merchant_code=m["code"], external_id="privacy-1", card_number=pan),
                now=NOW,
            )
            cur = await conn.execute(
                """
                SELECT t.card_last4, e.value_hash, e.display_hint
                  FROM transactions t
                  JOIN entities e ON e.id = t.card_entity_id
                 WHERE t.id = %s
                """,
                (result["transaction_id"],),
            )
            row = await cur.fetchone()
            assert row["card_last4"] == "1111"
            assert pan not in row["value_hash"]
            assert len(row["value_hash"]) == 64


class TestLatency:
    async def test_latency_is_measured_and_recorded(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await _ruleset_with_standard_rules(conn, m["id"])
            result = await decide_payment(
                conn, _payload(merchant_code=m["code"], external_id="lat-1"), now=NOW
            )
            stored = await dr.find_decision(conn, result["decision_id"])
            assert stored["latency_ms"] >= 0
            assert stored["exceeded_budget"] is False


class TestOmittedOptionalFields:
    """A caller who omits optional fields must still get a decision.

    Every other test in this file sends a full payload, which is exactly how
    a NOT NULL violation on `currency` survived 130 green tests and appeared
    only when a real client called the live server. Tests that mirror the
    author's assumptions test the assumptions, not the contract.
    """

    async def test_a_payload_with_only_required_fields_succeeds(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn, currency="USD")
            await _ruleset_with_standard_rules(conn, m["id"])
            result = await decide_payment(conn, {
                "merchant_code": m["code"],
                "external_id": "minimal-1",
                "amount_minor": 25000,
            }, now=NOW)
            assert result["decision"] in {"APPROVE", "CHALLENGE", "REVIEW", "DECLINE"}

    async def test_currency_present_but_null_falls_back_to_the_merchant(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn, currency="USD")
            await _ruleset_with_standard_rules(conn, m["id"])
            result = await decide_payment(conn, {
                "merchant_code": m["code"],
                "external_id": "no-currency-1",
                "amount_minor": 25000,
                "currency": None,      # exactly what Pydantic emits
            }, now=NOW)
            cur = await conn.execute(
                "SELECT currency FROM transactions WHERE id = %s",
                (result["transaction_id"],),
            )
            assert (await cur.fetchone())["currency"] == "USD"

    async def test_the_exact_dict_pydantic_produces_is_accepted(self):
        """Guards the real boundary: what model_dump() actually emits.

        Hand-written test dicts drift from the API contract. This payload is
        built by the request model itself, so it cannot.
        """
        from fraud_engine.api.schemas import DecisionRequest

        async with rollback_conn() as conn:
            m = await seed_merchant(conn, currency="EUR")
            await _ruleset_with_standard_rules(conn, m["id"])

            request = DecisionRequest(
                merchant_code=m["code"],
                external_id="pydantic-dump-1",
                amount_minor=25000,
                card_number="4111111111111111",
            )
            result = await decide_payment(conn, request.model_dump(), now=NOW)

            cur = await conn.execute(
                "SELECT currency, is_card_present, channel FROM transactions WHERE id = %s",
                (result["transaction_id"],),
            )
            row = await cur.fetchone()
            assert row["currency"] == "EUR"
            assert row["is_card_present"] is False
            assert row["channel"] == "WEB"

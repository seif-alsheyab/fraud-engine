from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fraud_engine.repositories import (
    decision_repository as dr,
)
from fraud_engine.repositories import (
    entity_repository as er,
)
from fraud_engine.repositories import (
    reference_repository as rr,
)
from fraud_engine.repositories import (
    velocity_repository as vr,
)
from tests.helpers.db import (
    rollback_conn,
    seed_entity,
    seed_merchant,
    seed_rule,
    seed_ruleset,
    seed_transaction,
)

NOW = datetime.now(UTC)


class TestReferenceRepository:
    async def test_feature_registry_is_loaded(self):
        async with rollback_conn() as conn:
            codes = await rr.list_feature_codes(conn)
            # 27 from migration 006, the 13 IEEE-era features in 007, and
            # account_seen_count in 008. Updated deliberately per migration:
            # this assertion exists so a feature cannot appear in the registry
            # without a migration behind it.
            assert len(codes) == 41
            assert "account_seen_count" in codes
            assert "velocity_card_1h" in codes
            assert "accounts_per_card_30d" in codes
            assert "vesta_c4" in codes

    async def test_finds_the_active_ruleset(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await seed_ruleset(conn, m["id"], version=1, status="RETIRED")
            active = await seed_ruleset(conn, m["id"], version=2, status="ACTIVE")
            found = await rr.find_ruleset_by_status(conn, m["id"], "ACTIVE")
            assert found["id"] == active["id"]
            assert found["version"] == 2

    async def test_returns_none_when_no_active_ruleset_exists(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            await seed_ruleset(conn, m["id"], status="DRAFT")
            assert await rr.find_ruleset_by_status(conn, m["id"], "ACTIVE") is None

    async def test_lists_only_enabled_rules_in_stable_order(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            await seed_rule(conn, rs["id"], code="B_RULE")
            await seed_rule(conn, rs["id"], code="A_RULE")
            await seed_rule(conn, rs["id"], code="C_OFF", is_enabled=False)
            rules = await rr.list_rules(conn, rs["id"])
            # Stable order matters: two evaluations of one ruleset must
            # produce the triggered_rules list in the same sequence, or a
            # backtest diff is meaningless.
            assert [r["code"] for r in rules] == ["A_RULE", "B_RULE"]

    async def test_condition_round_trips_as_a_dict_not_a_string(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            cond = {"all": [{"feature": "velocity_card_1h", "op": "gte", "value": 4}]}
            await seed_rule(conn, rs["id"], condition=cond)
            rules = await rr.list_rules(conn, rs["id"])
            assert rules[0]["condition"] == cond
            assert isinstance(rules[0]["condition"], dict)


class TestEntityRepository:
    async def test_first_sighting_creates_the_entity(self):
        async with rollback_conn() as conn:
            e = await er.upsert_entity(conn, "CARD", uuid4().hex, "4242", NOW)
            assert e["seen_count"] == 1

    async def test_repeat_sighting_increments_without_resetting_age(self):
        async with rollback_conn() as conn:
            h = uuid4().hex
            first_seen = NOW - timedelta(days=90)
            a = await er.upsert_entity(conn, "CARD", h, "4242", first_seen)
            b = await er.upsert_entity(conn, "CARD", h, "4242", NOW)
            assert b["id"] == a["id"]
            assert b["seen_count"] == 2
            # Entity age is a signal. Overwriting first_seen_at on every
            # sighting would make every card permanently brand new.
            assert b["first_seen_at"] == a["first_seen_at"] == first_seen
            assert b["last_seen_at"] == NOW

    async def test_the_same_value_on_different_types_is_different_entities(self):
        async with rollback_conn() as conn:
            h = uuid4().hex
            a = await er.upsert_entity(conn, "EMAIL", h, None, NOW)
            b = await er.upsert_entity(conn, "DEVICE", h, None, NOW)
            assert a["id"] != b["id"]

    async def test_expired_list_entries_stop_applying(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            e = await seed_entity(conn, "IP")
            await er.add_list_entry(
                conn, list_type="DENY", entity_id=e["id"], merchant_id=m["id"],
                reason="expired block", added_by="test",
                expires_at=NOW - timedelta(days=1),
            )
            entries = await er.list_active_list_entries(conn, [e["id"]], m["id"])
            # IP addresses are reassigned. A permanent block means a stranger
            # inherits someone else's punishment.
            assert entries == []

    async def test_a_global_entry_applies_to_every_merchant(self):
        async with rollback_conn() as conn:
            m1 = await seed_merchant(conn)
            m2 = await seed_merchant(conn)
            e = await seed_entity(conn, "CARD")
            await er.add_list_entry(
                conn, list_type="DENY", entity_id=e["id"], merchant_id=None,
                reason="confirmed fraud", added_by="test",
            )
            assert len(await er.list_active_list_entries(conn, [e["id"]], m1["id"])) == 1
            assert len(await er.list_active_list_entries(conn, [e["id"]], m2["id"])) == 1

    async def test_a_scoped_entry_does_not_leak_to_another_merchant(self):
        async with rollback_conn() as conn:
            m1 = await seed_merchant(conn)
            m2 = await seed_merchant(conn)
            e = await seed_entity(conn, "CARD")
            await er.add_list_entry(
                conn, list_type="WATCH", entity_id=e["id"], merchant_id=m1["id"],
                reason="merchant specific", added_by="test",
            )
            assert len(await er.list_active_list_entries(conn, [e["id"]], m1["id"])) == 1
            assert len(await er.list_active_list_entries(conn, [e["id"]], m2["id"])) == 0


class TestVelocityRepository:
    async def test_counts_only_inside_the_window(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            card = await seed_entity(conn, "CARD")
            for minutes in (5, 20, 50):
                await seed_transaction(
                    conn, m["id"], card_entity_id=card["id"],
                    occurred_at=NOW - timedelta(minutes=minutes),
                )
            # Outside the hour: must not be counted.
            await seed_transaction(
                conn, m["id"], card_entity_id=card["id"],
                occurred_at=NOW - timedelta(hours=5),
            )
            v = await vr.entity_velocity(
                conn, entity_column="card_entity_id", entity_id=card["id"],
                since=NOW - timedelta(hours=1), before=NOW,
            )
            assert v["txn_count"] == 3

    async def test_both_card_windows_come_from_one_query(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            card = await seed_entity(conn, "CARD")
            for minutes in (10, 30):
                await seed_transaction(
                    conn, m["id"], card_entity_id=card["id"], amount_minor=10000,
                    occurred_at=NOW - timedelta(minutes=minutes),
                )
            for hours in (3, 10, 20):
                await seed_transaction(
                    conn, m["id"], card_entity_id=card["id"], amount_minor=10000,
                    occurred_at=NOW - timedelta(hours=hours),
                )
            v = await vr.card_velocity_windows(
                conn, card_entity_id=card["id"], now=NOW,
                one_hour_ago=NOW - timedelta(hours=1),
                one_day_ago=NOW - timedelta(days=1),
            )
            assert v["count_1h"] == 2
            assert v["count_24h"] == 5
            assert v["amount_24h"] == 50000

    async def test_velocity_is_zero_for_a_brand_new_card(self):
        async with rollback_conn() as conn:
            card = await seed_entity(conn, "CARD")
            v = await vr.card_velocity_windows(
                conn, card_entity_id=card["id"], now=NOW,
                one_hour_ago=NOW - timedelta(hours=1),
                one_day_ago=NOW - timedelta(days=1),
            )
            assert v["count_1h"] == 0
            assert v["amount_24h"] == 0

    async def test_an_unknown_entity_column_is_refused(self):
        async with rollback_conn() as conn:
            card = await seed_entity(conn, "CARD")
            # Column names cannot be bind parameters, so the allow-list is
            # what stops this being an injection path.
            with pytest.raises(ValueError, match="Unknown entity column"):
                await vr.entity_velocity(
                    conn, entity_column="id; DROP TABLE transactions",
                    entity_id=card["id"], since=NOW - timedelta(hours=1), before=NOW,
                )

    async def test_shared_attribute_linking_finds_one_card_across_many_accounts(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            card = await seed_entity(conn, "CARD")
            accounts = [await seed_entity(conn, "ACCOUNT") for _ in range(4)]
            for acct in accounts:
                await seed_transaction(
                    conn, m["id"], card_entity_id=card["id"],
                    account_entity_id=acct["id"],
                    occurred_at=NOW - timedelta(days=2),
                )
            counts = await vr.shared_attribute_counts(
                conn, card_entity_id=card["id"], account_entity_id=None,
                device_entity_id=None, since=NOW - timedelta(days=30), before=NOW,
            )
            # Four different accounts, one card. Each transaction looks fine
            # alone; the pattern only exists across rows.
            assert counts["accounts_per_card_30d"] == 4

    async def test_shared_attribute_linking_finds_one_device_across_many_accounts(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            device = await seed_entity(conn, "DEVICE")
            for _ in range(6):
                acct = await seed_entity(conn, "ACCOUNT")
                email = await seed_entity(conn, "EMAIL")
                await seed_transaction(
                    conn, m["id"], device_entity_id=device["id"],
                    account_entity_id=acct["id"], email_entity_id=email["id"],
                    occurred_at=NOW - timedelta(days=1),
                )
            counts = await vr.shared_attribute_counts(
                conn, card_entity_id=None, account_entity_id=None,
                device_entity_id=device["id"],
                since=NOW - timedelta(days=30), before=NOW,
            )
            assert counts["accounts_per_device_30d"] == 6
            assert counts["emails_per_device_30d"] == 6


class TestDecisionRepository:
    async def test_stores_and_reads_back_the_frozen_snapshot(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            t = await seed_transaction(conn, m["id"])
            features = {"velocity_card_1h": 7, "cvv_match": False, "amount_minor": 25000}
            d = await dr.insert_decision(conn, {
                "transaction_id": t["id"], "ruleset_id": rs["id"], "mode": "LIVE",
                "decision": "REVIEW", "score": 60, "features": features,
                "triggered_rules": [{"code": "VEL", "weight": 35}],
                "latency_ms": 42, "exceeded_budget": False,
            })
            back = await dr.find_decision(conn, d["id"])
            # The snapshot must survive as structured data, not a string.
            assert back["features"] == features
            assert back["triggered_rules"][0]["code"] == "VEL"

    async def test_idempotency_lookup_finds_a_retried_payment(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            t = await seed_transaction(conn, m["id"], external_id="order-999")
            found = await dr.find_transaction_by_external_id(conn, m["id"], "order-999")
            assert found["id"] == t["id"]

    async def test_a_duplicate_label_from_the_same_source_is_ignored(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            t = await seed_transaction(conn, m["id"])
            payload = {
                "transaction_id": t["id"], "label": "FRAUD", "source": "CHARGEBACK",
                "reason_code": "10.4", "amount_minor": 25000,
                "labelled_at": NOW, "days_to_label": 41,
            }
            first = await dr.insert_label(conn, payload)
            second = await dr.insert_label(conn, payload)
            assert first is not None
            # The same chargeback file gets loaded twice more often than
            # anyone admits; a duplicate would double-count fraud.
            assert second is None

    async def test_two_sources_may_label_the_same_transaction(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            t = await seed_transaction(conn, m["id"])
            base = {"transaction_id": t["id"], "label": "FRAUD", "reason_code": None,
                    "amount_minor": 1000, "labelled_at": NOW, "days_to_label": 10}
            a = await dr.insert_label(conn, {**base, "source": "CHARGEBACK"})
            b = await dr.insert_label(conn, {**base, "source": "MANUAL_REVIEW"})
            assert a is not None and b is not None

    async def test_review_case_resolution_is_not_silently_overwritten(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            t = await seed_transaction(conn, m["id"])
            d = await dr.insert_decision(conn, {
                "transaction_id": t["id"], "ruleset_id": rs["id"], "mode": "LIVE",
                "decision": "REVIEW", "score": 65, "features": {},
                "triggered_rules": [], "latency_ms": 30, "exceeded_budget": False,
            })
            case = await dr.open_review_case(
                conn, decision_id=d["id"], sla_due_at=NOW + timedelta(hours=4)
            )
            first = await dr.resolve_review_case(
                conn, case_id=case["id"], disposition="APPROVE",
                analyst_note="verified by phone", assigned_to="analyst-a",
            )
            assert first["disposition"] == "APPROVE"
            # A second analyst acting on a stale queue gets None, not a
            # silent overwrite of a colleague's verdict.
            second = await dr.resolve_review_case(
                conn, case_id=case["id"], disposition="DECLINE",
                analyst_note="looks bad", assigned_to="analyst-b",
            )
            assert second is None

    async def test_the_review_queue_is_ordered_by_sla(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            for hours in (8, 2, 5):
                t = await seed_transaction(conn, m["id"])
                d = await dr.insert_decision(conn, {
                    "transaction_id": t["id"], "ruleset_id": rs["id"], "mode": "LIVE",
                    "decision": "REVIEW", "score": 65, "features": {},
                    "triggered_rules": [], "latency_ms": 30, "exceeded_budget": False,
                })
                await dr.open_review_case(
                    conn, decision_id=d["id"], sla_due_at=NOW + timedelta(hours=hours)
                )
            queue = await dr.list_open_review_cases(conn, limit=10)
            dues = [c["sla_due_at"] for c in queue]
            assert dues == sorted(dues)

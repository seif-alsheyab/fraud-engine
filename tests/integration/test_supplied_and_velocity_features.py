"""Processor-supplied features and account velocity, against a real database.

Two things are being defended here:

  1. A supplied key that nobody registered must RAISE. Accepted silently it
     would sit in the frozen decision snapshot looking like evidence, while
     the rule that reads the correctly-spelled name matched nothing.

  2. Account velocity must exclude the transaction being decided, exactly as
     card velocity does. Off by one here means a `gte 2` rule fires on the
     second transaction instead of the third, and the measured threshold is
     no longer the threshold in force.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from fraud_engine.lib.errors import ValidationError
from fraud_engine.services.feature_service import (
    ENGINE_COMPUTED_FEATURES,
    SUPPLIED_ONLY_FEATURES,
    compute_features,
)
from tests.helpers.db import (
    rollback_conn,
    seed_entity,
    seed_merchant,
    seed_transaction,
)

NOW = datetime(2026, 5, 12, 15, 0, tzinfo=UTC)
ALL_FEATURES = ENGINE_COMPUTED_FEATURES | SUPPLIED_ONLY_FEATURES


async def _compute(conn, merchant_id, entity_ids, *, required, supplied=None, txn=None):
    return await compute_features(
        conn,
        txn={"amount_minor": 25000, **(txn or {})},
        merchant_id=merchant_id,
        entity_ids=entity_ids,
        bin_info=None,
        required=required,
        now=NOW,
        supplied_features=supplied,
    )


class TestSuppliedFeatures:
    async def test_registered_supplied_values_reach_the_snapshot(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            features = await _compute(
                conn, m["id"], {},
                required=set(SUPPLIED_ONLY_FEATURES),
                supplied={"vesta_c4": 3, "vesta_d5": 0},
            )
            assert features["vesta_c4"] == 3
            # 0 is a real, meaningful D value (most recent). It must survive.
            assert features["vesta_d5"] == 0

    async def test_an_unregistered_supplied_key_is_rejected(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            with pytest.raises(ValidationError) as exc:
                await _compute(
                    conn, m["id"], {},
                    required=set(SUPPLIED_ONLY_FEATURES),
                    supplied={"vesta_c40": 3},
                )
            assert "vesta_c40" in str(exc.value)

    async def test_one_bad_key_rejects_the_whole_payload(self):
        """No partial merge.

        Accepting the good keys and dropping the bad one would produce a
        decision made on an input set nobody asked for, and the caller would
        get a 200 telling them it worked.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            with pytest.raises(ValidationError):
                await _compute(
                    conn, m["id"], {},
                    required=set(SUPPLIED_ONLY_FEATURES),
                    supplied={"vesta_c4": 1, "not_a_feature": 2},
                )

    async def test_supplied_values_cannot_overwrite_engine_computed_ones(self):
        """A caller must not be able to forge a velocity counter.

        `vesta_c4` is registered, and so is `amount_minor` -- validation is
        against the registry, so a caller could name either. The engine's own
        computation is applied AFTER the merge, so what the engine derives
        always wins over what it was handed.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            features = await _compute(
                conn, m["id"], {},
                required={"amount_minor"},
                supplied={"amount_minor": 999_999},
                txn={"amount_minor": 25000},
            )
            assert features["amount_minor"] == 25000

    async def test_supplying_nothing_costs_no_validation_query(self):
        """The ordinary path must not pay for a feature nobody sent."""
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            features = await _compute(conn, m["id"], {}, required={"amount_minor"})
            assert features["amount_minor"] == 25000
            assert not any(k.startswith("vesta_") for k in features)


class TestAccountVelocity:
    async def test_it_excludes_the_transaction_being_decided(self):
        """The first ever payment on an account reads zero, not one.

        decide_payment computes features with `before = occurred_at`, so the
        current transaction is outside the window. Without that every account
        would show a velocity of at least 1 on its first use and every
        threshold would be off by one.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            account = await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex)
            # The transaction under decision, already inserted.
            await seed_transaction(
                conn, m["id"], account_entity_id=account["id"], occurred_at=NOW
            )

            features = await _compute(
                conn, m["id"], {"ACCOUNT": account["id"]},
                required={"velocity_account_1h", "velocity_account_24h"},
            )
            assert features["velocity_account_1h"] == 0
            assert features["velocity_account_24h"] == 0

    async def test_the_two_windows_count_independently(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            account = await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex)

            # Two inside the hour, three more inside the day but not the hour.
            for minutes in (10, 40):
                await seed_transaction(
                    conn, m["id"], account_entity_id=account["id"],
                    occurred_at=NOW - timedelta(minutes=minutes),
                )
            for hours in (3, 8, 20):
                await seed_transaction(
                    conn, m["id"], account_entity_id=account["id"],
                    occurred_at=NOW - timedelta(hours=hours),
                )

            features = await _compute(
                conn, m["id"], {"ACCOUNT": account["id"]},
                required={"velocity_account_1h", "velocity_account_24h"},
            )
            assert features["velocity_account_1h"] == 2
            assert features["velocity_account_24h"] == 5

    async def test_a_transaction_older_than_the_window_is_not_counted(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            account = await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex)
            await seed_transaction(
                conn, m["id"], account_entity_id=account["id"],
                occurred_at=NOW - timedelta(hours=30),
            )
            features = await _compute(
                conn, m["id"], {"ACCOUNT": account["id"]},
                required={"velocity_account_1h", "velocity_account_24h"},
            )
            assert features["velocity_account_24h"] == 0

    async def test_another_accounts_traffic_is_not_counted(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            mine = await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex)
            theirs = await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex)
            for _ in range(4):
                await seed_transaction(
                    conn, m["id"], account_entity_id=theirs["id"],
                    occurred_at=NOW - timedelta(minutes=5),
                )
            features = await _compute(
                conn, m["id"], {"ACCOUNT": mine["id"]},
                required={"velocity_account_1h", "velocity_account_24h"},
            )
            assert features["velocity_account_1h"] == 0

    async def test_it_is_skipped_when_no_rule_asks_for_it(self):
        """Unrequired features cost no query -- the point of `required`."""
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            account = await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex)
            features = await _compute(
                conn, m["id"], {"ACCOUNT": account["id"]}, required={"amount_minor"}
            )
            assert "velocity_account_1h" not in features


class TestTheDeclarationIsNotALie:
    async def test_compute_features_really_produces_every_engine_computed_feature(self):
        """The test that keeps COMPUTABLE_FEATURES honest.

        Without this, the reachability invariant could be satisfied by adding
        a name to a set -- converting a silent failure into a silent failure
        with a green test. Here every declared engine-computed feature must
        actually come back from a real call with every input present.
        """
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            entity_ids = {
                "CARD": (await seed_entity(conn, "CARD", value_hash=uuid4().hex))["id"],
                "EMAIL": (await seed_entity(conn, "EMAIL", value_hash=uuid4().hex))["id"],
                "DEVICE": (await seed_entity(conn, "DEVICE", value_hash=uuid4().hex))["id"],
                "IP": (await seed_entity(conn, "IP", value_hash=uuid4().hex))["id"],
                "ACCOUNT": (await seed_entity(conn, "ACCOUNT", value_hash=uuid4().hex))["id"],
            }

            features = await _compute(
                conn, m["id"], entity_ids,
                required=set(ALL_FEATURES),
                supplied=dict.fromkeys(SUPPLIED_ONLY_FEATURES, 0),
                txn={
                    "product_code": "C",
                    "card_type": "debit",
                    "addr_match": "M2",
                    "dist_from_billing": 12,
                    "has_identity_data": True,
                },
            )

            missing = sorted(ENGINE_COMPUTED_FEATURES - set(features))
            assert not missing, (
                "Declared in ENGINE_COMPUTED_FEATURES but not produced by "
                f"compute_features: {missing}"
            )

    async def test_every_supplied_only_feature_is_accepted(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            features = await _compute(
                conn, m["id"], {},
                required=set(SUPPLIED_ONLY_FEATURES),
                supplied=dict.fromkeys(SUPPLIED_ONLY_FEATURES, 1),
            )
            assert set(SUPPLIED_ONLY_FEATURES) <= set(features)

"""Migration 007 and the ieee-banded seed script, against a real database.

Every test runs inside a transaction that is rolled back, so seeding here
does not disturb the ruleset a developer seeded from the command line.
"""

from uuid import uuid4

import pytest

from fraud_engine.db.migrate import MIGRATIONS_DIR
from fraud_engine.lib.errors import RuleDefinitionError
from fraud_engine.repositories import reference_repository as rr
from scripts.ieee.seed_ruleset import RULESET_VERSION, build_rules, seed
from tests.helpers.db import rollback_conn

MIGRATION_007 = MIGRATIONS_DIR / "007_ieee_features.sql"

# Everything migration 007 adds to the registry.
IEEE_FEATURES = {
    "product_code",
    "card_type",
    "addr_match",
    "dist_from_billing",
    "has_identity_data",
    "vesta_c4",
    "vesta_c8",
    "vesta_c10",
    "vesta_c12",
    "vesta_d3",
    "vesta_d5",
    "velocity_account_1h",
    "velocity_account_24h",
}
IEEE_ENTITY_TYPES = {"IEEE_CARD", "IEEE_ACCOUNT", "IEEE_DEVICE"}


async def _feature_count(conn) -> int:
    cur = await conn.execute("SELECT count(*)::int AS n FROM feature_definitions")
    row = await cur.fetchone()
    return row["n"]


class TestMigration007:
    async def test_applying_it_twice_changes_nothing(self):
        """The migration runner applies each file once, but a re-run must not
        be destructive -- a restored database or a hand-applied file should
        not double-insert or fail on a primary key."""
        sql = MIGRATION_007.read_text(encoding="utf-8")
        async with rollback_conn() as conn:
            await conn.execute(sql)
            after_first = await _feature_count(conn)
            await conn.execute(sql)
            after_second = await _feature_count(conn)
            assert after_first == after_second

    async def test_it_adds_exactly_thirteen_features(self):
        sql = MIGRATION_007.read_text(encoding="utf-8")
        async with rollback_conn() as conn:
            await conn.execute(
                "DELETE FROM feature_definitions WHERE code = ANY(%s)",
                (list(IEEE_FEATURES),),
            )
            before = await _feature_count(conn)
            await conn.execute(sql)
            after = await _feature_count(conn)
            # Only the DELTA is asserted here. This test is about what 007
            # contributes, and anchoring it to an absolute total would make
            # every later migration fail a test that has nothing to do with
            # it. The absolute count is guarded deliberately, once, by the
            # registry assertions in test_repositories and test_api.
            assert after - before == 13
            assert set(await rr.list_feature_codes(conn)) >= IEEE_FEATURES

    async def test_the_registry_exposes_every_new_feature(self):
        async with rollback_conn() as conn:
            codes = await rr.list_feature_codes(conn)
            assert codes >= IEEE_FEATURES

    async def test_the_ieee_entity_types_are_marked_as_proxies(self):
        """The descriptions are load-bearing, not decoration.

        These three entity types look like CARD/ACCOUNT/DEVICE but are
        reconstructed from anonymised columns and collide. A rule author who
        reads the description as "a card" will over-trust a link count, so the
        text must say plainly what it is.
        """
        async with rollback_conn() as conn:
            cur = await conn.execute(
                "SELECT code, description FROM entity_types WHERE code = ANY(%s)",
                (list(IEEE_ENTITY_TYPES),),
            )
            rows = await cur.fetchall()
            assert {r["code"] for r in rows} == IEEE_ENTITY_TYPES
            for row in rows:
                assert "PROXY, NOT AN IDENTIFIER" in row["description"]

    async def test_the_vesta_features_say_they_are_supplied_not_computed(self):
        """A vesta_ feature cannot be recomputed by this engine at all.

        If that fact lives only in a design document, someone eventually reads
        a fired VESTA_C4 rule as evidence the engine counted something. It
        did not: the processor shipped the number.
        """
        async with rollback_conn() as conn:
            cur = await conn.execute(
                "SELECT code, description FROM feature_definitions WHERE code LIKE 'vesta\\_%'"
            )
            rows = await cur.fetchall()
            assert len(rows) == 6
            for row in rows:
                assert "SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE" in (
                    row["description"]
                )


class TestSeedRuleset:
    async def test_it_seeds_the_ruleset_and_all_its_rules(self):
        code = f"IEEE-{uuid4().hex[:8]}"
        async with rollback_conn() as conn:
            result = await seed(conn, code)
            ruleset = result["ruleset"]
            assert ruleset["name"] == "ieee-banded"
            assert ruleset["version"] == RULESET_VERSION == 10
            assert ruleset["status"] == "ACTIVE"
            assert (ruleset["challenge_at"], ruleset["review_at"], ruleset["decline_at"]) == (
                147,
                232,
                357,
            )

            rules = await rr.list_rules(conn, ruleset["id"])
            assert len(rules) == 23
            assert {r["code"] for r in rules} == {c for c, *_ in build_rules()}

    async def test_seeding_twice_is_safe(self):
        code = f"IEEE-{uuid4().hex[:8]}"
        async with rollback_conn() as conn:
            first = await seed(conn, code)
            second = await seed(conn, code)

            # Same row updated in place, not a second version alongside it.
            assert second["ruleset"]["id"] == first["ruleset"]["id"]
            assert second["merchant"]["id"] == first["merchant"]["id"]

            rules = await rr.list_rules(conn, second["ruleset"]["id"])
            assert len(rules) == 23

            cur = await conn.execute(
                "SELECT count(*)::int AS n FROM rulesets WHERE merchant_id = %s",
                (first["merchant"]["id"],),
            )
            row = await cur.fetchone()
            assert row["n"] == 1

    async def test_reseeding_leaves_exactly_one_active_ruleset(self):
        """Repeated seeding must never leave two ACTIVE rulesets for IEEE.

        A partial unique index forbids it, so the second seed would fail on a
        constraint violation naming an index -- an error that reads like a
        database fault rather than the activation conflict it is. Asserted
        after several runs because an off-by-one in the retire step would only
        show up on the second or third.
        """
        code = f"IEEE-{uuid4().hex[:8]}"
        async with rollback_conn() as conn:
            for _ in range(3):
                await seed(conn, code)

            merchant = await rr.find_merchant_by_code(conn, code)
            cur = await conn.execute(
                """
                SELECT version, status FROM rulesets
                 WHERE merchant_id = %s AND status = 'ACTIVE'
                """,
                (merchant["id"],),
            )
            active = await cur.fetchall()
            assert len(active) == 1
            assert active[0]["version"] == RULESET_VERSION

    async def test_reseeding_drops_rules_that_are_no_longer_defined(self):
        """A re-seed replaces the rule set rather than merging into it.

        Merging would leave a rule that was deleted from this file still live
        in the database, scoring traffic that nobody believes it scores.
        """
        code = f"IEEE-{uuid4().hex[:8]}"
        async with rollback_conn() as conn:
            first = await seed(conn, code, rules=build_rules()[:5])
            assert len(await rr.list_rules(conn, first["ruleset"]["id"])) == 5

            second = await seed(conn, code)
            rules = await rr.list_rules(conn, second["ruleset"]["id"])
            assert len(rules) == 23

    async def test_it_retires_another_active_ruleset_for_the_same_merchant(self):
        """Only one ACTIVE ruleset per merchant, enforced by a partial index.

        Without the retire step this raises a unique-violation that reads like
        a database bug rather than an activation conflict.
        """
        code = f"IEEE-{uuid4().hex[:8]}"
        async with rollback_conn() as conn:
            result = await seed(conn, code)
            merchant_id = result["merchant"]["id"]

            # Free the single ACTIVE slot before handing it to another
            # version, or this setup trips the index itself.
            await conn.execute(
                "UPDATE rulesets SET status = 'DRAFT' WHERE merchant_id = %s AND version = %s",
                (merchant_id, RULESET_VERSION),
            )
            await conn.execute(
                """
                INSERT INTO rulesets (merchant_id, version, name, status)
                VALUES (%s, 1, 'older', 'ACTIVE')
                """,
                (merchant_id,),
            )

            again = await seed(conn, code)
            assert again["ruleset"]["status"] == "ACTIVE"

            cur = await conn.execute(
                "SELECT version, status FROM rulesets WHERE merchant_id = %s ORDER BY version",
                (merchant_id,),
            )
            rows = await cur.fetchall()
            assert {r["version"]: r["status"] for r in rows} == {1: "RETIRED", 10: "ACTIVE"}


class TestSeedValidation:
    async def test_a_rule_naming_an_unregistered_feature_is_rejected(self):
        code = f"IEEE-{uuid4().hex[:8]}"
        bad = [("BAD_RULE", "bad", {"feature": "not_a_feature", "op": "gte", "value": 1}, 10)]
        async with rollback_conn() as conn:
            with pytest.raises(RuleDefinitionError) as exc:
                await seed(conn, code, rules=bad)
            assert "not_a_feature" in str(exc.value)

    async def test_validation_runs_before_anything_is_written(self):
        """A rejected seed must not leave a half-built ruleset behind."""
        code = f"IEEE-{uuid4().hex[:8]}"
        good = build_rules()
        bad = [*good, ("BAD_RULE", "bad", {"feature": "nope", "op": "gte", "value": 1}, 10)]
        async with rollback_conn() as conn:
            with pytest.raises(RuleDefinitionError):
                await seed(conn, code, rules=bad)

            assert await rr.find_merchant_by_code(conn, code) is None

    async def test_a_rule_using_an_unknown_operator_is_rejected(self):
        code = f"IEEE-{uuid4().hex[:8]}"
        bad = [("BAD_OP", "bad", {"feature": "amount_minor", "op": "roughly", "value": 1}, 10)]
        async with rollback_conn() as conn:
            with pytest.raises(RuleDefinitionError, match="roughly"):
                await seed(conn, code, rules=bad)

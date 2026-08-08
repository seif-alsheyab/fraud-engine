"""Every feature a stored rule references must be reachable.

THE BUG THIS GENERALISES
------------------------
Four times now a rule has been written against a feature the engine does not
produce, and every time the symptom was the same: nothing raised, no test
failed, and the rule simply never fired. A rule that never fires is
indistinguishable from a rule that is merely quiet, so nobody investigates.

  * cvv_match           demo rules reused against IEEE data, which has no CVV
  * addr_match          registered in 007, computed nowhere
  * vesta_c4 .. d5      registered in 007, computed nowhere
  * velocity_account_*  registered in 007, computed nowhere

The last three left the IEEE ruleset able to reach a maximum score of 80
against a decline threshold of 357, so a full replay would have approved
every transaction and reported 0% recall without a single error.

WHY IT IS TESTED THIS WAY
-------------------------
References are extracted with referenced_features(), the same function
decision_service uses to decide what to compute. Matching on strings would
diverge from the evaluator the moment a condition nests, and a check that
appears to verify something while verifying something else is worse than no
check at all.
"""

from uuid import uuid4

import pytest

from fraud_engine.domain.conditions import referenced_features
from fraud_engine.repositories import reference_repository as rr
from fraud_engine.services.feature_service import (
    COMPUTABLE_FEATURES,
    ENGINE_COMPUTED_FEATURES,
    SUPPLIED_ONLY_FEATURES,
)
from scripts.ieee.seed_ruleset import seed as seed_ieee_ruleset
from tests.helpers.db import rollback_conn


async def _all_stored_rules(conn) -> list[dict]:
    """Every rule in every ruleset, enabled or not.

    Disabled rules are included deliberately: a disabled rule is one someone
    intends to switch on, and discovering it was unreachable at that moment
    is discovering it too late.

    The IEEE ruleset is seeded into this rolled-back transaction first, so
    the check has something real to bite on even against the empty database
    CI starts from. Without it the test would pass vacuously on exactly the
    run most likely to be the only one anybody looks at -- and the ruleset
    that has been broken four times is the one being skipped.
    """
    await seed_ieee_ruleset(conn, f"IEEE-REACH-{uuid4().hex[:8]}")

    cur = await conn.execute(
        """
        SELECT rs.name AS ruleset_name, rs.version, rs.status,
               r.code, r.condition
          FROM rules r
          JOIN rulesets rs ON rs.id = r.ruleset_id
         ORDER BY rs.name, rs.version, r.code
        """
    )
    return await cur.fetchall()


class TestEveryStoredRuleIsReachable:
    async def test_no_rule_references_a_feature_the_engine_cannot_produce(self):
        async with rollback_conn() as conn:
            rules = await _all_stored_rules(conn)
            # Never vacuous: _all_stored_rules seeds the IEEE ruleset first,
            # so an empty database still has 23 rules to check here.
            assert len(rules) >= 23

            unreachable: list[str] = []
            for row in rules:
                missing = referenced_features(row["condition"]) - COMPUTABLE_FEATURES
                for feature in sorted(missing):
                    unreachable.append(
                        f"{row['ruleset_name']} v{row['version']} "
                        f"({row['status']}) rule {row['code']} -> {feature}"
                    )

            assert not unreachable, (
                "These rules reference features the feature service never "
                "produces. Each one scores zero forever and looks quiet:\n  "
                + "\n  ".join(unreachable)
            )

    async def test_every_referenced_feature_is_also_registered(self):
        """Reachable and registered are different failures.

        A feature can be computed but unregistered (the registry stops being
        the description of what rules may use) or registered but uncomputed
        (the rule never fires). Both are checked, separately, so the error
        message names the right fix.
        """
        async with rollback_conn() as conn:
            registered = await rr.list_feature_codes(conn)
            rules = await _all_stored_rules(conn)

            unregistered: list[str] = []
            for row in rules:
                for feature in sorted(referenced_features(row["condition"]) - registered):
                    unregistered.append(f"{row['code']} -> {feature}")

            assert not unregistered, (
                "Rules reference features missing from feature_definitions:\n  "
                + "\n  ".join(unregistered)
            )


class TestTheComputableDeclarationIsHonest:
    """COMPUTABLE_FEATURES is a claim about the code, so it needs checking.

    Without these, the reachability test above could be satisfied by adding a
    name to a set -- which would convert a silent failure into a silent
    failure with a passing test.
    """

    async def test_every_computable_feature_is_registered(self):
        async with rollback_conn() as conn:
            registered = await rr.list_feature_codes(conn)
            assert registered >= COMPUTABLE_FEATURES, (
                "Declared computable but absent from feature_definitions: "
                f"{sorted(COMPUTABLE_FEATURES - registered)}"
            )

    def test_engine_computed_and_supplied_only_do_not_overlap(self):
        """The distinction must stay sharp.

        A feature in both sets would mean the engine both derives it and
        accepts it from outside, and nobody reading a decision could tell
        which one produced the number in front of them.
        """
        assert not (ENGINE_COMPUTED_FEATURES & SUPPLIED_ONLY_FEATURES)

    @pytest.mark.parametrize("feature", sorted(SUPPLIED_ONLY_FEATURES))
    def test_supplied_only_features_are_not_claimed_as_engine_computed(self, feature):
        assert feature not in ENGINE_COMPUTED_FEATURES

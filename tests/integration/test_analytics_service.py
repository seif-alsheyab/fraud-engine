from datetime import UTC, datetime, timedelta

from fraud_engine.repositories import decision_repository as dr
from fraud_engine.services.analytics_service import backtest_ruleset, performance_report
from tests.helpers.db import (
    rollback_conn,
    seed_merchant,
    seed_rule,
    seed_ruleset,
    seed_transaction,
)

NOW = datetime.now(UTC)
SINCE = NOW - timedelta(days=60)
BEFORE = NOW + timedelta(days=1)


async def _decision(conn, merchant_id, ruleset_id, *, decision, score, features,
                    rules, amount, occurred_at, label=None, days=40):
    t = await seed_transaction(conn, merchant_id, amount_minor=amount,
                               occurred_at=occurred_at)
    d = await dr.insert_decision(conn, {
        "transaction_id": t["id"], "ruleset_id": ruleset_id, "mode": "LIVE",
        "decision": decision, "score": score, "features": features,
        "triggered_rules": rules, "latency_ms": 30, "exceeded_budget": False,
    })
    if label:
        await dr.insert_label(conn, {
            "transaction_id": t["id"], "label": label, "source": "CHARGEBACK",
            "reason_code": "10.4" if label == "FRAUD" else None,
            "amount_minor": amount, "labelled_at": occurred_at + timedelta(days=days),
            "days_to_label": days,
        })
    return d


class TestPerformanceReport:
    async def test_reports_the_confusion_matrix_from_real_rows(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            base = NOW - timedelta(days=30)
            hit = [{"code": "VEL", "name": "velocity", "weight": 35}]

            await _decision(conn, m["id"], rs["id"], decision="DECLINE", score=85,
                            features={"velocity_card_1h": 9}, rules=hit,
                            amount=10000, occurred_at=base, label="FRAUD")
            await _decision(conn, m["id"], rs["id"], decision="APPROVE", score=5,
                            features={"velocity_card_1h": 1}, rules=[],
                            amount=20000, occurred_at=base, label="LEGITIMATE")
            await _decision(conn, m["id"], rs["id"], decision="APPROVE", score=10,
                            features={"velocity_card_1h": 2}, rules=[],
                            amount=90000, occurred_at=base, label="FRAUD")

            report = await performance_report(
                conn, merchant_id=m["id"], since=SINCE, before=BEFORE
            )
            counts = report["matrix"]["counts"]
            assert counts["true_positive"] == 1
            assert counts["true_negative"] == 1
            assert counts["false_negative"] == 1
            # The missed fraud was worth 9x the caught one -- visible only
            # because amounts are tracked alongside counts.
            assert report["matrix"]["amounts_minor"]["fraud_missed"] == 90000

    async def test_label_coverage_is_reported_alongside_the_metrics(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            base = NOW - timedelta(days=10)
            await _decision(conn, m["id"], rs["id"], decision="APPROVE", score=1,
                            features={}, rules=[], amount=1000,
                            occurred_at=base, label="LEGITIMATE")
            await _decision(conn, m["id"], rs["id"], decision="APPROVE", score=1,
                            features={}, rules=[], amount=1000,
                            occurred_at=base, label=None)

            report = await performance_report(
                conn, merchant_id=m["id"], since=SINCE, before=BEFORE
            )
            # Chargebacks take 30-90 days, so recent periods always look
            # fraud-free. Coverage is what stops that being mistaken for
            # good performance.
            assert report["coverage"]["decisions"] == 2
            assert report["coverage"]["labelled"] == 1
            assert report["coverage"]["coverage"] == 0.5

    async def test_per_rule_precision_and_lift_are_computed(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            rs = await seed_ruleset(conn, m["id"])
            base = NOW - timedelta(days=20)
            good = [{"code": "GOOD", "name": "good rule", "weight": 40}]
            noisy = [{"code": "NOISY", "name": "noisy rule", "weight": 15}]

            for _ in range(3):
                await _decision(conn, m["id"], rs["id"], decision="DECLINE", score=90,
                                features={}, rules=good, amount=5000,
                                occurred_at=base, label="FRAUD")
            for _ in range(6):
                await _decision(conn, m["id"], rs["id"], decision="REVIEW", score=65,
                                features={}, rules=noisy, amount=5000,
                                occurred_at=base, label="LEGITIMATE")

            report = await performance_report(
                conn, merchant_id=m["id"], since=SINCE, before=BEFORE
            )
            by_code = {r["code"]: r for r in report["rules"]}
            assert by_code["GOOD"]["precision"] == 1.0
            # The noisy rule fired six times and was wrong every time: it is
            # pure friction on honest customers.
            assert by_code["NOISY"]["precision"] == 0.0


class TestBacktest:
    async def test_a_stricter_candidate_catches_more_fraud_and_blocks_more_good(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            live = await seed_ruleset(conn, m["id"], challenge_at=40, review_at=60,
                                      decline_at=80)
            base = NOW - timedelta(days=25)

            # History: one fraud the live rules missed, one good customer.
            await _decision(conn, m["id"], live["id"], decision="APPROVE", score=30,
                            features={"velocity_card_1h": 3}, rules=[],
                            amount=50000, occurred_at=base, label="FRAUD")
            await _decision(conn, m["id"], live["id"], decision="APPROVE", score=30,
                            features={"velocity_card_1h": 3}, rules=[],
                            amount=20000, occurred_at=base, label="LEGITIMATE")

            # Candidate: fires at 3 instead of 4, hard enough to decline.
            cand = await seed_ruleset(conn, m["id"], version=2, status="DRAFT",
                                      challenge_at=20, review_at=40, decline_at=60)
            await seed_rule(conn, cand["id"], code="VEL3", name="Card 3+ in 1h",
                            condition={"feature": "velocity_card_1h", "op": "gte",
                                       "value": 3},
                            weight=70)

            result = await backtest_ruleset(
                conn, merchant_id=m["id"], candidate_ruleset_id=cand["id"],
                since=SINCE, before=BEFORE,
            )

            assert result["replayed_count"] == 2
            # Both decisions flip, because the candidate cannot tell the two
            # apart on these features. That is the trade-off made visible.
            assert result["changed_decision_count"] == 2
            assert result["delta"]["extra_fraud_caught_minor"] == 50000
            assert result["delta"]["extra_good_customers_blocked"] == 1

    async def test_a_backtest_writes_nothing(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            live = await seed_ruleset(conn, m["id"])
            base = NOW - timedelta(days=5)
            await _decision(conn, m["id"], live["id"], decision="APPROVE", score=0,
                            features={"amount_minor": 1000}, rules=[],
                            amount=1000, occurred_at=base, label="LEGITIMATE")
            cand = await seed_ruleset(conn, m["id"], version=2, status="DRAFT")

            cur = await conn.execute("SELECT count(*)::int AS n FROM decisions")
            before_count = (await cur.fetchone())["n"]

            await backtest_ruleset(conn, merchant_id=m["id"],
                                   candidate_ruleset_id=cand["id"],
                                   since=SINCE, before=BEFORE)

            cur = await conn.execute("SELECT count(*)::int AS n FROM decisions")
            # A backtest is a read-only thought experiment. Writing a row per
            # replay would bloat the table production reads from.
            assert (await cur.fetchone())["n"] == before_count

    async def test_the_replay_uses_frozen_features_not_recomputed_ones(self):
        async with rollback_conn() as conn:
            m = await seed_merchant(conn)
            live = await seed_ruleset(conn, m["id"])
            base = NOW - timedelta(days=40)

            # The snapshot says velocity was 9 at the time. The card may be
            # quiet today -- irrelevant, because the replay must answer the
            # question the engine actually faced.
            await _decision(conn, m["id"], live["id"], decision="APPROVE", score=0,
                            features={"velocity_card_1h": 9}, rules=[],
                            amount=10000, occurred_at=base, label="FRAUD")

            cand = await seed_ruleset(conn, m["id"], version=2, status="DRAFT",
                                      challenge_at=20, review_at=40, decline_at=50)
            await seed_rule(conn, cand["id"], code="VEL", name="velocity",
                            condition={"feature": "velocity_card_1h", "op": "gte",
                                       "value": 4},
                            weight=60)

            result = await backtest_ruleset(
                conn, merchant_id=m["id"], candidate_ruleset_id=cand["id"],
                since=SINCE, before=BEFORE,
            )
            assert result["changed_decision_count"] == 1
            assert result["changed_sample"][0]["now"] == "DECLINE"
            assert result["delta"]["extra_fraud_caught_minor"] == 10000

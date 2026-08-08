from datetime import UTC, datetime

import pytest
import pytest_asyncio

from tests.helpers.api import (
    cleanup_scope,
    direct_conn,
    seed_merchant_with_rules,
    shared_api_client,
)

SCOPE = "APITEST-HTTP"

# Two separate scopes have to agree here, and they are configured in two
# different places:
#
#   scope="module"      how long the fixture VALUE lives      (pytest)
#   loop_scope="module" which event loop the fixture RUNS on  (pytest-asyncio)
#
# loop_scope is NOT a pytest.fixture argument -- it only exists on
# pytest_asyncio.fixture. Using @pytest.fixture(loop_scope=...) raises
# TypeError at collection; omitting loop_scope entirely leaves the fixture on
# the default function-scoped loop and raises ScopeMismatch for every test in
# the file, with a message naming an internal fixture that explains nothing.
#
# pyproject sets asyncio_default_fixture_loop_scope = "function". That is a
# DEFAULT, not a constraint: this override applies to this fixture only and
# leaves every other test file untouched.
pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def client():
    """One pool, one app, one client for the whole file.

    Per-test setup would open and tear down a connection pool 22 times --
    slow, and exactly the kind of shared-resource churn that turns flaky
    under parallel runs.
    """
    await cleanup_scope(SCOPE)
    await seed_merchant_with_rules(SCOPE)

    async with shared_api_client() as c:
        yield c

    removed = await cleanup_scope(SCOPE)
    async with direct_conn() as conn:
        cur = await conn.execute(
            "SELECT count(*)::int AS n FROM merchants WHERE code LIKE %s",
            (f"{SCOPE}%",),
        )
        left = (await cur.fetchone())["n"]
    if left != 0:
        raise AssertionError(f"cleanup incomplete: {left} merchants left ({removed})")


def payload(**over):
    base = {
        "merchant_code": f"{SCOPE}-M",
        "external_id": f"ord-{datetime.now(UTC).timestamp()}",
        "amount_minor": 25000,
        "currency": "USD",
        "card_number": "4111111111111111",
        "email": "buyer@example.com",
        "device_fingerprint": "dev-1",
        "ip_address": "203.0.113.9",
        "account_id": "acct-1",
        "cvv_match": True,
        "three_ds_status": "NOT_USED",
        "billing_country": "JO",
        "ip_country": "JO",
    }
    base.update(over)
    return base


class TestOps:
    async def test_health_does_not_touch_the_database(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_ready_checks_the_database(self, client):
        r = await client.get("/ready")
        assert r.status_code == 200
        assert r.json()["database"] == "reachable"

    async def test_openapi_schema_is_generated(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        assert "/v1/decide" in r.json()["paths"]


class TestDecide:
    async def test_a_clean_payment_is_approved(self, client):
        r = await client.post("/v1/decide", json=payload(external_id=f"{SCOPE}-clean"))
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "APPROVE"
        assert body["idempotent_replay"] is False

    async def test_the_latency_header_is_present(self, client):
        r = await client.post("/v1/decide", json=payload(external_id=f"{SCOPE}-lat"))
        assert "x-decision-latency-ms" in r.headers
        assert int(r.headers["x-decision-latency-ms"]) >= 0

    async def test_the_response_never_echoes_the_card_number(self, client):
        pan = "4111111111111111"
        r = await client.post(
            "/v1/decide", json=payload(external_id=f"{SCOPE}-pan", card_number=pan)
        )
        # The whole serialised body must not contain the PAN anywhere --
        # not in features, not in triggered rules, not in an error message.
        assert pan not in r.text

    async def test_an_unknown_field_is_rejected_rather_than_ignored(self, client):
        body = payload(external_id=f"{SCOPE}-extra")
        body["card_numbr"] = "4111111111111111"  # typo
        r = await client.post("/v1/decide", json=body)
        # Silently ignoring it would mean deciding with no card at all and
        # returning a confident APPROVE nobody could explain later.
        assert r.status_code == 422

    async def test_a_float_amount_is_rejected(self, client):
        r = await client.post(
            "/v1/decide",
            json=payload(external_id=f"{SCOPE}-float", amount_minor=125.55),
        )
        assert r.status_code == 422

    async def test_a_card_number_that_is_not_digits_is_rejected(self, client):
        r = await client.post(
            "/v1/decide",
            json=payload(external_id=f"{SCOPE}-badcard", card_number="not-a-card"),
        )
        assert r.status_code == 422

    async def test_an_unknown_merchant_returns_404(self, client):
        r = await client.post(
            "/v1/decide",
            json=payload(merchant_code="NOPE", external_id=f"{SCOPE}-nomerchant"),
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    async def test_a_retry_returns_the_original_decision(self, client):
        body = payload(external_id=f"{SCOPE}-idem")
        first = await client.post("/v1/decide", json=body)
        second = await client.post("/v1/decide", json=body)
        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True
        assert second.json()["decision_id"] == first.json()["decision_id"]


class TestVelocityOverHttp:
    async def test_a_burst_on_one_card_escalates_the_decision(self, client):
        for i in range(5):
            await client.post(
                "/v1/decide",
                json=payload(
                    external_id=f"{SCOPE}-burst-{i}",
                    card_number="4222222222222222",
                    cvv_match=False,
                ),
            )
        final = await client.post(
            "/v1/decide",
            json=payload(
                external_id=f"{SCOPE}-burst-final",
                card_number="4222222222222222",
                cvv_match=False,
            ),
        )
        body = final.json()
        # No single payment in this burst is remarkable. The pattern is.
        assert body["features"]["velocity_card_1h"] >= 4
        assert body["decision"] in {"REVIEW", "DECLINE"}


class TestSuppliedFeatures:
    """The HTTP contract for processor-supplied aggregates.

    The engine cannot derive the vesta_ family, so this endpoint is the only
    way they can reach a rule. If the field is dropped or renamed, every
    banded rule in the IEEE ruleset silently stops firing.
    """

    async def test_supplied_values_are_recorded_in_the_snapshot(self, client):
        r = await client.post(
            "/v1/decide",
            json=payload(
                external_id=f"{SCOPE}-supplied",
                supplied_features={"vesta_c4": 3, "vesta_d5": 0},
            ),
        )
        assert r.status_code == 200
        features = r.json()["features"]
        assert features["vesta_c4"] == 3
        # 0 is the most recent possible D value, not a missing one.
        assert features["vesta_d5"] == 0

    async def test_an_unregistered_supplied_key_is_rejected(self, client):
        r = await client.post(
            "/v1/decide",
            json=payload(
                external_id=f"{SCOPE}-supplied-bad",
                supplied_features={"vesta_c40": 1},
            ),
        )
        # 400, not 200-with-the-key-dropped. Silently ignoring it would put a
        # value in the frozen snapshot that no rule reads, while the rule
        # that reads the correct spelling matched nothing.
        assert r.status_code == 400
        assert "vesta_c40" in r.text

    async def test_the_new_transaction_attributes_reach_the_snapshot(self, client):
        r = await client.post(
            "/v1/decide",
            json=payload(
                external_id=f"{SCOPE}-attrs",
                product_code="C",
                card_type="debit",
                addr_match="M2",
                dist_from_billing=12.5,
                has_identity_data=True,
            ),
        )
        assert r.status_code == 200
        features = r.json()["features"]
        assert features["product_code"] == "C"
        assert features["card_type"] == "debit"
        assert features["addr_match"] == "M2"
        assert features["dist_from_billing"] == 12.5
        assert features["has_identity_data"] is True

    async def test_omitting_them_yields_none_rather_than_a_guess(self, client):
        r = await client.post("/v1/decide", json=payload(external_id=f"{SCOPE}-attrs-absent"))
        assert r.status_code == 200
        features = r.json()["features"]
        assert features["product_code"] is None
        assert features["card_type"] is None
        assert features["dist_from_billing"] is None
        assert features["has_identity_data"] is None
        # The one deliberate exception: M4's absence is itself a category.
        assert features["addr_match"] == "(absent)"


class TestLabels:
    async def test_a_label_can_be_attached_to_a_past_transaction(self, client):
        ext = f"{SCOPE}-label-1"
        await client.post("/v1/decide", json=payload(external_id=ext))
        r = await client.post(
            "/v1/labels",
            json={
                "merchant_code": f"{SCOPE}-M",
                "external_id": ext,
                "label": "FRAUD",
                "source": "CHARGEBACK",
                "reason_code": "10.4",
            },
        )
        assert r.status_code == 201
        assert r.json()["created"] is True

    async def test_a_duplicate_label_is_reported_not_silently_accepted(self, client):
        ext = f"{SCOPE}-label-2"
        await client.post("/v1/decide", json=payload(external_id=ext))
        body = {
            "merchant_code": f"{SCOPE}-M",
            "external_id": ext,
            "label": "FRAUD",
            "source": "CHARGEBACK",
        }
        await client.post("/v1/labels", json=body)
        second = await client.post("/v1/labels", json=body)
        # The same chargeback file gets loaded twice more often than anyone
        # admits; a silent success would double-count fraud in every metric.
        assert second.json()["duplicate"] is True

    async def test_labelling_an_unknown_transaction_returns_404(self, client):
        r = await client.post(
            "/v1/labels",
            json={
                "merchant_code": f"{SCOPE}-M",
                "external_id": "does-not-exist",
                "label": "FRAUD",
                "source": "CHARGEBACK",
            },
        )
        assert r.status_code == 404


class TestAnalytics:
    async def test_performance_report_returns_a_matrix_and_coverage(self, client):
        r = await client.get(
            "/v1/performance", params={"merchant_code": f"{SCOPE}-M", "days": 90}
        )
        assert r.status_code == 200
        body = r.json()
        assert "matrix" in body
        assert "counts" in body["matrix"]
        # Coverage sits beside the metrics because a period with few labels
        # always looks flatteringly fraud-free.
        assert "coverage" in body

    async def test_an_out_of_range_window_is_rejected(self, client):
        r = await client.get(
            "/v1/performance", params={"merchant_code": f"{SCOPE}-M", "days": 9999}
        )
        assert r.status_code == 422


class TestReference:
    async def test_the_feature_registry_is_exposed(self, client):
        r = await client.get("/v1/features")
        assert r.status_code == 200
        codes = {f["code"] for f in r.json()["data"]}
        # A rule-authoring UI reads this rather than hardcoding a list that
        # drifts out of sync with the database.
        assert "velocity_card_1h" in codes
        # 27 from migration 006, the 13 IEEE-era features in 007, and
        # account_seen_count in 008. Updated deliberately per migration.
        assert len(codes) == 41

    async def test_adding_to_the_deny_list_does_not_echo_the_raw_value(self, client):
        pan = "4999999999999999"
        r = await client.post(
            "/v1/lists",
            json={
                "list_type": "DENY",
                "entity_type": "CARD",
                "value": pan,
                "merchant_code": f"{SCOPE}-M",
                "reason": "confirmed fraud",
                "added_by": "test",
            },
        )
        assert r.status_code == 201
        assert pan not in r.text
        assert r.json()["data"]["display_hint"] == "9999"

    async def test_a_deny_listed_card_is_then_declined(self, client):
        pan = "4555555555555555"
        await client.post(
            "/v1/lists",
            json={
                "list_type": "DENY",
                "entity_type": "CARD",
                "value": pan,
                "merchant_code": f"{SCOPE}-M",
                "reason": "confirmed fraud",
                "added_by": "test",
            },
        )
        r = await client.post(
            "/v1/decide",
            json=payload(
                external_id=f"{SCOPE}-denied",
                card_number=pan,
                three_ds_status="AUTHENTICATED",
            ),
        )
        # 3DS normally pulls the score down and shifts liability. A deny
        # entry is newer information about the same card and must win.
        assert r.json()["decision"] == "DECLINE"

    async def test_the_review_queue_is_readable(self, client):
        r = await client.get("/v1/review-queue")
        assert r.status_code == 200
        assert isinstance(r.json()["data"], list)

    async def test_resolving_an_unknown_case_returns_409(self, client):
        r = await client.post(
            "/v1/review-cases/00000000-0000-0000-0000-000000000000/resolve",
            json={"disposition": "APPROVE", "assigned_to": "analyst-a"},
        )
        # Not 404: "already resolved by a colleague" and "does not exist"
        # both mean the same thing to the caller -- you cannot act on it.
        assert r.status_code == 409


class TestMinimalPayload:
    """The smallest request a real caller can send.

    A live smoke test found a NOT NULL violation here that 130 tests missed,
    because every test payload supplied every optional field.
    """

    async def test_a_minimal_payload_returns_a_decision(self, client):
        r = await client.post("/v1/decide", json={
            "merchant_code": f"{SCOPE}-M",
            "external_id": f"{SCOPE}-minimal-1",
            "amount_minor": 25000,
        })
        assert r.status_code == 200, r.text
        assert r.json()["decision"] in {"APPROVE", "CHALLENGE", "REVIEW", "DECLINE"}

    async def test_explicit_nulls_on_nullable_fields_are_accepted(self, client):
        """Nullable fields may be sent as null.

        currency and card_number are typed `str | None`, so an explicit null
        is valid input and must fall back to the merchant currency rather
        than reaching a NOT NULL column.
        """
        r = await client.post("/v1/decide", json={
            "merchant_code": f"{SCOPE}-M",
            "external_id": f"{SCOPE}-minimal-2",
            "amount_minor": 25000,
            "currency": None,
            "card_number": None,
        })
        assert r.status_code == 200, r.text

    async def test_a_null_on_a_non_nullable_field_is_rejected(self, client):
        """Optional is not the same as nullable.

        `channel: Literal["WEB","MOBILE","API","POS"] = "WEB"` may be OMITTED
        -- Pydantic then supplies "WEB" -- but null is not one of the four
        permitted values. Rejecting it with a field-level 422 is correct: a
        caller sending null meant something, and silently substituting a
        default would hide the mistake.
        """
        r = await client.post("/v1/decide", json={
            "merchant_code": f"{SCOPE}-M",
            "external_id": f"{SCOPE}-null-channel",
            "amount_minor": 25000,
            "channel": None,
        })
        assert r.status_code == 422
        detail = r.json()["detail"][0]
        assert detail["loc"] == ["body", "channel"]

    async def test_an_omitted_channel_uses_the_pydantic_default(self, client):
        r = await client.post("/v1/decide", json={
            "merchant_code": f"{SCOPE}-M",
            "external_id": f"{SCOPE}-omitted-channel",
            "amount_minor": 25000,
        })
        assert r.status_code == 200, r.text

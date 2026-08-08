"""T6 acceptance: the replay harness.

This suite COMMITS -- replay() drives decide_payment through the real pool
and cannot share a rolled-back transaction. It therefore owns its own scope
prefix and cleans up after itself, exactly as the HTTP suite does. A shared
prefix would let one suite's cleanup delete another's fixtures mid-run
(CLAUDE.md §7).

The replay runs against the committed 100-row fixture, not the real CSVs:
the dataset is licensed for academic use, never redistributed, and CI must
be green without it (plan §1.1). The one test that needs the real file skips
when it is absent.
"""

import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from scripts.ieee.loader import (
    IDENTITY_FILENAME,
    TRANSACTION_FILENAME,
    transaction_csv,
)
from scripts.ieee.replay import EPOCH, build_payload, occurred_at_for, replay
from scripts.ieee.seed_ruleset import seed as seed_ieee_ruleset
from tests.helpers.api import cleanup_scope, direct_conn

SCOPE = "IEEEREPLAY"
MERCHANT = f"{SCOPE}-M"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_ROWS = 100

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    """The fixture CSVs under the filenames the loader expects.

    transaction_csv() resolves `<base>/train_transaction.csv`, so the sample
    files have to be presented under those names rather than pointed at
    directly.
    """
    base = tmp_path_factory.mktemp("ieee")
    shutil.copy(FIXTURES / "ieee_sample.csv", base / TRANSACTION_FILENAME)
    shutil.copy(FIXTURES / "ieee_sample_identity.csv", base / IDENTITY_FILENAME)
    return base


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def merchant_with_ruleset():
    await cleanup_scope(SCOPE)
    async with direct_conn() as conn:
        await seed_ieee_ruleset(conn, MERCHANT)
        await conn.commit()
    yield
    await cleanup_scope(SCOPE)


async def _transaction_count() -> int:
    async with direct_conn() as conn:
        cur = await conn.execute(
            """
            SELECT count(*)::int AS n FROM transactions t
              JOIN merchants m ON m.id = t.merchant_id
             WHERE m.code = %s
            """,
            (MERCHANT,),
        )
        return (await cur.fetchone())["n"]


async def _label_counts() -> dict[str, int]:
    async with direct_conn() as conn:
        cur = await conn.execute(
            """
            SELECT l.label, count(*)::int AS n FROM labels l
              JOIN transactions t ON t.id = l.transaction_id
              JOIN merchants m ON m.id = t.merchant_id
             WHERE m.code = %s GROUP BY l.label
            """,
            (MERCHANT,),
        )
        return {r["label"]: r["n"] for r in await cur.fetchall()}


class TestEpochMapping:
    def test_the_epoch_is_fixed_and_documented_as_arbitrary(self):
        """Only consistency matters, but it matters absolutely.

        Velocity is relative, so no metric depends on the absolute date --
        but a run that used a different epoch would compute different window
        boundaries and could not be compared with this one.
        """
        assert EPOCH.isoformat() == "2017-12-01T00:00:00+00:00"

    def test_transaction_dt_is_seconds_from_the_epoch(self):
        # 86,400 is the smallest TransactionDT in the real file.
        assert occurred_at_for(86_400).isoformat() == "2017-12-02T00:00:00+00:00"


class TestPayloadMapping:
    @pytest.fixture(scope="class")
    def row(self, data_dir):
        from scripts.ieee.loader import identity_csv, load_transactions

        return next(iter(load_transactions(transaction_csv(data_dir), identity_csv(data_dir))))

    def test_entity_keys_are_raw_composites_not_pre_hashed(self, row):
        """decide_payment hashes what it is given.

        Pre-hashing here would produce a hash of a hash: stable, consistent,
        and therefore invisible -- while making every entity in the replay
        unrelatable to one created by any other code path.
        """
        payload = build_payload(row, MERCHANT)
        # The composite key separator survives, so this is plainly not a hash.
        assert "|" in payload["card_number"]
        assert "|" in payload["account_id"]

    def test_amount_is_integer_minor_units(self, row):
        payload = build_payload(row, MERCHANT)
        assert isinstance(payload["amount_minor"], int)
        assert payload["amount_minor"] == round(row["TransactionAmt"] * 100)

    def test_a_null_m4_becomes_the_absent_category(self, row):
        payload = build_payload({**row, "M4": None}, MERCHANT)
        assert payload["addr_match"] == "(absent)"

    def test_a_null_vesta_column_is_omitted_not_sent_as_none(self, row):
        payload = build_payload({**row, "C4": None}, MERCHANT)
        supplied = payload["supplied_features"]
        # Omitted, not None: a supplied None would sit in the frozen snapshot
        # looking like a measured zero.
        assert "vesta_c4" not in supplied
        assert "vesta_c8" in supplied

    def test_all_vesta_columns_null_omits_the_key_entirely(self, row):
        blank = {**row, "C4": None, "C8": None, "C10": None,
                 "C12": None, "D3": None, "D5": None}
        assert "supplied_features" not in build_payload(blank, MERCHANT)

    def test_a_row_without_device_columns_yields_no_device_fingerprint(self, row):
        """None, never a hash of "nan|nan|nan|nan|nan".

        Hashing the rendered key would merge three quarters of the dataset
        into one device entity and make every link rule fire on everything.
        """
        stripped = {**row, "DeviceInfo": None, "DeviceType": None,
                    "id_30": None, "id_31": None, "id_33": None}
        assert build_payload(stripped, MERCHANT)["device_fingerprint"] is None


class TestReplayIsIdempotent:
    async def test_replaying_the_same_rows_twice_leaves_one_transaction_each(self, data_dir):
        """The acceptance criterion from plan §4.4.

        A re-run must be safe: external_id is unique per merchant and
        decide_payment returns the original decision for one already present.
        Without that, a resumed or repeated replay would double every
        velocity counter it had already built.
        """
        first = await replay(
            merchant_code=MERCHANT, warmup_days=0, limit=None, base_path=data_dir
        )
        assert first["rows"]["replayed"] == FIXTURE_ROWS
        assert first["rows"]["idempotent_replays"] == 0
        assert await _transaction_count() == FIXTURE_ROWS

        second = await replay(
            merchant_code=MERCHANT, warmup_days=0, limit=None, base_path=data_dir
        )
        assert second["rows"]["replayed"] == FIXTURE_ROWS
        # Every row recognised as a repeat, not re-decided.
        assert second["rows"]["idempotent_replays"] == FIXTURE_ROWS
        assert await _transaction_count() == FIXTURE_ROWS

        # The decision mix must be identical -- a repeat returns the ORIGINAL
        # answer, not a fresh one computed against moved-on velocity.
        assert second["decisions"] == first["decisions"]

    async def test_one_label_per_transaction_and_no_duplicates_on_re_run(self):
        counts = await _label_counts()
        assert sum(counts.values()) == FIXTURE_ROWS
        # The fixture carries both classes, so neither branch is untested.
        assert counts.get("FRAUD", 0) > 0
        assert counts.get("LEGITIMATE", 0) > 0

    async def test_labels_record_the_synthetic_delay(self):
        async with direct_conn() as conn:
            cur = await conn.execute(
                """
                SELECT DISTINCT l.days_to_label FROM labels l
                  JOIN transactions t ON t.id = l.transaction_id
                  JOIN merchants m ON m.id = t.merchant_id
                 WHERE m.code = %s
                """,
                (MERCHANT,),
            )
            assert [r["days_to_label"] for r in await cur.fetchall()] == [45]


class TestTheRunSummary:
    async def test_it_reports_all_three_period_boundaries(self, data_dir):
        summary = await replay(
            merchant_code=MERCHANT, warmup_days=0, limit=None, base_path=data_dir
        )
        periods = summary["periods"]
        # T7 filters on these directly rather than re-deriving epoch
        # arithmetic and getting a different answer.
        for key in ("warmup_end", "fit_end", "eval_start", "data_start", "data_end"):
            assert key in periods
        assert periods["eval_start"] == periods["fit_end"]
        assert periods["warmup_end"] <= periods["fit_end"] <= periods["data_end"]

    async def test_a_warmup_longer_than_the_data_is_flagged_unmeasurable(self, data_dir):
        """A smoke run must not look like a measurement.

        The fixture spans under a day, so a 60-day warm-up leaves nothing in
        the measured window. Collapsing all three boundaries onto one instant
        would otherwise produce a summary that reads as valid.
        """
        summary = await replay(
            merchant_code=MERCHANT, warmup_days=60, limit=None, base_path=data_dir
        )
        assert summary["periods"]["measurable"] is False
        assert "smoke test, not a measurement" in summary["periods"]["why_not_measurable"]

    async def test_it_records_the_ruleset_actually_used(self, data_dir):
        summary = await replay(
            merchant_code=MERCHANT, warmup_days=0, limit=2, base_path=data_dir
        )
        assert summary["ruleset"]["name"] == "ieee-banded"
        assert summary["ruleset"]["version"] == 10

    async def test_days_to_label_is_declared_synthetic(self, data_dir):
        summary = await replay(
            merchant_code=MERCHANT, warmup_days=0, limit=2, base_path=data_dir
        )
        assert summary["days_to_label"]["value"] == 45
        assert summary["days_to_label"]["synthetic"] is True


class TestAgainstTheRealDataset:
    """Skipped wherever the dataset is absent -- which includes CI."""

    @pytest.fixture(scope="class")
    def real_data(self):
        try:
            path = transaction_csv()
        except RuntimeError:
            pytest.skip("IEEE_DATA_PATH is not configured")
        if not path.exists():
            pytest.skip(f"{path} not present")
        return path.parent

    async def test_replaying_a_thousand_rows_twice_leaves_a_thousand(self, real_data):
        """Plan §4.4 verbatim, on the real file rather than the fixture."""
        scope = f"{SCOPE}REAL"
        merchant = f"{scope}-M"
        await cleanup_scope(scope)
        try:
            async with direct_conn() as conn:
                await seed_ieee_ruleset(conn, merchant)
                await conn.commit()

            first = await replay(
                merchant_code=merchant, warmup_days=0, limit=1000, base_path=real_data
            )
            assert first["rows"]["replayed"] == 1000

            async with direct_conn() as conn:
                cur = await conn.execute(
                    "SELECT count(*)::int AS n FROM transactions t "
                    "JOIN merchants m ON m.id = t.merchant_id WHERE m.code = %s",
                    (merchant,),
                )
                assert (await cur.fetchone())["n"] == 1000

            second = await replay(
                merchant_code=merchant, warmup_days=0, limit=1000, base_path=real_data
            )
            assert second["rows"]["idempotent_replays"] == 1000

            async with direct_conn() as conn:
                cur = await conn.execute(
                    "SELECT count(*)::int AS n FROM transactions t "
                    "JOIN merchants m ON m.id = t.merchant_id WHERE m.code = %s",
                    (merchant,),
                )
                assert (await cur.fetchone())["n"] == 1000
        finally:
            await cleanup_scope(scope)

    async def test_the_decision_mix_is_not_uniformly_approve(self, real_data):
        """The check that catches a ruleset gone inert.

        Every feature the IEEE rules reference has to survive the trip from
        CSV column to payload field. When one does not, nothing raises: the
        rules simply stop firing and every transaction is approved.
        """
        scope = f"{SCOPE}MIX"
        merchant = f"{scope}-M"
        await cleanup_scope(scope)
        try:
            async with direct_conn() as conn:
                await seed_ieee_ruleset(conn, merchant)
                await conn.commit()
            summary = await replay(
                merchant_code=merchant, warmup_days=0, limit=2000, base_path=real_data
            )
            assert set(summary["decisions"]) != {"APPROVE"}
        finally:
            await cleanup_scope(scope)

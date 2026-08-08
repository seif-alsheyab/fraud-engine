"""The committed fixtures must be synthetic, and provably so.

The IEEE-CIS licence (Vesta, 2019) permits research use and forbids
redistribution. The README states that the dataset is not in this repository.
A fixture carrying real rows would make that statement false, and the failure
is silent: a CSV with the right header looks correct whatever its provenance.

So provenance is asserted here instead, and asserted **without the dataset**.
A licence check that only runs on a machine where the licensed data already
sits is a check that never runs where it matters -- not in CI, and not for
anyone auditing the repository from a clone.

The guarantee is arithmetic rather than statistical: every fixture id lies
outside the real dataset's id range, so no fixture row can be a real row
whatever else is true of it. Regenerate with `scripts/ieee/make_fixtures.py`.
"""

import csv
from pathlib import Path

import pytest

from scripts.ieee.make_fixtures import (
    FIRST_ID,
    IDENTITY_COLUMNS,
    REAL_ID_MAX,
    REAL_ID_MIN,
    ROWS,
    TRANSACTION_COLUMNS,
    generate,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "ieee_sample.csv"
SAMPLE_IDENTITY = FIXTURES / "ieee_sample_identity.csv"


def _rows(path: Path) -> list[dict[str, str]]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _ids(path: Path) -> list[int]:
    return [int(r["TransactionID"]) for r in _rows(path)]


class TestProvenance:
    """The licence check. Needs no dataset, by design."""

    @pytest.mark.parametrize("path", [SAMPLE, SAMPLE_IDENTITY], ids=["transactions", "identity"])
    def test_no_fixture_id_falls_in_the_real_dataset_range(self, path):
        """The whole guarantee, in one assertion.

        The real ids span 2,987,000-3,577,539. Every fixture id is above that,
        so no fixture row can be a row of the licensed dataset -- there is no
        id it could share. This is what makes the README's "not in this
        repository" claim checkable rather than merely stated.
        """
        offenders = [i for i in _ids(path) if REAL_ID_MIN <= i <= REAL_ID_MAX]
        assert not offenders, (
            f"{path.name} contains ids inside the real IEEE-CIS range "
            f"({REAL_ID_MIN}-{REAL_ID_MAX}): {offenders[:10]}. These may be rows "
            "of the licensed dataset, which must not be redistributed. "
            "Regenerate with scripts/ieee/make_fixtures.py."
        )

    @pytest.mark.parametrize("path", [SAMPLE, SAMPLE_IDENTITY], ids=["transactions", "identity"])
    def test_ids_start_where_the_generator_says(self, path):
        assert min(_ids(path)) >= FIRST_ID

    def test_the_gap_below_the_generated_range_is_wide(self):
        """Not a near miss.

        FIRST_ID sits millions above the real maximum rather than just past
        it, so no plausible extension of the dataset could ever reach it.
        """
        assert FIRST_ID - REAL_ID_MAX > 5_000_000


class TestSchemaFidelity:
    """Synthetic content, identical shape -- the fixture is only useful if the
    loader sees the same columns it would see in the real file."""

    def test_the_transaction_header_matches_the_declared_schema(self):
        with open(SAMPLE, newline="") as f:
            assert next(csv.reader(f)) == TRANSACTION_COLUMNS

    def test_the_identity_header_matches_the_declared_schema(self):
        with open(SAMPLE_IDENTITY, newline="") as f:
            assert next(csv.reader(f)) == IDENTITY_COLUMNS

    def test_the_full_v_block_is_present(self):
        """All 339, not a token few.

        The loader drops V columns at the CSV boundary; a fixture with five of
        them would test that on a 60-column header instead of the 394-column
        one the real file has.
        """
        v = [c for c in TRANSACTION_COLUMNS if c.startswith("V")]
        assert len(v) == 339
        assert v[0] == "V1" and v[-1] == "V339"

    def test_the_row_count_is_what_the_tests_expect(self):
        assert len(_ids(SAMPLE)) == ROWS


class TestDeterminism:
    def test_the_same_seed_reproduces_the_committed_file(self):
        """The committed fixture must be exactly what the generator emits.

        Without this, a fixture could be hand-edited and the generator would
        become documentation of how the file was once produced rather than a
        way to reproduce it.
        """
        transactions, identity = generate()
        assert [r["TransactionID"] for r in transactions] == [str(i) for i in _ids(SAMPLE)]
        assert transactions == _rows(SAMPLE)
        assert identity == _rows(SAMPLE_IDENTITY)

    def test_a_different_seed_produces_different_data(self):
        assert generate(seed=7)[0] != generate(seed=42)[0]


class TestTheShapeTheOtherTestsRelyOn:
    """Properties the loader and mapping suites assume.

    Asserted here so a regeneration that quietly dropped one of them fails
    with a message naming the missing property, rather than as an unrelated
    test failing somewhere else for a reason nobody can see.
    """

    def test_timestamps_strictly_ascend(self):
        dts = [int(r["TransactionDT"]) for r in _rows(SAMPLE)]
        assert dts == sorted(dts)
        assert len(set(dts)) == len(dts)

    def test_every_m4_level_including_absence_appears(self):
        """A rule tests `addr_match eq "(absent)"`.

        If the fixture never produced an absent M4 that rule would go
        untested, which is how the vesta_/addr_match family went inert before.
        """
        values = {r["M4"] for r in _rows(SAMPLE)}
        assert values == {"M0", "M1", "M2", ""}

    def test_some_rows_have_no_identity_row_at_all(self):
        matched = {r["TransactionID"] for r in _rows(SAMPLE_IDENTITY)}
        all_ids = {r["TransactionID"] for r in _rows(SAMPLE)}
        assert 0 < len(matched) < len(all_ids)

    def test_some_identity_rows_carry_no_device_columns(self):
        """"Has identity data" and "has a device" are different questions.

        3,373 real rows join an identity record whose device columns are all
        blank. mapping.device_entity has a branch for exactly that, and it is
        only exercised if the fixture contains one.
        """
        device_columns = ("DeviceInfo", "DeviceType", "id_30", "id_31", "id_33")
        blank = [r for r in _rows(SAMPLE_IDENTITY)
                 if all(r[c] == "" for c in device_columns)]
        assert len(blank) >= 2

    def test_some_identity_rows_do_carry_device_columns(self):
        populated = [r for r in _rows(SAMPLE_IDENTITY) if r["DeviceType"] != ""]
        assert populated

    def test_a_card_key_repeats(self):
        """Entity resolution is only exercised when a card appears twice."""
        columns = ("card1", "card2", "card3", "card5", "card6")
        keys = [tuple(r[c] for c in columns) for r in _rows(SAMPLE)]
        assert len(keys) > len(set(keys))

    def test_blank_fields_exist_so_null_handling_is_exercised(self):
        rows = _rows(SAMPLE)
        assert any(r["addr1"] == "" for r in rows)
        assert any(r["dist2"] == "" for r in rows)

    def test_the_fraud_rate_is_plausible(self):
        fraud = sum(1 for r in _rows(SAMPLE) if r["isFraud"] == "1")
        # Small sample, so a wide band -- the point is that both classes are
        # present, not that 100 rows reproduce a 3.499% base rate.
        assert 0 < fraud <= 12

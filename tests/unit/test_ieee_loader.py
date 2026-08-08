"""T1 acceptance: loads the fixture, yields correctly typed dicts, raises on
out-of-order timestamps.

The fixture is 100 rows of *synthetic* data carrying the real schema. The
real CSVs are never committed (plan §1.1), so every assertion here is about
shape and typing, and the measured facts about the real file live in the
loader's own docstring.
"""

import csv
from pathlib import Path

import pytest

from fraud_engine.config import get_settings
from scripts.ieee.loader import (
    IDENTITY_FILENAME,
    TRANSACTION_FILENAME,
    identity_csv,
    load_transactions,
    resolve_data_path,
    transaction_csv,
)
from scripts.ieee.make_fixtures import FIRST_ID

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "ieee_sample.csv"
SAMPLE_IDENTITY = FIXTURES / "ieee_sample_identity.csv"


def _rows(identity: bool = True):
    return list(load_transactions(SAMPLE, SAMPLE_IDENTITY if identity else None))


class TestDataPathResolution:
    @pytest.fixture(autouse=True)
    def _fresh_settings(self):
        """Settings is lru_cached, so an env change alone would not be seen.

        The cache is what makes .env parse once per process rather than once
        per request; here it means a monkeypatched variable is invisible
        until the cache is dropped. Cleared afterwards too, so a test that
        set a fake path cannot leak it into the rest of the session.
        """
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_an_unconfigured_path_raises_rather_than_guessing(self, monkeypatch):
        # §5.7 of CLAUDE.md: missing configuration is an error. A default
        # path here would silently read the wrong file or an empty one.
        #
        # The SETTING is cleared, not the environment variable. Settings
        # reads .env as well as os.environ, so deleting the variable proves
        # nothing on a machine where .env supplies it -- which is every
        # machine that can actually run a replay.
        monkeypatch.setattr(get_settings(), "ieee_data_path", None)
        with pytest.raises(RuntimeError, match="IEEE_DATA_PATH"):
            resolve_data_path()

    def test_the_two_filenames_hang_off_the_configured_directory(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "ieee_data_path", "/somewhere/ieee")
        assert transaction_csv() == Path("/somewhere/ieee") / TRANSACTION_FILENAME
        assert identity_csv() == Path("/somewhere/ieee") / IDENTITY_FILENAME

    def test_an_explicit_base_does_not_need_the_setting(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "ieee_data_path", None)
        assert transaction_csv(Path("/tmp/x")).name == TRANSACTION_FILENAME

    def test_only_the_labelled_training_split_is_addressable(self):
        # test_transaction.csv has no isFraud column (§1.3). Naming it here
        # would be an invitation to score it and report the result.
        assert "train" in TRANSACTION_FILENAME
        assert "test" not in TRANSACTION_FILENAME


class TestLoading:
    def test_every_fixture_row_is_yielded(self):
        assert len(_rows()) == 100

    def test_the_v_columns_are_dropped_at_the_csv_boundary(self):
        # §3.5: 339 undisclosed features. Dropping them here rather than
        # "remembering not to use them" is what makes that guarantee hold.
        header = SAMPLE.read_text().splitlines()[0].split(",")
        assert [c for c in header if c.startswith("V")], "fixture must contain V columns to drop"
        assert not [k for k in _rows()[0] if k.startswith("V")]

    def test_rows_arrive_in_file_order(self):
        ids = [r["TransactionID"] for r in _rows()]
        assert ids == sorted(ids)


class TestTyping:
    def test_identifiers_and_the_label_are_integers(self):
        row = _rows()[0]
        for column in ("TransactionID", "TransactionDT", "isFraud"):
            assert isinstance(row[column], int), column

    def test_amounts_and_counters_are_floats(self):
        row = _rows()[0]
        for column in ("TransactionAmt", "card1", "C1", "D1"):
            assert isinstance(row[column], float), column

    def test_categoricals_stay_strings(self):
        row = _rows()[0]
        for column in ("ProductCD", "card4", "card6", "P_emaildomain"):
            assert isinstance(row[column], str), column

    def test_a_blank_field_becomes_none_not_an_empty_string(self):
        # "" and None both look falsy and mean different things downstream:
        # _is_absent in mapping.py treats them alike precisely because this
        # boundary is the only place the distinction is reliably known.
        rows = _rows()
        assert any(r["addr1"] is None for r in rows)
        assert not any(r["addr1"] == "" for r in rows)

    def test_numeric_identity_columns_are_not_left_as_strings(self):
        # id_01-id_11 are numeric in the real file but id_12, id_15, id_16
        # and id_23 are not, despite sitting in the same range.
        row = next(r for r in _rows() if r["id_01"] is not None)
        assert isinstance(row["id_01"], float)

    def test_categorical_identity_columns_stay_strings(self):
        row = next(r for r in _rows() if r["id_31"] is not None)
        assert isinstance(row["id_31"], str)
        assert isinstance(row["DeviceInfo"], str)


class TestIdentityJoin:
    def _identity_by_id(self) -> dict[str, dict[str, str]]:
        with open(SAMPLE_IDENTITY, newline="") as f:
            return {r["TransactionID"]: r for r in csv.DictReader(f)}

    def test_a_matching_identity_row_is_merged_in(self):
        """Rows are chosen by PROPERTY, never by a hardcoded id.

        This test used to name one TransactionID from the old fixture and
        assert a literal device string against it -- pinning the test to a
        single row whose id sat inside the licensed dataset's range. It now
        finds a row that has an identity match and checks the merged values
        against the identity file itself, which tests the join rather than a
        constant, and survives any regeneration of the fixture.

        No id in the real range appears anywhere in this file, so a plain grep
        is enough to audit it.
        """
        identity = self._identity_by_id()
        with_device = {
            tid: r for tid, r in identity.items() if r["DeviceType"] != ""
        }
        assert with_device, "fixture must contain an identity row carrying a device"

        merged = next(
            r for r in _rows() if str(r["TransactionID"]) in with_device
        )
        source = with_device[str(merged["TransactionID"])]
        assert merged["DeviceType"] == source["DeviceType"]
        assert merged["id_33"] == source["id_33"]
        assert merged["DeviceInfo"] == source["DeviceInfo"]

    def test_identity_columns_are_present_and_none_when_there_is_no_match(self):
        # A left join, not an inner one: callers must never have to tell
        # "key absent" from "value unknown" (the dict.get trap in CLAUDE.md).
        matched = set(self._identity_by_id())
        row = next(r for r in _rows() if str(r["TransactionID"]) not in matched)
        assert row["DeviceType"] is None
        assert "DeviceInfo" in row and "id_33" in row

    def test_not_every_row_matches(self):
        rows = _rows()
        matched = [r for r in rows if r["DeviceType"] is not None]
        assert 0 < len(matched) < len(rows)

    def test_without_an_identity_path_no_identity_columns_appear(self):
        assert "DeviceType" not in _rows(identity=False)[0]

    def test_the_shared_empty_identity_row_is_not_aliased_between_rows(self):
        # Every unmatched row fills from one dict. If that dict were shared
        # by reference, mutating one record would rewrite the others.
        rows = [r for r in _rows() if r["DeviceType"] is None]
        rows[0]["DeviceType"] = "mutated"
        assert rows[1]["DeviceType"] is None


class TestOrdering:
    def _write(self, tmp_path: Path, dts: list[int]) -> Path:
        path = tmp_path / "tx.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["TransactionID", "isFraud", "TransactionDT", "TransactionAmt"])
            for i, dt in enumerate(dts):
                w.writerow([FIRST_ID + i, 0, dt, "10.00"])
        return path

    def test_a_decreasing_timestamp_raises(self, tmp_path):
        # §1.2/§4.2: D1-D15 are timedeltas since a previous event, so replaying
        # out of order leaks future information backwards. The loader refuses
        # rather than sorting, because a loader that quietly fixes the input
        # hides a data problem the caller needs to see.
        path = self._write(tmp_path, [100, 200, 150])
        with pytest.raises(ValueError, match="out of order"):
            list(load_transactions(path))

    def test_the_error_names_the_row_and_both_timestamps(self, tmp_path):
        path = self._write(tmp_path, [100, 200, 150])
        with pytest.raises(ValueError) as excinfo:
            list(load_transactions(path))
        message = str(excinfo.value)
        assert str(FIRST_ID + 2) in message and "150" in message and "200" in message

    def test_equal_timestamps_are_allowed(self, tmp_path):
        # The requirement is non-decreasing, not strictly increasing: the real
        # file has many transactions sharing a second.
        path = self._write(tmp_path, [100, 100, 100])
        assert len(list(load_transactions(path))) == 3

    def test_rows_before_the_bad_one_are_still_yielded(self, tmp_path):
        # The loader is a generator, so it fails at the offending row rather
        # than validating up front. Callers see the good prefix.
        path = self._write(tmp_path, [100, 200, 150])
        seen = []
        with pytest.raises(ValueError):
            for row in load_transactions(path):
                seen.append(row["TransactionID"])
        assert seen == [FIRST_ID, FIRST_ID + 1]

    def test_the_committed_fixture_is_itself_in_order(self):
        dts = [r["TransactionDT"] for r in _rows()]
        assert dts == sorted(dts)


class TestMalformedInput:
    def test_a_headerless_file_raises_a_message_naming_the_file(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("")
        with pytest.raises(ValueError, match="no header row"):
            list(load_transactions(path))

    def test_a_header_with_no_rows_yields_nothing(self, tmp_path):
        path = tmp_path / "headers_only.csv"
        path.write_text("TransactionID,isFraud,TransactionDT,TransactionAmt\n")
        assert list(load_transactions(path)) == []

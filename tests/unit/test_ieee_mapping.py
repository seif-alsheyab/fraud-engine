"""T2 acceptance (plan §7):

  (a) the same card columns produce the same entity
  (b) missing device columns produce None and NOT a shared hash
  (c) `P_emaildomain` never becomes an EMAIL entity

Plus the two failure modes §3.3/§3.4 name explicitly, both of which are
silent: they do not raise, they produce one enormous entity that makes every
velocity and linking rule fire on everything.
"""

import re
from pathlib import Path

import pytest

from fraud_engine.domain.entities import hash_value
from scripts.ieee.loader import load_transactions
from scripts.ieee.mapping import (
    ACCOUNT_ENTITY_TYPE,
    CARD_ENTITY_TYPE,
    DEVICE_ENTITY_TYPE,
    EMPTY_FEATURE_SLOTS,
    FEATURE_SLOT,
    account_entity,
    card_entity,
    device_entity,
    email_domain,
    entities_for_row,
)

SALT = "test_salt_at_least_16_chars"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES / "ieee_sample.csv"
SAMPLE_IDENTITY = FIXTURES / "ieee_sample_identity.csv"


def row(**overrides):
    """A row with every key column present, so each test varies one thing."""
    base = {
        "card1": 4648.0,
        "card2": 112.0,
        "card3": 150.0,
        "card5": 224.0,
        "card6": "credit",
        "addr1": 87.0,
        "P_emaildomain": "gmail.com",
        "DeviceInfo": "Windows",
        "DeviceType": "desktop",
        "id_30": "Windows 10",
        "id_31": "chrome 62.0",
        "id_33": "1366x768",
    }
    return base | overrides


DEVICELESS = row(DeviceInfo=None, DeviceType=None, id_30=None, id_31=None, id_33=None)


class TestCardEntityIsStable:
    def test_the_same_card_columns_produce_the_same_entity(self):
        assert card_entity(row(), SALT) == card_entity(row(), SALT)

    def test_columns_outside_the_key_do_not_change_the_card(self):
        # The same card used at a different address is still the same card.
        assert card_entity(row(), SALT) == card_entity(row(addr1=999.0), SALT)

    @pytest.mark.parametrize("column", ["card1", "card2", "card3", "card5"])
    def test_a_different_value_in_any_key_column_is_a_different_card(self, column):
        assert card_entity(row(), SALT) != card_entity(row(**{column: 1.0}), SALT)

    def test_credit_and_debit_are_different_cards(self):
        # §3.2 states this outright, and it is the assertion that catches the
        # real bug: routing the composite key through the CARD normaliser
        # strips every non-digit, deleting "credit"/"debit" entirely and
        # merging the two into one entity.
        assert card_entity(row(card6="credit"), SALT) != card_entity(row(card6="debit"), SALT)

    def test_the_separator_is_not_stripped_from_the_key(self):
        # Without separators, ("4648","112") and ("46","48112") are one key.
        # This is the second half of the same normaliser bug.
        left = row(card1=4648.0, card2=112.0)
        right = row(card1=46.0, card2=48112.0)
        assert card_entity(left, SALT) != card_entity(right, SALT)

    def test_the_entity_survives_a_change_of_csv_reader(self):
        # This loader types card1 as float (4648.0); pandas gives int (4648)
        # and a raw csv reader gives str ("4648"). If these disagreed,
        # swapping the reader would silently re-partition every card.
        as_float = row(card1=4648.0, card2=112.0)
        as_int = row(card1=4648, card2=112)
        as_str = row(card1="4648", card2="112")
        assert card_entity(as_float, SALT) == card_entity(as_int, SALT) == card_entity(as_str, SALT)

    def test_a_fractional_value_is_not_rounded_into_its_neighbour(self):
        assert card_entity(row(card2=112.5), SALT) != card_entity(row(card2=112.0), SALT)

    def test_a_different_salt_gives_a_different_entity(self):
        assert card_entity(row(), SALT) != card_entity(row(), "some_other_salt_value")

    def test_a_row_with_no_card_identity_at_all_raises(self):
        # Never occurs in the real file (card1 is populated in all 590,540
        # rows), so this guards a replacement loader. Raising beats one
        # fabricated card absorbing the velocity of every broken row.
        empty = row(card1=None, card2=None, card3=None, card5=None, card6=None)
        with pytest.raises(ValueError, match="no card identity"):
            card_entity(empty, SALT)


class TestDeviceAbsenceIsNotAnEntity:
    def test_missing_device_columns_produce_none(self):
        assert device_entity(DEVICELESS, SALT) is None

    def test_two_different_deviceless_rows_do_not_share_an_entity(self):
        # The failure this prevents: hashing "None|None|None|None|None" gives
        # every one of the 449,680 device-less real rows (76.1%) the same
        # device, and every device-linking rule then fires on nearly all
        # traffic. Both being None is the point -- there is no shared hash.
        a = device_entity(DEVICELESS, SALT)
        b = device_entity(DEVICELESS | {"card1": 999.0, "addr1": 1.0}, SALT)
        assert a is None and b is None

    def test_a_row_that_joined_identity_but_has_no_device_columns_is_still_none(self):
        # 3,373 real rows match an identity record whose five device columns
        # are all blank. "Has identity data" and "has a device" are different
        # questions; only the second one decides this.
        joined_but_deviceless = DEVICELESS | {"id_01": 12.4, "id_02": 301.5, "id_12": "NotFound"}
        assert device_entity(joined_but_deviceless, SALT) is None

    @pytest.mark.parametrize("blank", [None, "", "   ", "nan", "NaN", "None", "null"])
    def test_every_spelling_of_missing_is_treated_as_missing(self, blank):
        # "nan" is not paranoia: the plan's own §4.2 sketch reads the file
        # with pandas, where a missing cell str()s to exactly that -- the
        # literal string the §3.4 warning is named after.
        all_blank = {c: blank for c in ("DeviceInfo", "DeviceType", "id_30", "id_31", "id_33")}
        assert device_entity(row(**all_blank), SALT) is None

    def test_absent_identity_columns_are_treated_as_missing_not_looked_up(self):
        # load_transactions(identity_path=None) omits the columns entirely.
        assert device_entity({"card1": 4648.0}, SALT) is None

    def test_a_partially_present_device_still_hashes(self):
        # 69,740 real identity rows have some device columns filled and
        # others blank. Returning None for those would discard the signal on
        # nearly half of all identity data.
        partial = DEVICELESS | {"id_31": "chrome 62.0", "DeviceType": "desktop"}
        assert device_entity(partial, SALT) is not None

    def test_partial_rows_are_distinguished_by_which_column_is_present(self):
        left = DEVICELESS | {"id_31": "chrome 62.0"}
        right = DEVICELESS | {"id_30": "chrome 62.0"}
        assert device_entity(left, SALT) != device_entity(right, SALT)

    def test_other_is_a_real_category_and_not_a_missing_marker(self):
        # "other" appears 327 times across real id_30/id_31. Treating it as
        # absent would throw away a genuine value.
        assert device_entity(DEVICELESS | {"id_31": "other"}, SALT) is not None

    def test_the_same_device_columns_produce_the_same_entity(self):
        assert device_entity(row(), SALT) == device_entity(row(card1=1.0), SALT)


class TestEmailDomainIsNeverAnEntity:
    def test_the_account_entity_is_not_a_hash_of_the_email_domain(self):
        # The §3.3 trap: gmail.com covers hundreds of thousands of rows, so
        # an EMAIL entity built from it would count every Gmail user in the
        # dataset as one customer.
        assert account_entity(row(), SALT) != hash_value("EMAIL", "gmail.com", SALT)

    def test_two_gmail_users_are_not_the_same_account(self):
        # The property that matters, stated without reference to hashing:
        # sharing a mail provider must not make two people one customer.
        a = account_entity(row(card1=1111.0, addr1=10.0, P_emaildomain="gmail.com"), SALT)
        b = account_entity(row(card1=2222.0, addr1=20.0, P_emaildomain="gmail.com"), SALT)
        assert a != b

    def test_no_entity_here_is_typed_email(self):
        for entity_type in (CARD_ENTITY_TYPE, ACCOUNT_ENTITY_TYPE, DEVICE_ENTITY_TYPE):
            assert "EMAIL" not in entity_type

    def test_entities_for_a_row_never_include_an_email(self):
        # A code path that cannot produce an EMAIL entity is a stronger
        # guarantee than a review that noticed it did not.
        assert set(entities_for_row(row(), SALT)) == {
            CARD_ENTITY_TYPE,
            ACCOUNT_ENTITY_TYPE,
            DEVICE_ENTITY_TYPE,
        }

    def test_no_feature_slot_is_an_email_or_ip_slot(self):
        assert set(FEATURE_SLOT.values()) == {"CARD", "ACCOUNT", "DEVICE"}

    def test_the_domain_is_kept_as_a_plain_attribute(self):
        # §3.3: store it for a future email_domain_risk feature. A domain is
        # not a mailbox, so there is nothing here to pseudonymise.
        assert email_domain(row()) == "gmail.com"
        assert email_domain(row(P_emaildomain="GMail.Com ")) == "gmail.com"

    def test_a_missing_domain_is_none(self):
        assert email_domain(row(P_emaildomain=None)) is None
        assert email_domain(row(P_emaildomain="nan")) is None


class TestAccountEntity:
    def test_the_same_triple_produces_the_same_account(self):
        assert account_entity(row(), SALT) == account_entity(row(), SALT)

    @pytest.mark.parametrize("column", ["card1", "addr1", "P_emaildomain"])
    def test_each_key_column_changes_the_account(self, column):
        other = {"card1": 1.0, "addr1": 2.0, "P_emaildomain": "outlook.com"}[column]
        assert account_entity(row(), SALT) != account_entity(row(**{column: other}), SALT)

    def test_a_card_shared_by_two_addresses_is_two_accounts(self):
        # This is what makes accounts_per_card_30d able to count anything.
        assert account_entity(row(addr1=87.0), SALT) != account_entity(row(addr1=88.0), SALT)

    def test_an_account_is_not_the_same_entity_as_its_card(self):
        # Distinct entity types keep identical keys from colliding across
        # kinds -- the same guarantee hash_value gives EMAIL versus PHONE.
        assert account_entity(row(), SALT) != card_entity(row(), SALT)


class TestFeatureSlotContract:
    """The translation T6 depends on. A wrong slot name does not raise -- the
    feature comes back null and the rule silently never fires, which is the
    same signature as the null-collapse bug and the opposite cause."""

    def test_every_entity_type_produced_has_a_slot(self):
        assert set(entities_for_row(row(), SALT)) == set(FEATURE_SLOT)

    def test_the_slots_are_the_ones_feature_service_actually_reads(self):
        # Read the real keys out of feature_service rather than restating
        # them here, so this fails if the service is renamed underneath us
        # (CLAUDE.md §7: test the contract, not your assumptions).
        source = (
            Path(__file__).resolve().parents[2]
            / "src/fraud_engine/services/feature_service.py"
        ).read_text()
        read_keys = set(re.findall(r'entity_ids\.get\("(\w+)"\)', source))
        assert read_keys, "feature_service no longer reads entity_ids by name"
        assert set(FEATURE_SLOT.values()) <= read_keys
        assert set(EMPTY_FEATURE_SLOTS) == read_keys

    def test_the_empty_slots_are_full_width_so_no_key_is_merely_absent(self):
        # list_active_list_entries iterates entity_ids.values(); a short dict
        # would quietly narrow the list lookup instead of failing.
        assert len(EMPTY_FEATURE_SLOTS) == 5
        assert all(v is None for v in EMPTY_FEATURE_SLOTS.values())

    def test_filling_the_slots_leaves_email_and_ip_explicitly_none(self):
        filled = dict(EMPTY_FEATURE_SLOTS)
        for entity_type, value in entities_for_row(row(), SALT).items():
            filled[FEATURE_SLOT[entity_type]] = value
        assert filled["EMAIL"] is None and filled["IP"] is None
        assert filled["CARD"] and filled["ACCOUNT"] and filled["DEVICE"]


class TestProxiesAreNotRealIdentifiers:
    def test_the_entity_types_are_namespaced_away_from_real_ones(self):
        # `entities` is keyed (entity_type, value_hash). A reconstructed
        # proxy must never be read or reported as though it came from a real
        # card number, so it gets its own type rather than borrowing CARD.
        assert CARD_ENTITY_TYPE == "IEEE_CARD"
        assert card_entity(row(), SALT) != hash_value("CARD", "4648|112|150|224|credit", SALT)

    def test_the_hash_does_not_leak_the_key(self):
        assert "4648" not in card_entity(row(), SALT)

    def test_entities_are_fixed_length_hex_digests(self):
        for value in entities_for_row(row(), SALT).values():
            assert len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class TestAgainstTheLoader:
    """Rows straight from the loader, not hand-built dicts -- the mapping has
    to hold against what the CSV boundary actually produces."""

    def _rows(self):
        return list(load_transactions(SAMPLE, SAMPLE_IDENTITY))

    def test_every_fixture_row_yields_a_card_and_an_account(self):
        for record in self._rows():
            entities = entities_for_row(record, SALT)
            assert entities[CARD_ENTITY_TYPE] is not None
            assert entities[ACCOUNT_ENTITY_TYPE] is not None

    def test_device_is_none_for_exactly_the_rows_with_no_device_signal(self):
        columns = ("DeviceInfo", "DeviceType", "id_30", "id_31", "id_33")
        for record in self._rows():
            expected_none = all(record[c] is None for c in columns)
            assert (device_entity(record, SALT) is None) is expected_none

    def test_the_fixture_covers_both_device_cases(self):
        # A test asserting a property no fixture row exercises documents
        # nothing. The identity fixture carries the two real shapes: a row
        # that joins identity with no device columns, and a partial one.
        devices = [device_entity(r, SALT) for r in self._rows()]
        assert any(d is None for d in devices)
        assert any(d is not None for d in devices)

    def test_no_two_different_devices_collapse_into_one_entity(self):
        columns = ("DeviceInfo", "DeviceType", "id_30", "id_31", "id_33")
        by_entity: dict[str, set[tuple]] = {}
        for record in self._rows():
            entity = device_entity(record, SALT)
            if entity is not None:
                by_entity.setdefault(entity, set()).add(tuple(record[c] for c in columns))
        assert by_entity, "fixture should produce at least one device entity"
        collapsed = {e: keys for e, keys in by_entity.items() if len(keys) > 1}
        assert not collapsed, f"distinct device columns share an entity: {collapsed}"

    def test_repeated_card_keys_in_the_fixture_map_to_one_entity(self):
        columns = ("card1", "card2", "card3", "card5", "card6")
        by_key: dict[tuple, set[str]] = {}
        for record in self._rows():
            key = tuple(record[c] for c in columns)
            by_key.setdefault(key, set()).add(card_entity(record, SALT))
        repeated = [key for key, entities in by_key.items() if len(entities) > 1]
        assert not repeated, f"one card key produced several entities: {repeated}"
        assert max(
            sum(1 for r in self._rows() if tuple(r[c] for c in columns) == key) for key in by_key
        ) > 1, "fixture should contain a card used more than once"

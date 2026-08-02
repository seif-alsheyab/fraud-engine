from fraud_engine.domain.entities import (
    display_hint,
    hash_value,
    normalise_card,
    normalise_email,
    normalise_phone,
)

SALT = "test_salt_at_least_16_chars"


class TestNormalisation:
    def test_gmail_dots_and_plus_tags_are_one_mailbox(self):
        # These three strings reach the same inbox. Hashing them raw would
        # create three unrelated entities, and every velocity counter would
        # read zero for a returning customer.
        a = normalise_email("Sief@Gmail.com")
        b = normalise_email("s.ief@gmail.com")
        c = normalise_email("sief+shopping@gmail.com")
        assert a == b == c == "sief@gmail.com"

    def test_dots_are_preserved_outside_gmail(self):
        # Only Google treats dots as insignificant. Stripping them elsewhere
        # would merge two genuinely different mailboxes into one entity.
        assert normalise_email("first.last@company.com") == "first.last@company.com"

    def test_plus_tags_are_stripped_everywhere(self):
        assert normalise_email("user+tag@company.com") == "user@company.com"

    def test_card_formatting_is_not_identity(self):
        assert normalise_card("4111 1111 1111 1111") == "4111111111111111"
        assert normalise_card("4111-1111-1111-1111") == "4111111111111111"

    def test_phone_normalises_to_e164_shape(self):
        assert normalise_phone("+962 79 530 3335") == "+962795303335"
        assert normalise_phone("(962) 79-530-3335") == "+962795303335"


class TestHashing:
    def test_the_same_card_hashes_the_same_way(self):
        assert hash_value("CARD", "4111 1111 1111 1111", SALT) == hash_value(
            "CARD", "4111111111111111", SALT
        )

    def test_different_cards_hash_differently(self):
        assert hash_value("CARD", "4111111111111111", SALT) != hash_value(
            "CARD", "4111111111111112", SALT
        )

    def test_a_different_salt_produces_a_different_hash(self):
        # This is what makes a precomputed table of every card number
        # useless against the database.
        assert hash_value("CARD", "4111111111111111", SALT) != hash_value(
            "CARD", "4111111111111111", "a_different_salt_value"
        )

    def test_entity_type_is_mixed_in_so_types_cannot_collide(self):
        assert hash_value("EMAIL", "same", SALT) != hash_value("DEVICE", "same", SALT)

    def test_the_hash_does_not_contain_the_input(self):
        pan = "4111111111111111"
        assert pan not in hash_value("CARD", pan, SALT)

    def test_the_hash_is_a_fixed_length_hex_digest(self):
        h = hash_value("CARD", "4111111111111111", SALT)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestDisplayHint:
    def test_card_hint_is_last_four_only(self):
        assert display_hint("CARD", "4111 1111 1111 1234") == "1234"

    def test_email_hint_is_the_domain_only(self):
        assert display_hint("EMAIL", "sief@example.com") == "example.com"

    def test_ip_hint_is_masked(self):
        assert display_hint("IP", "192.168.14.7") == "192.168.x.x"

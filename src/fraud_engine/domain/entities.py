"""Entity normalisation and pseudonymisation.

Two jobs, and the order matters:

  1. NORMALISE, so the same real-world thing produces the same string.
     "Sief@Gmail.com", "sief@gmail.com" and "s.ief+shop@gmail.com" are one
     mailbox. Hash them raw and you get three unrelated entities, and every
     velocity counter reads zero for a repeat customer.

  2. HASH with a secret salt, so the database never holds the value.
     The engine does not need to KNOW the card number -- only to recognise
     that this is the SAME card as before. A stable hash does that exactly
     as well, without turning the database into cardholder data.

Why the salt is not optional: card numbers have low entropy (16 digits, of
which the BIN is known and the last is a checksum). An unsalted hash of
every possible card is computable in advance. With a secret salt it is not.
"""

import hashlib
import re

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
_NON_DIGITS = re.compile(r"\D")


def normalise_email(value: str) -> str:
    """Lowercase, strip plus-tags, and drop dots on Gmail-family domains."""
    value = value.strip().lower()
    if "@" not in value:
        return value
    local, _, domain = value.partition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def normalise_card(value: str) -> str:
    """Digits only. Spaces and dashes are formatting, not identity."""
    return _NON_DIGITS.sub("", value)


def normalise_phone(value: str) -> str:
    """Digits only, with a leading + preserved for E.164."""
    digits = _NON_DIGITS.sub("", value)
    return f"+{digits}" if digits else ""


def normalise(entity_type: str, value: str) -> str:
    if entity_type == "EMAIL":
        return normalise_email(value)
    if entity_type == "CARD":
        return normalise_card(value)
    if entity_type == "PHONE":
        return normalise_phone(value)
    return value.strip().lower()


def hash_value(entity_type: str, value: str, salt: str) -> str:
    """Salted sha256 of the normalised value.

    The entity type is mixed in so an email and a phone number that happen
    to share a string can never collide into one entity.
    """
    normalised = normalise(entity_type, value)
    payload = f"{salt}|{entity_type}|{normalised}".encode()
    return hashlib.sha256(payload).hexdigest()


def display_hint(entity_type: str, value: str) -> str:
    """A fragment safe to show a human and useless to an attacker."""
    if entity_type == "CARD":
        digits = normalise_card(value)
        return digits[-4:] if len(digits) >= 4 else ""
    if entity_type == "EMAIL":
        return normalise_email(value).partition("@")[2]
    if entity_type == "IP":
        parts = value.split(".")
        return f"{parts[0]}.{parts[1]}.x.x" if len(parts) == 4 else ""
    return ""

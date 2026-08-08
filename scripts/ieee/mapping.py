"""IEEE-CIS entity reconstruction (plan §3.2-3.4).

IEEE-CIS has no real card, account or device identifiers -- only anonymised
columns that the competition consensus treats as stand-ins for them. Every
function here builds a *proxy* entity, not ground truth, and each is an
approximation for the reason given in its docstring.

Entities are hashed through `fraud_engine.domain.entities.hash_value`, the
same salted-SHA-256 path real payment identifiers go through (CLAUDE.md
§5.1) -- an IEEE card key is exactly as sensitive an input as a real PAN
would be, so it gets exactly the same treatment.

Why the entity types are `IEEE_*` and not `CARD` / `ACCOUNT` / `DEVICE`
----------------------------------------------------------------------
Two reasons, one of which is a bug that this module previously had.

1. `hash_value` picks a normaliser from the entity type, and `normalise`
   for `CARD` is *digits only* -- correct for a real PAN, destructive for a
   composite key. `hash_value("CARD", "4648|112|150|224|credit", salt)`
   normalises to `"46481121502 24"`-style digits, which

     * silently deletes the `credit`/`debit` token, so the credit and debit
       cards that §3.2 explicitly calls "different cards" collapse into one
       entity, and
     * deletes the separators, so `("4648","112")` and `("46","48112")`
       hash identically.

   The `IEEE_*` types fall through to the default normaliser, which strips
   and lowercases and preserves the key exactly as composed.

2. A reconstructed proxy is not the same kind of thing as an entity built
   from a real card number, and `entities` is keyed `(entity_type,
   value_hash)`. Distinct types keep a proxy from ever being read, linked
   or reported as though it were the real identifier.

   NOTE for T3: these three codes need rows in `entity_types`, which is a
   foreign key. Seeding them belongs with the other reference data.

Every component is rendered canonically before hashing (CLAUDE.md §5.2:
normalise *before* you hash). The loader here types `card1` as a float, so
it arrives as `4648.0`; a pandas-based loader would produce `4648` and a raw
`csv` reader `"4648"`. All three must yield one entity, or swapping the
reader silently re-partitions every card in the dataset.
"""

from typing import Any

from fraud_engine.domain.entities import hash_value

CARD_ENTITY_TYPE = "IEEE_CARD"
ACCOUNT_ENTITY_TYPE = "IEEE_ACCOUNT"
DEVICE_ENTITY_TYPE = "IEEE_DEVICE"

_CARD_KEY_COLUMNS = ("card1", "card2", "card3", "card5", "card6")
_ACCOUNT_KEY_COLUMNS = ("card1", "addr1", "P_emaildomain")
_DEVICE_KEY_COLUMNS = ("DeviceInfo", "DeviceType", "id_30", "id_31", "id_33")

# `|` is safe as a separator: no value in any of these columns contains one
# (checked across all 144,233 real identity rows).
_SEPARATOR = "|"

# Strings that mean "no value" rather than being one. `nan` is here because
# the plan's own §4.2 sketch reads the CSV with pandas, where a missing cell
# becomes float('nan') and `str()` renders it "nan" -- the literal string the
# §3.4 warning is named after. Treating it as data is how you get the
# "nan|nan|nan|nan|nan" entity that swallows three quarters of the dataset.
#
# `other` is deliberately NOT in this set: it is a real, frequent category in
# `id_30` and `id_31` (327 real rows), not a missing marker.
_ABSENT_TOKENS = frozenset({"", "nan", "none", "null", "n/a"})


def _is_absent(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in _ABSENT_TOKENS


def _key_component(value: Any) -> str:
    """Render one key column so that equal values agree across loaders.

    Integer-valued floats lose their `.0`, because every numeric column used
    in a key below is integer-valued in the real dataset (verified: card1
    <= 18396, card2 <= 600, card3 <= 231, card5 <= 237, addr1 <= 540, no
    fractional values). A non-integral float is still rendered faithfully
    rather than rounded -- silently merging 112.5 into 112 would be the same
    class of error this function exists to prevent.
    """
    if _is_absent(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _compose_key(row: dict[str, Any], columns: tuple[str, ...]) -> str | None:
    """The joined key, or None when every component is absent.

    None is the caller's signal that there is no identity here at all. What
    to do about that differs by entity type, so this function does not
    decide: see §3.4 versus §3.2.
    """
    parts = [_key_component(row.get(c)) for c in columns]
    if all(p == "" for p in parts):
        return None
    return _SEPARATOR.join(parts)


def card_key(row: dict[str, Any]) -> str | None:
    """The composite card key, unhashed. None when every column is absent.

    Exists so the replay can hand decide_payment a KEY and let it do the
    hashing, rather than hashing here and having decide_payment hash the
    result again. A hash of a hash is stable and consistent, which is
    precisely why it would never be noticed.
    """
    return _compose_key(row, _CARD_KEY_COLUMNS)


def account_key(row: dict[str, Any]) -> str | None:
    """The composite account key, unhashed. See card_key."""
    return _compose_key(row, _ACCOUNT_KEY_COLUMNS)


def device_key(row: dict[str, Any]) -> str | None:
    """The composite device key, unhashed, or None when there is no device
    signal at all -- 76.1% of the real rows. See device_entity for why None
    rather than a hash of the rendered blanks."""
    return _compose_key(row, _DEVICE_KEY_COLUMNS)


def card_entity(row: dict[str, Any], salt: str) -> str:
    """`card1` alone is not a card -- it collides across genuinely different
    cards. The stable combination of columns is the best available proxy for
    card identity. `card6` (credit/debit) is included in the key, not just
    carried as an attribute: a credit and a debit card that happen to share
    `card1` are different cards, not the same card with two products.

    Raises when every card column is absent. That never happens in the real
    file (`card1` is populated in all 590,540 rows), so this guards against a
    replacement loader rather than the data -- and a raise is right, because
    the alternative is one fabricated card entity accumulating the velocity
    of every broken row in the dataset.
    """
    key = _compose_key(row, _CARD_KEY_COLUMNS)
    if key is None:
        raise ValueError(
            f"no card identity in row: all of {_CARD_KEY_COLUMNS} are absent. "
            "Hashing this would merge every such row into one card entity."
        )
    return hash_value(CARD_ENTITY_TYPE, key, salt)


def account_entity(row: dict[str, Any], salt: str) -> str:
    """The "UID" heuristic: a pseudo-account built from card, billing address
    and email domain together.

    `P_emaildomain` is a domain, not an address -- `gmail.com` alone appears
    in hundreds of thousands of rows. Used by itself it would make every
    Gmail transaction in the dataset look like the same customer, and
    `velocity_email_24h`-style features would fire on essentially everything.
    It is therefore never hashed alone; it only ever contributes to this
    combined key.

    The proxy is weakest where the other two columns are missing: `addr1` is
    absent in 11.1% of rows and `P_emaildomain` in 16.0%, and a row missing
    both degenerates to a card-scoped account. That is a real limitation of
    the reconstruction and belongs in the results write-up, not hidden here.
    """
    key = _compose_key(row, _ACCOUNT_KEY_COLUMNS)
    if key is None:
        raise ValueError(
            f"no account identity in row: all of {_ACCOUNT_KEY_COLUMNS} are absent."
        )
    return hash_value(ACCOUNT_ENTITY_TYPE, key, salt)


def device_entity(row: dict[str, Any], salt: str) -> str | None:
    """None when the row carries no device signal at all -- 449,680 of the
    590,540 real rows (76.1%).

    That figure is not simply "the 75.6% with no identity match". 3,373 rows
    *do* join to an identity record whose five device columns are all blank,
    so "has identity data" and "has a device" are different questions and
    only the second one is asked here.

    Absence must return None rather than hash the rendered key, which would
    be a single fabricated device shared by three quarters of the dataset --
    every device-linking rule would then fire on nearly every transaction.

    Partial presence is the common case, not an edge case: 69,740 identity
    rows have some device columns filled and others blank. Those hash
    normally, on whatever signal is there.
    """
    key = _compose_key(row, _DEVICE_KEY_COLUMNS)
    if key is None:
        return None
    return hash_value(DEVICE_ENTITY_TYPE, key, salt)


def email_domain(row: dict[str, Any]) -> str | None:
    """`P_emaildomain` as a plain attribute (§3.3), never an entity.

    This is the whole permitted use of the column: a low-cardinality
    categorical that a future `email_domain_risk` feature can score. It is
    returned in the clear on purpose -- a domain is not a mailbox, so there
    is nothing here to pseudonymise, and hashing it would only make the
    resulting rule unreadable.
    """
    value = row.get("P_emaildomain")
    return None if _is_absent(value) else str(value).strip().lower()


def entities_for_row(row: dict[str, Any], salt: str) -> dict[str, str | None]:
    """All three proxies for one row, keyed by entity type -> hash.

    Keyed by the type code that actually goes in the database, so this stays
    truthful about what it produced. It is deliberately NOT the shape
    `feature_service` consumes: that wants the engine's own slot names keyed
    to entity row UUIDs, and the hash -> UUID step is an upsert. Hashing is
    pure and upserting is I/O, so T6 owns the translation via FEATURE_SLOT
    rather than a database call appearing in a mapping module.

    Deliberately no EMAIL key. §3.3's trap is not a thing you avoid once in
    review; it is avoided by there being no code path that creates one.
    """
    return {
        CARD_ENTITY_TYPE: card_entity(row, salt),
        ACCOUNT_ENTITY_TYPE: account_entity(row, salt),
        DEVICE_ENTITY_TYPE: device_entity(row, salt),
    }


# The one translation between IEEE proxy types and the engine's feature slots.
# `feature_service` reads entity_ids.get("CARD"/"EMAIL"/"DEVICE"/"IP"/"ACCOUNT")
# and a missing key yields a null feature, which the condition evaluator treats
# as "does not match" -- so a slot named wrong here is not an error, it is a
# rule that silently never fires. One mapping, one place, greppable.
FEATURE_SLOT = {
    CARD_ENTITY_TYPE: "CARD",
    ACCOUNT_ENTITY_TYPE: "ACCOUNT",
    DEVICE_ENTITY_TYPE: "DEVICE",
}

# T6 must start from this and fill from FEATURE_SLOT, so that EMAIL and IP
# being absent is a recorded decision rather than a key that happens to be
# missing -- and so `list_active_list_entries`, which iterates
# entity_ids.values(), receives the full-width dict it expects.
EMPTY_FEATURE_SLOTS: dict[str, Any] = {
    "CARD": None,
    "EMAIL": None,
    "DEVICE": None,
    "IP": None,
    "ACCOUNT": None,
}

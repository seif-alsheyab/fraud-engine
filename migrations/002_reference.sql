-- Reference data: things that describe the world, not the traffic.

CREATE TABLE merchants (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code        TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  -- Risk appetite differs enormously by vertical. A digital goods seller
  -- ships instantly and cannot recall the goods, so it declines harder than
  -- a furniture shop with a two-week lead time.
  vertical    TEXT NOT NULL,
  country     CHAR(2) NOT NULL,
  currency    CHAR(3) NOT NULL,
  is_active   BOOLEAN NOT NULL DEFAULT true,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The kinds of thing we can track, list, and link on.
CREATE TABLE entity_types (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL
);

-- Card BIN reference. The first 6-8 digits identify the issuer, country,
-- brand and product. This is the ONLY part of a card number that is safe to
-- store in the clear -- it identifies a bank, not a person.
CREATE TABLE card_bins (
  bin           TEXT PRIMARY KEY,
  issuer_name   TEXT,
  issuer_country CHAR(2),
  brand         TEXT,          -- VISA, MASTERCARD, AMEX
  card_type     TEXT,          -- DEBIT, CREDIT, PREPAID
  -- Prepaid cards are disproportionately used in fraud: they can be bought
  -- with cash, carry no identity, and cannot be traced back to a person.
  is_prepaid    BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE decision_types (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,
  -- Was the payment allowed through, in some form?
  is_approval BOOLEAN NOT NULL,
  sort_order  INTEGER NOT NULL
);

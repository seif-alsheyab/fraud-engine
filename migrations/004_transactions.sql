-- Inbound payment events and the entities involved.

-- An entity is any identifier we track over time: a card, an email, a
-- device, an IP, an account.
--
-- The identifier itself is stored as a SALTED HASH, never in the clear.
-- Reasoning that belongs in an interview answer:
--   * A raw card number ("PAN") in a database makes that database a PCI-DSS
--     cardholder data environment, with everything that implies.
--   * The engine does not need to KNOW the card number. It only needs to
--     recognise that this is the SAME card as before. A stable hash does
--     that exactly as well.
--   * Salting stops a rainbow-table attack. Card numbers have low entropy
--     (16 digits with a checksum), so an unsalted hash of every possible
--     card is computable. With a secret salt it is not.
CREATE TABLE entities (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type   TEXT NOT NULL REFERENCES entity_types(code),
  -- sha256 hex of (salt || normalised value)
  value_hash    TEXT NOT NULL,
  -- Safe display fragment only: last4 for a card, domain for an email.
  -- Enough for a human to recognise a case, useless to an attacker.
  display_hint  TEXT,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Denormalised counter. Entity age and usage count are needed on EVERY
  -- decision, and counting transactions each time would not fit the latency
  -- budget.
  seen_count    BIGINT NOT NULL DEFAULT 0,
  UNIQUE (entity_type, value_hash)
);

CREATE INDEX idx_entities_last_seen ON entities (entity_type, last_seen_at);

CREATE TABLE transactions (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id        UUID NOT NULL REFERENCES merchants(id),
  -- The merchant's own reference. Unique per merchant so a retry of the
  -- same request cannot create a second transaction.
  external_id        TEXT NOT NULL,

  amount_minor       BIGINT NOT NULL CHECK (amount_minor > 0),
  currency           CHAR(3) NOT NULL,

  -- Card details: BIN and last4 only. Never the full number.
  card_bin           TEXT REFERENCES card_bins(bin),
  card_last4         CHAR(4),
  card_entity_id     UUID REFERENCES entities(id),

  email_entity_id    UUID REFERENCES entities(id),
  device_entity_id   UUID REFERENCES entities(id),
  ip_entity_id       UUID REFERENCES entities(id),
  account_entity_id  UUID REFERENCES entities(id),

  ip_address         INET,
  ip_country         CHAR(2),
  billing_country    CHAR(2),
  shipping_country   CHAR(2),

  -- Authentication and verification signals captured at authorisation.
  -- These CANNOT be collected later: if the AVS result was not stored at
  -- the time, it is gone. Same lesson as chargeback evidence.
  avs_match          TEXT CHECK (avs_match IN ('FULL','PARTIAL','NONE','UNAVAILABLE')),
  cvv_match          BOOLEAN,
  three_ds_status    TEXT CHECK (three_ds_status IN ('AUTHENTICATED','ATTEMPTED','FAILED','NOT_USED')),

  is_card_present    BOOLEAN NOT NULL DEFAULT false,
  channel            TEXT NOT NULL DEFAULT 'WEB' CHECK (channel IN ('WEB','MOBILE','API','POS')),
  occurred_at        TIMESTAMPTZ NOT NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (merchant_id, external_id)
);

-- Velocity queries always filter by one entity within a recent time window,
-- so the index leads with the entity and then time. Leading with time would
-- force a scan of every entity's rows inside that window.
CREATE INDEX idx_txn_card_time    ON transactions (card_entity_id, occurred_at DESC);
CREATE INDEX idx_txn_email_time   ON transactions (email_entity_id, occurred_at DESC);
CREATE INDEX idx_txn_device_time  ON transactions (device_entity_id, occurred_at DESC);
CREATE INDEX idx_txn_ip_time      ON transactions (ip_entity_id, occurred_at DESC);
CREATE INDEX idx_txn_account_time ON transactions (account_entity_id, occurred_at DESC);
CREATE INDEX idx_txn_merchant_time ON transactions (merchant_id, occurred_at DESC);

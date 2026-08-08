-- account_seen_count: the feature that makes "brand new account" expressible.
--
-- THE PROBLEM THIS SOLVES
--
-- account_age_days alone cannot distinguish a first-ever transaction from a
-- returning account seen earlier the same day. Both read 0.
--
-- That is not a bug in the age calculation, it is a consequence of ordering:
-- decision_service resolves and UPSERTS entities before it computes features,
-- so on a first-ever transaction the entity row already exists and its
-- first_seen_at is this transaction's own occurred_at. age = 0. An account
-- that transacted three hours ago also reads age = 0. The signal that
-- separates them is not time, it is the count.
--
-- Measured on IEEE-CIS: 'account_age_days <= 0' alone gave lift 1.08 -- no
-- signal at all. Adding 'and this is not the account's first-ever transaction'
-- gave lift 3.05. Same population, one extra clause.
--
-- Idempotent: ON CONFLICT DO NOTHING, so re-running changes nothing. 007 is
-- already applied and its checksum is recorded, so this is a new file rather
-- than an edit to that one.

INSERT INTO feature_definitions (code, name, description, value_type, source) VALUES
  ('account_seen_count', 'Account transaction count',
   'Lifetime sightings of this account entity, INCLUDING the transaction being '
   'decided right now. A value of 1 therefore means first-ever: entities are '
   'upserted before features are computed, so the current transaction has '
   'already been counted by the time a rule reads this. A returning account is '
   '2 or more. Pair it with account_age_days to separate a genuinely new '
   'account from one that was simply first seen earlier today -- age alone '
   'reads 0 for both.',
   'NUMBER', 'ENTITY')
ON CONFLICT (code) DO NOTHING;

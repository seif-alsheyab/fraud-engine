-- IEEE-CIS fraud dataset: entity types and feature registry.
--
-- This migration registers the vocabulary needed to run the engine against
-- the IEEE-CIS competition data instead of the synthetic generator. Nothing
-- here computes anything; it declares what a rule is ALLOWED to reference.
--
-- HONESTY NOTE, because it changes how these features may be used:
-- the IEEE-CIS data is anonymised. The identifier-shaped columns are not
-- identifiers, and the engineered columns were not engineered by us. Both
-- facts are recorded in the description text below rather than in a README,
-- because the description is what a rule author reads in the UI at 02:00.
--
-- Idempotent: every INSERT is ON CONFLICT DO NOTHING, so re-running this
-- file against a database that already has it changes nothing.

-- Entity types -------------------------------------------------------------
--
-- The existing CARD/EMAIL/DEVICE types mean "a salted hash of a real
-- identifier". These do NOT. They are proxies reconstructed from anonymised
-- columns, they collide, and they must never be presented as the real thing.
INSERT INTO entity_types (code, name, description) VALUES
  ('IEEE_CARD',    'IEEE card proxy',
   'PROXY, NOT AN IDENTIFIER. Derived by combining the anonymised card1-card6 '
   'columns of the IEEE-CIS dataset. The publisher never released what those '
   'columns contain, so this is a stable grouping key that behaves like a card '
   'across rows -- not a card number, and not a hash of one. Distinct real '
   'cards can collide into one proxy.'),
  ('IEEE_ACCOUNT', 'IEEE account proxy',
   'PROXY, NOT AN IDENTIFIER. Derived from the anonymised address columns '
   '(addr1, addr2) together with the card columns. It stands in for "the same '
   'customer again" because the dataset ships no account id. Coarser than a '
   'real account: two people at one billing address look like one account.'),
  ('IEEE_DEVICE',  'IEEE device proxy',
   'PROXY, NOT AN IDENTIFIER. Derived from DeviceType, DeviceInfo and the '
   'id_30-id_33 columns, which describe a device CLASS (OS, browser, screen '
   'size), not a device. Thousands of unrelated phones of the same model share '
   'one proxy, so link counts built on it are an upper bound, not a fact.')
ON CONFLICT (code) DO NOTHING;

-- Features the engine derives from the transaction ---------------------------
INSERT INTO feature_definitions (code, name, description, value_type, source) VALUES
  ('product_code', 'Product code',
   'The ProductCD column: W, C, R, H or S. The publisher never disclosed what '
   'the letters stand for. It is used because it separates fraud rates sharply, '
   'and that is the whole of the justification -- there is no product story to '
   'tell a risk committee beyond the measured rate.',
   'STRING', 'TRANSACTION'),

  ('card_type', 'Card funding type',
   'The card6 column: debit, credit or charge card. One of the few IEEE columns '
   'with a published meaning.',
   'STRING', 'TRANSACTION'),

  ('addr_match', 'Address match result (M4)',
   'The M4 column: M0, M1 or M2, or the literal string "(absent)" when the '
   'column is null. Absence is represented as a value rather than as NULL '
   'because a missing M4 is itself informative and a rule must be able to test '
   'for it -- a NULL would simply make the rule not fire.',
   'STRING', 'TRANSACTION'),

  ('dist_from_billing', 'Distance from billing address',
   'The dist1 column. Units were never published -- not miles, not kilometres, '
   'not confirmed as either. Treat it as an ordered magnitude only: bigger '
   'means further, and no threshold here can be stated in real distance.',
   'NUMBER', 'TRANSACTION'),

  ('has_identity_data', 'Identity record present',
   'True when the transaction has a matching row in the IEEE identity table. '
   'This is a fact about the DATA, not about the shopper: roughly three '
   'quarters of transactions have no identity row at all, and their absence '
   'tracks the collection channel rather than anything the customer did.',
   'BOOLEAN', 'DERIVED'),

  -- Processor-supplied features ---------------------------------------------
  --
  -- Everything below is a column the payment processor (Vesta) shipped with
  -- the dataset ALREADY COMPUTED. This engine does not calculate any of them
  -- and cannot reproduce them. The vesta_ prefix exists so that no one reading
  -- a fired rule mistakes one for a velocity feature this engine derived.
  --
  -- The consequence is operational, not cosmetic: if this ruleset were run on
  -- live traffic, these values would have to arrive from the processor on the
  -- authorisation message. There is no code path in this repository that could
  -- produce them.
  ('vesta_c4', 'Vesta counting feature C4',
   'SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE. One of the IEEE-CIS '
   'C columns, described by the publisher only as a count of addresses or other '
   'entities associated with the card. The exact definition was never released, '
   'so the weight attached to it is justified by measured lift alone and by no '
   'causal account of what it counts.',
   'NUMBER', 'TRANSACTION'),

  ('vesta_c8', 'Vesta counting feature C8',
   'SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE. An IEEE-CIS C '
   'column with an undisclosed definition. Carried because it separates fraud '
   'from non-fraud on held-out data, not because its meaning is understood.',
   'NUMBER', 'TRANSACTION'),

  ('vesta_c10', 'Vesta counting feature C10',
   'SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE. An IEEE-CIS C '
   'column with an undisclosed definition. Correlated with the other C columns, '
   'so its weight should not be read as an independent contribution.',
   'NUMBER', 'TRANSACTION'),

  ('vesta_c12', 'Vesta counting feature C12',
   'SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE. An IEEE-CIS C '
   'column with an undisclosed definition. Correlated with the other C columns, '
   'so its weight should not be read as an independent contribution.',
   'NUMBER', 'TRANSACTION'),

  ('vesta_d3', 'Vesta timedelta feature D3',
   'SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE. One of the IEEE-CIS '
   'D columns, described only as a timedelta -- days between some previous '
   'event and this one. Which event was never published. A SMALL value means '
   'recent, which is why the rules that use it test with lte, not gte.',
   'NUMBER', 'TRANSACTION'),

  ('vesta_d5', 'Vesta timedelta feature D5',
   'SUPPLIED BY THE PROCESSOR, NOT COMPUTED BY THIS ENGINE. An IEEE-CIS D '
   'column measuring days since an undisclosed previous event. As with D3, a '
   'small value means recent.',
   'NUMBER', 'TRANSACTION'),

  -- Account velocity ---------------------------------------------------------
  --
  -- These two are ordinary engine-computed velocity features, not IEEE
  -- columns. They are registered here because the IEEE banded ruleset is the
  -- first thing to reference them and a rule cannot be stored against an
  -- unregistered feature.
  --
  -- NOT YET COMPUTED. feature_service computes card, email, device and IP
  -- velocity; it has no account window. Until that lands, a rule on these
  -- features is inert: evaluate_condition treats a missing feature as
  -- no-match, so the rule scores zero rather than failing loudly.
  ('velocity_account_1h', 'Account uses, 1 hour',
   'Transactions on one account proxy in the past hour. Over IEEE data the '
   'account is the addr1/addr2 proxy, so this counts a billing address rather '
   'than a login.',
   'NUMBER', 'VELOCITY'),

  ('velocity_account_24h', 'Account uses, 24 hours',
   'Transactions on one account proxy in the past day. Same proxy caveat as '
   'the 1-hour window: it is coarser than a real account.',
   'NUMBER', 'VELOCITY')
ON CONFLICT (code) DO NOTHING;

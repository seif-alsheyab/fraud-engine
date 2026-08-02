-- Rules, stored as DATA.
--
-- The alternative is rules written in Python and shipped in a deploy. That
-- fails for three reasons that matter in a real risk team:
--   1. A fraud attack starts at 02:00. Changing a threshold must not require
--      a code review, a build, and a release.
--   2. You cannot answer "which rules were live on 14 March?" if the answer
--      lives in git history rather than in the database.
--   3. You cannot backtest a rule you have not written yet if writing it
--      means writing code.
--
-- So a rule is a row, a ruleset is a versioned collection of rows, and
-- exactly one ruleset per merchant is active at a time.

CREATE TABLE rulesets (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  merchant_id  UUID NOT NULL REFERENCES merchants(id),
  version      INTEGER NOT NULL,
  name         TEXT NOT NULL,
  description  TEXT,
  status       TEXT NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','SHADOW','ACTIVE','RETIRED')),
  -- Score thresholds. Below review_at is APPROVE; at or above decline_at is
  -- DECLINE; between them the case is challenged or queued for a human.
  -- These are per ruleset, not global, because risk appetite is per merchant.
  challenge_at INTEGER NOT NULL DEFAULT 40 CHECK (challenge_at >= 0),
  review_at    INTEGER NOT NULL DEFAULT 60 CHECK (review_at >= 0),
  decline_at   INTEGER NOT NULL DEFAULT 80 CHECK (decline_at >= 0),
  created_by   TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at TIMESTAMPTZ,
  retired_at   TIMESTAMPTZ,
  UNIQUE (merchant_id, version),
  -- The bands must be ordered, or the decision logic is undefined.
  CHECK (challenge_at <= review_at AND review_at <= decline_at)
);

-- Only ONE active ruleset per merchant, enforced by the database.
-- A partial unique index is the right tool: it constrains only the rows
-- where status = 'ACTIVE' and ignores every draft and retired version.
-- Enforcing this in application code alone means a race between two admins
-- activating different versions leaves both live, and decisions become
-- non-deterministic.
CREATE UNIQUE INDEX idx_one_active_ruleset_per_merchant
  ON rulesets (merchant_id) WHERE status = 'ACTIVE';

-- SHADOW mode: a candidate ruleset that is evaluated on live traffic but
-- whose decision is recorded rather than applied. Also limited to one.
CREATE UNIQUE INDEX idx_one_shadow_ruleset_per_merchant
  ON rulesets (merchant_id) WHERE status = 'SHADOW';

-- The features a rule is allowed to test. A registry, not free text, so a
-- typo in a rule condition fails at write time instead of silently never
-- matching in production.
CREATE TABLE feature_definitions (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,
  value_type  TEXT NOT NULL CHECK (value_type IN ('NUMBER','BOOLEAN','STRING')),
  -- Where the value comes from, useful for documentation and for knowing
  -- which features are cheap and which cost a query.
  source      TEXT NOT NULL CHECK (source IN ('TRANSACTION','VELOCITY','ENTITY','LIST','LINK','DERIVED'))
);

CREATE TABLE rules (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ruleset_id    UUID NOT NULL REFERENCES rulesets(id) ON DELETE CASCADE,
  code          TEXT NOT NULL,
  name          TEXT NOT NULL,
  description   TEXT,
  -- The condition, as structured JSON rather than a string to be eval'd.
  -- Shape: {"all":[{"feature":"velocity_card_1h","op":"gte","value":4}]}
  -- Structured JSON can be validated, inspected, indexed and rendered in a
  -- UI. A string that gets eval'd is arbitrary code execution wearing a
  -- rule's clothing.
  condition     JSONB NOT NULL,
  -- Points added to the risk score when the condition matches.
  -- Negative weights are allowed and are important: strong positive signals
  -- (3DS authenticated, long-standing customer) should pull the score DOWN,
  -- otherwise the engine can only ever become more suspicious.
  weight        INTEGER NOT NULL,
  -- A hard action bypasses scoring entirely. Used for deny-list hits, where
  -- accumulating points would be absurd.
  hard_action   TEXT CHECK (hard_action IN ('DECLINE','APPROVE','REVIEW')),
  is_enabled    BOOLEAN NOT NULL DEFAULT true,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (ruleset_id, code)
);

CREATE INDEX idx_rules_ruleset ON rules (ruleset_id) WHERE is_enabled;

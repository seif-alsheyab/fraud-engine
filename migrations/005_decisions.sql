-- Decisions, and everything needed to reproduce one.

CREATE TABLE decisions (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  transaction_id    UUID NOT NULL REFERENCES transactions(id),
  ruleset_id        UUID NOT NULL REFERENCES rulesets(id),
  -- Was this the live decision, or a shadow ruleset scored in parallel?
  -- Shadow decisions are recorded and never applied.
  mode              TEXT NOT NULL DEFAULT 'LIVE' CHECK (mode IN ('LIVE','SHADOW','BACKTEST')),

  decision          TEXT NOT NULL REFERENCES decision_types(code),
  score             INTEGER NOT NULL,

  -- THE frozen snapshot: every feature value the engine saw, at the instant
  -- it decided. This is what makes a decision defensible six weeks later.
  --
  -- Re-running the rules today would use today's rules and today's velocity
  -- counters -- a different answer to a different question. Only a stored
  -- snapshot answers "why did we approve THAT, THEN?"
  features          JSONB NOT NULL,
  -- Which rules fired, with their weights, in evaluation order.
  triggered_rules   JSONB NOT NULL DEFAULT '[]'::jsonb,

  latency_ms        INTEGER NOT NULL CHECK (latency_ms >= 0),
  -- True when the decision exceeded the configured budget. Stored rather
  -- than computed on read, because the budget may change and a breach is a
  -- fact about the past.
  exceeded_budget   BOOLEAN NOT NULL DEFAULT false,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_decisions_txn      ON decisions (transaction_id);
CREATE INDEX idx_decisions_ruleset  ON decisions (ruleset_id, created_at DESC);
CREATE INDEX idx_decisions_outcome  ON decisions (decision, created_at DESC) WHERE mode = 'LIVE';

-- Decisions are immutable. A decision that can be edited after the fact is
-- worthless as evidence -- the same reasoning as the chargeback audit log.
-- The escape hatch exists for retention purges and test fixtures, and must
-- be requested explicitly in the same transaction.
CREATE FUNCTION reject_decision_mutation() RETURNS trigger AS $fn$
BEGIN
  IF current_setting('fraud.allow_decision_purge', true) = 'on' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  RAISE EXCEPTION 'decisions is append-only; % is not permitted', TG_OP;
END;
$fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_decisions_immutable
  BEFORE UPDATE OR DELETE ON decisions
  FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation();

-- Lists: allow, deny, watch.
--
-- A list entry beats scoring. If a card is on the deny list, no combination
-- of good signals should approve it -- that is what hard_action is for.
CREATE TABLE list_entries (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  list_type     TEXT NOT NULL CHECK (list_type IN ('ALLOW','DENY','WATCH')),
  entity_id     UUID NOT NULL REFERENCES entities(id),
  -- NULL scope means the entry applies to every merchant. A confirmed
  -- fraudulent card should not have to be blocked merchant by merchant.
  merchant_id   UUID REFERENCES merchants(id),
  reason        TEXT NOT NULL,
  added_by      TEXT NOT NULL,
  -- Entries expire. A permanent block on an IP address is a mistake: IPs
  -- are reassigned, and a customer inherits a stranger's punishment.
  expires_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_list_lookup ON list_entries (entity_id, list_type);

-- Labels: the truth, arriving weeks later.
--
-- Separate from decisions because the timing is completely different. A
-- decision is made in 200ms; the label arrives in 6 weeks, from a
-- chargeback file or a manual fraud report. Joining them is what makes
-- measurement possible at all -- without labels you can report how many
-- transactions you declined, but never whether you were right.
CREATE TABLE labels (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  transaction_id  UUID NOT NULL REFERENCES transactions(id),
  label           TEXT NOT NULL CHECK (label IN ('FRAUD','LEGITIMATE','DISPUTED_NON_FRAUD')),
  source          TEXT NOT NULL CHECK (source IN ('CHARGEBACK','MANUAL_REVIEW','ISSUER_REPORT','REFUND_REQUEST','ASSUMED_GOOD')),
  -- Chargeback reason code where the label came from a dispute, so fraud
  -- disputes can be told apart from service disputes.
  reason_code     TEXT,
  amount_minor    BIGINT,
  labelled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- How long after the transaction the truth arrived. Drives how long you
  -- must wait before performance figures for a period are trustworthy.
  days_to_label   INTEGER,
  UNIQUE (transaction_id, source)
);

CREATE INDEX idx_labels_txn   ON labels (transaction_id);
CREATE INDEX idx_labels_label ON labels (label, labelled_at DESC);

-- Manual review queue for decisions that were neither approved nor declined.
CREATE TABLE review_cases (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  decision_id   UUID NOT NULL REFERENCES decisions(id) UNIQUE,
  status        TEXT NOT NULL DEFAULT 'OPEN'
    CHECK (status IN ('OPEN','IN_PROGRESS','RESOLVED','EXPIRED')),
  assigned_to   TEXT,
  disposition   TEXT CHECK (disposition IN ('APPROVE','DECLINE')),
  analyst_note  TEXT,
  -- Review has a deadline too: an unreviewed order is an unshipped order,
  -- and a customer who waits three days has already bought elsewhere.
  sla_due_at    TIMESTAMPTZ NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at   TIMESTAMPTZ,
  CHECK (status <> 'RESOLVED' OR disposition IS NOT NULL)
);

CREATE INDEX idx_review_open ON review_cases (sla_due_at) WHERE status IN ('OPEN','IN_PROGRESS');

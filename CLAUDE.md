# CLAUDE.md — fraud-engine

Context for Claude Code. Read this before writing anything.

---

## 1. What this is

A **real-time payment fraud decision engine**. A transaction arrives over HTTP;
within a latency budget the system returns `APPROVE` / `CHALLENGE` / `REVIEW` /
`DECLINE`, with the reasons, and stores a permanently reproducible record of
every input value that produced that answer.

It is a **portfolio project**, published at
`github.com/seif-alsheyab/fraud-engine` under All Rights Reserved. Its purpose
is to demonstrate the author's engineering and payments-domain judgement. It is
not deployed and must never be presented as production-ready.

**Companion repo:** `github.com/seif-alsheyab/chargeback-iq` — the other half
of the same loop. fraud-engine decides at checkout; chargeback-iq handles the
dispute that arrives six weeks later. Keep them conceptually consistent.

### The core idea, in one paragraph

You have ~200ms to guess whether the person holding a card owns it. You can
never know. **Both errors cost money**: approve a fraudster and you lose the
goods, the money and a chargeback fee; decline a real customer and you lose the
sale and often the customer. A system that blocks everything has zero fraud and
zero revenue. The job is not "stop fraud" — it is finding the cheapest mixture
of both mistakes. Every design decision below follows from that.

---

## 2. Current state

| | |
|---|---|
| Commits | 14 on `main`, all attributed to `alsheyab.seif@gmail.com` |
| Tests | **144 passing**, ~1.5s |
| Lint | ruff clean across `src/ tests/ scripts/` |
| Migrations | 6 applied |
| Tables | 14 |
| Features registered | 27 |
| CI | green on Python 3.12 and 3.13 |
| Demo data | 1,378 synthetic transactions, seed 42 |

### Measured results (seed 42, 2.47% fraud)

```
TP=19   FP=0   FN=15   TN=1344
precision 1.000 · recall 0.559 · FPR 0.000 · approval 0.986
latency avg 2.5ms · p95 3ms · max 7ms · 0 breaches of the 250ms budget
```

**Recall is 56% and stays reported, not tuned away.** The misses are mostly
bust-out, which no current rule catches. Do not "fix" this by adding a rule
that special-cases the generator's output — that would be fitting to the test
data. If recall improves it must be because a genuinely general rule was added.

Two numbers that are misleading and are flagged as such in the README — keep
the caveats if you touch them:

- **Every detection rule shows lift 40.5.** Arithmetic, not insight: all scored
  precision 1.0, and lift = precision ÷ base rate. Lift only discriminates when
  precision varies.
- **Precision 1.000 with zero false positives is unrealistic.** Synthetic data
  contains no ambiguous transactions.

---

## 3. Environment

```
Machine        Ubuntu 24.04, 8 cores, 35GB RAM
Python         3.12.3   (system; 3.10 headers only — prebuilt wheels required)
Package mgr    uv 0.11.32   (NOT pip: PEP 668 blocks system installs)
Postgres       16 in Docker, container `fraud-engine-db`, host port 5434
API port       4020
Docker context MUST be `default`, not `desktop-linux`
```

**Ports 4010/5433 belong to chargeback-iq. 3000 and 8080 are taken. Do not use
them.**

### Pinned versions

```
fastapi 0.141.1 · starlette 1.3.1 · pydantic 2.13.4 · pydantic-settings 2.14.2
psycopg 3.3.4 (+psycopg-binary, +psycopg-pool 3.3.1) · uvicorn 0.52.1
pytest 9.1.1 · pytest-asyncio 1.4.0 · httpx 0.28.1 · ruff 0.16.1
```

### Commands

```bash
uv sync --extra dev                          # install
uv run python -m fraud_engine.db.migrate     # migrate (idempotent)
uv run python scripts/generate.py            # synthetic data
uv run python -m fraud_engine.api.server     # API on :4020
uv run pytest -q                             # 144 tests
uv run ruff check src/ tests/ scripts/       # lint
docker compose up -d / down                  # database
```

---

## 4. Layout

```
src/fraud_engine/
  config.py                  Pydantic Settings, read once, validated once
  db/
    pool.py                  async psycopg pool; open() is explicit, not on import
    migrate.py               checksummed, transactional, idempotent
  domain/                    PURE — no I/O, no database, no network
    operators.py             the six rule operators, and only six
    conditions.py            validate + evaluate JSON conditions
    scoring.py               Rule, Thresholds, decide()
    entities.py              normalise + salted hash + display hints
    metrics.py               confusion matrix, per-rule stats, backtest compare
  repositories/              SQL ONLY — no business rules
    reference_repository.py  merchants, rulesets, rules, features, BINs
    entity_repository.py     entity upsert, list entries
    velocity_repository.py   velocity + shared-attribute queries
    decision_repository.py   transactions, decisions, labels, review cases
    analytics_repository.py  decisions joined to labels
  services/                  orchestration — what must be atomic
    feature_service.py       raw payment -> feature dict
    decision_service.py      the pipeline
    analytics_service.py     performance report, backtest
  api/
    app.py  server.py  schemas.py  errors.py
    routes/decisions.py labels.py analytics.py reference.py
  lib/
    errors.py                typed errors carrying HTTP status + code
    logging.py               JSON lines with a secret denylist

migrations/   001_extensions 002_reference 003_rules
              004_transactions 005_decisions 006_seed_reference
scripts/generate.py
tests/unit/  tests/integration/  tests/helpers/
```

---

## 5. Invariants — never violate these

These are load-bearing. Breaking one silently produces a system that looks fine
and is wrong.

### 5.1 Raw card numbers are never stored, logged, or returned

Only `hash_value(entity_type, value, salt)` — salted SHA-256 — plus the BIN and
last four digits. The engine does not need to *know* a card, only to recognise
the *same* card.

Salting is not optional: card numbers have low entropy (16 digits, known BIN,
checksum last digit), so an unsalted hash of every possible card is
precomputable.

`ENTITY_HASH_SALT` never enters a tracked file. CI fails if it does.

### 5.2 Identifiers are normalised BEFORE hashing

`Sief@Gmail.com`, `s.ief@gmail.com` and `sief+shop@gmail.com` are one mailbox.
Hash them raw and you create three unrelated entities — and every velocity
counter silently reads zero for a returning customer. Gmail-family dots and all
plus-tags are stripped; dots are preserved elsewhere.

### 5.3 Rules are data, never code, and are interpreted, never `eval`'d

A condition is JSON walked by an interpreter that knows six operators:
`eq ne gte lte in not_in`. It cannot call a function, import a module, or read
a file. **Never add an operator that takes a callable, a regex from user input,
or a format string.**

An unknown operator is rejected when the rule is *saved*, not when traffic hits
it — a rule that silently never matches looks like a quiet rule and nobody
investigates.

### 5.4 Domain functions receive their data, never fetch it

`decide(rules, features, thresholds)` is handed everything. It does not query.
This is why 60+ unit tests run in milliseconds with no database, and why the
same function is exercised against fake data in unit tests and real rows in
integration tests. **Do not add a database call to anything in `domain/`.**

### 5.5 `decisions` is append-only

Enforced by a database trigger, not by convention. The one deliberate door is
`set_config('fraud.allow_decision_purge','on',true)` in the same transaction —
used only by test cleanup and retention purges. That is the difference between
*impossible* and *only possible on purpose*.

### 5.6 The feature snapshot is frozen at decision time

`decisions.features` stores every value the engine saw. Re-running rules later
uses *today's* rules and *today's* velocity — a different answer to a different
question. **Backtests must replay stored snapshots, never recompute features.**

### 5.7 Missing configuration is an error, never a default-approve

No `ACTIVE` ruleset raises `NoActiveRulesetError`. Silently approving because
nobody configured a merchant is the single most expensive failure mode a fraud
engine has.

### 5.8 `None` and `0` are different

`_rate()` returns `None` when the denominator is zero. "No fraud occurred" and
"we caught 0% of fraud" look identical as `0.0` and mean opposite things. A
compliance surface that says "you're fine" when it has no idea is worse than
one that says nothing.

### 5.9 Money is `BIGINT` in minor units

Never float. A currency column typed `FLOAT` eventually produces a payment of
12499.999999 fils.

### 5.10 Negative-weight rules are PROTECTIVE, not failing detectors

`THREE_DS_OK` (weight −35) fires on legitimate traffic by design. Judge
detection rules on precision and protective rules on `protection_error_rate`
(how often they lowered the score on something that turned out to be fraud).
Reporting both in one "precision" column would show `THREE_DS_OK` at 0% and
invite someone to delete the rule keeping good customers approved.

---

## 6. Conventions

**SQL** — raw, via psycopg 3, no ORM. Parameters always `%s`, never string
concatenation. Column *names* cannot be bind parameters, so where one must be
interpolated (`velocity_repository.entity_velocity`) it is validated against a
fixed allow-list first.

**Velocity queries** — filter on the entity column FIRST, then time. Indexes
are `(entity_id, occurred_at DESC)`. Leading with time forces a scan of every
entity's rows. Never wrap a timestamp column in a function: `occurred_at >= %s`
uses the index, `date_trunc('month', occurred_at) = %s` cannot.

**Repositories** take `conn` as the first argument, always. That may be the
pool or a single connection inside a rolled-back transaction; the function
neither knows nor cares.

**Errors** are typed subclasses of `AppError` carrying `status` and `code`. The
API layer maps them without inspecting message strings. Unexpected exceptions
log full detail internally and return a generic message externally — internal
messages leak table names and query fragments.

**Logging** is JSON lines. `lib/logging.py` scrubs `card_number`, `email`,
`ip_address`, `device_fingerprint`, `account_id` and the salt from every line.
Log lines outlive databases.

**Pydantic** request models use `extra="forbid"`. A typo like `card_numbr` must
return 422, not silently decide the payment with no card. **Optional and
nullable are different** and the schema draws the line deliberately: `currency`
may be omitted *or* null; `channel` may be omitted but not null.

**Comments explain WHY, not what.** Every non-obvious decision in this codebase
carries its reasoning inline. Match that. A comment restating the code is
noise; a comment explaining why the obvious approach was rejected is the point.

---

## 7. Testing

```
tests/unit/          no database at all — pure functions
tests/integration/   inside a transaction that is ALWAYS rolled back
tests/integration/test_api.py   commits, cleans up by prefix scope
```

**HTTP tests cannot share the test's transaction** — the app borrows its own
pool connection and would never see uncommitted rows. They commit and clean up
afterwards, scoped by a prefix unique to the file.

**Each HTTP suite owns its own scope prefix.** Pytest runs files in parallel; a
shared prefix means one suite's cleanup deletes another's fixtures mid-run,
producing "row vanished" failures and foreign-key violations that look like
several unrelated bugs.

**Never assert on global state.** `assert len(queue) == 1` only passes on an
empty database. Scope every assertion to the rows the test itself created.

**CI runs the suite three times**: empty database, again to prove cleanup is
repeatable, then against a populated one. A suite that only passes on a virgin
database is not a suite you can trust.

**Test the contract, not your assumptions.** `test_the_exact_dict_pydantic_produces_is_accepted`
builds its payload from `DecisionRequest.model_dump()` rather than a
hand-written dict, so it cannot drift from what the API actually receives.

---

## 8. Traps — every one of these cost real time here

| Trap | What happens | Rule |
|---|---|---|
| `dict.get(k, default)` on a Pydantic dump | Pydantic emits every field, so an omitted optional is a **present key set to None** and the default never applies. Sent `None` into a NOT NULL column; 130 tests passed, first real request 500'd. | Use `payload.get(k) or default` |
| `pytest.fixture(loop_scope=...)` | `TypeError` at collection. `loop_scope` exists only on `pytest_asyncio.fixture`. | Verify a library API by introspection before writing against it |
| Async fixture with `scope="module"` and no `loop_scope="module"` | `ScopeMismatch` errors every test in the file, with a message naming an internal fixture that explains nothing. | Fixture scope and loop scope must match |
| `date_trunc(col AT TIME ZONE 'UTC') = $ts` | Yields `timestamp`; driver sends `timestamptz`. Silently returns **zero rows** on a non-UTC server. | Half-open range: `>= start AND < next` |
| `now()` in Postgres | Returns *transaction* start time. Events written in one transaction share an identical timestamp; ordering by it falls through to a random UUID. | Order by a `BIGSERIAL` sequence |
| Docker context drifting to `desktop-linux` | `docker ps` shows nothing while containers are plainly running. | `docker context ls` is the FIRST check when Docker behaves impossibly |
| `grep -q "api_client()"` | Matches inside `shared_api_client()`. A check that appears to verify something while verifying something else is worse than no check. | Anchor greps |
| Guessing an expected value in an assertion | Asserted `9990.0`; correct was `9990.01`. | Compute it, or express the computation in the assertion |
| `set -euo pipefail` around `psql` | A deliberately-failing check aborts the whole script and the remaining checks never run. | Omit `set -e` in verification scripts |

**The meta-lesson:** three of the four real bugs in this project were found by
tests, and the fourth by a live smoke test that 130 passing tests missed. When
several tests fail at once in different files, suspect one shared resource
before suspecting several unrelated defects.

---

## 9. Domain reference

**Decisions.** `APPROVE` and `CHALLENGE` let money through (challenge adds
step-up auth and shifts liability to the issuer). `REVIEW` and `DECLINE` block
— a held order is not a completed sale, so `REVIEW` counts as blocking in the
confusion matrix.

**Feature sources.** `TRANSACTION` (free, no query) · `DERIVED` (free) ·
`VELOCITY` (query) · `ENTITY` (query) · `LINK` (query) · `LIST` (query). The
feature service computes only what the active ruleset references.

**Fraud patterns the generator injects.**

| Pattern | Signature | Caught? |
|---|---|---|
| Card testing | one card, 5–12 charges in minutes, CVV failing, escalating amounts | yes |
| Account takeover | established account, sudden new device + country + card | partly |
| Fraud ring | many accounts and emails, one shared device or card | yes |
| Bust-out | quiet account, long good history, then spending far above it | **no** |

Bust-out is deliberately uncaught. A demo where the engine catches everything
is a demo that is lying.

**Metrics.** Precision = of what you blocked, how much was fraud. Recall = of
all fraud, how much you caught. They pull against each other. FPR = of all good
customers, how many you blocked — the most commercially expensive number and
the one most often left off dashboards. Label coverage matters because
chargebacks take 30–90 days, so a recent period always looks fraud-free.

---

## 10. Git and publishing

**`main` requires cryptographically signed commits.** SSH signing is already
configured locally (`commit.gpgsign=true`, `gpg.format=ssh`,
`gpg.ssh.allowedSignersFile=~/.ssh/allowed_signers`). Commits made in this repo
sign automatically. **Do not disable it** — an unsigned push to `main` is
rejected by GitHub.

**A pre-commit hook** (`core.hooksPath=.githooks`) blocks: the wrong author
email, `.env` and key files, credential-shaped strings, database dumps, and
files over 10MB. It deliberately does **not** block `*.sql` — migrations are
schema and must be committed.

**Repository is locked down**: issues, wiki, projects and discussions disabled;
external PRs auto-closed and locked; Actions token read-only; fork PRs never
run CI; force-push and deletion blocked.

**Commit messages** follow Conventional Commits (`feat(scope):`, `fix(scope):`,
`docs:`, `chore:`) with a body explaining *why* when the change is not obvious.
Look at the existing 14 for the register.

---

## 11. Roadmap

Ordered by value. Do not skip ahead — each depends on the one before.

### Tier 1 — makes the repo materially stronger

**1.1 API authentication.** Currently the single largest gap, and named first
in the README's "Not included". API keys hashed at rest, per-merchant, with a
`merchant_api_keys` table and a FastAPI dependency. Must not break the existing
144 tests: add a test-mode bypass or seed a key in the fixtures.

**1.2 Rule admin endpoints.** `POST /v1/rulesets`, `POST /v1/rules`,
`POST /v1/rulesets/{id}/activate`. Rules are already data; there is no way to
edit them without SQL. Activation must validate every condition against the
feature registry **before** the ruleset can go `ACTIVE`, and must be atomic
(the partial unique index enforces one active ruleset — handle the conflict
rather than letting it 500).

**1.3 A velocity feature that catches bust-out.** Something like
`amount_vs_account_p95_30d` — this transaction's amount relative to the
account's own history. This is the honest way to raise recall above 56%. Add
the feature to the registry, compute it in `feature_service`, write a rule that
uses it, and **re-measure**. Report the new precision/recall honestly, including
if false positives appear.

**1.4 Scheduled SLA sweep.** Review cases have `sla_due_at` and nothing expires
them. A small async task, plus an endpoint to trigger it, plus a test that a
breached case is marked and not silently forgotten.

### Tier 2 — depth

**2.1 PSP webhook ingestion.** chargeback-iq already has a signed,
replay-protected, idempotent webhook receiver — mirror that design here for
inbound label ingestion (a chargeback file arriving 40 days later). HMAC over
`timestamp.rawBody`, timestamp tolerance, unique `(processor, event_id)`.

**2.2 Reason-code taxonomy for labels.** Currently `reason_code` is free text.
chargeback-iq has a full Visa/Mastercard mapping — reuse the concept so a fraud
dispute can be told apart from a service dispute. A `DISPUTED_NON_FRAUD` label
should not count as fraud in the confusion matrix.

**2.3 Multi-tenancy isolation tests.** Merchant A must never see merchant B's
decisions, entities or lists. Currently trusted, not proven.

### Tier 3 — only with real data

**3.1 Backtest against real labelled transactions.** Everything above is
measured on synthetic data with exact ground truth. The single most valuable
next step is not code: it is obtaining anonymised historical transactions with
chargeback labels and running the existing backtest endpoint against them.
Whatever precision comes back — realistically 0.3–0.6 — is the first honest
number this project will have produced.

**3.2 A model, and only then.** A model trained on synthetic data proves
nothing about fraud. If one is ever added it belongs alongside the rules as a
score contributor, never replacing them, and its output must remain explainable
enough to defend to a risk committee.

### Do not build

- Redis or a feature store. Velocity is measured at p95 = 3ms. Caching would be
  optimisation without evidence. Add it when a measurement demands it.
- A frontend. The API and its OpenAPI docs are the deliverable.
- Anything that improves the demo numbers by special-casing the generator.

---

## 12. Working agreement

- **Verify before asserting.** If a library API is uncertain, introspect it
  first. Three separate failures here came from writing against a remembered
  API instead of the installed one.
- **Read the file before patching it.** Anchor-based edits must assert the
  anchor exists exactly once, and abort rather than half-apply.
- **Every change ends green**: `uv run ruff check src/ tests/ scripts/` and
  `uv run pytest -q`.
- **A new behaviour needs a test that would fail without it.** A test that
  passes either way documents nothing.
- **When a measured number changes, report it** — including if it got worse.
  The README's credibility rests on 56% recall being stated rather than hidden,
  and that is worth more than a better-looking figure.
- **If asked to add something in the "Do not build" list**, say so and explain
  why before complying.

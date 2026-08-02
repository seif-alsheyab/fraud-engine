# fraud-engine

**Real-time payment fraud decision engine.** A transaction arrives, and within a
latency budget the system returns `APPROVE` / `CHALLENGE` / `REVIEW` / `DECLINE`
with the exact reasons — plus a permanent, reproducible record of every input
value that produced that answer.

[![CI](https://github.com/seif-alsheyab/fraud-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/seif-alsheyab/fraud-engine/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![PostgreSQL 16](https://img.shields.io/badge/postgresql-16-336791.svg)](https://www.postgresql.org/)
[![License: All Rights Reserved](https://img.shields.io/badge/license-all%20rights%20reserved-red.svg)](LICENSE)

```
  payment  ──▶  features  ──▶  rules  ──▶  score  ──▶  decision
                    │             │           │            │
                    └─────────────┴───────────┴────────────┘
                              frozen snapshot
                         (reproducible six weeks later)
```

> **Viewing only.** This repository is published so the work can be read. It
> does not accept contributions: pull requests are closed automatically and
> `main` accepts only signed commits from the author. See `LICENSE`.

---

## Contents

| Section | |
|---|---|
| [The problem this solves](#the-problem-this-solves) | Why both kinds of mistake cost money |
| [Architecture](#architecture) | Three decisions that shape everything |
| [Card numbers are never stored](#card-numbers-are-never-stored) | Salted hashing and normalisation |
| [What it detects](#what-it-detects) | Velocity, shared attributes, entity age, lists |
| [Every decision is reproducible](#every-decision-is-reproducible) | Append-only decisions, honest backtesting |
| [Running it](#running-it) | Clone to first decision in six commands |
| [About the data](#about-the-data) | Synthetic, and why — plus measured results |
| [API](#api) | Endpoint reference |
| [Not included](#not-included) | What was deliberately left out |

---

## The problem this solves

A payment arrives. You have roughly 200 milliseconds to answer one question:
*is the person holding this card the person who owns it?* You can never know
for certain. You are always betting.

**Both ways of being wrong cost money, and they cost it differently.**

| Mistake | What it costs |
|---|---|
| Approve a fraudster | The goods, the money, **and** a chargeback fee |
| Decline a real customer | The sale — and often the customer, permanently |

A system that blocks everything has zero fraud and zero revenue. A system that
approves everything bleeds. Measured on 1,000 transactions at a realistic 0.5%
fraud rate:

| Strategy | Recall | Precision | Approval rate | Fraud losses |
|---|---:|---:|---:|---:|
| Block everything | 1.000 | 0.005 | 0.000 | 0 bps |
| Approve everything | 0.000 | n/a | 1.000 | 197 bps |
| **A real engine** | **0.800** | **0.167** | **0.976** | **39 bps** |

> The job is not "stop fraud". It is finding the cheapest mixture of both
> mistakes — and *whether 20 blocked customers is worth 1 caught fraud* is a
> business decision the numbers inform but never answer.

---

## Architecture

```
HTTP  ───────────────────────────────────────────────────────────
      api/            FastAPI · Pydantic validation · redacted logs
                        │
      services/       orchestration — what must happen atomically
                        │
      domain/         pure functions — rules, scoring, metrics, hashing
                        │
      repositories/   SQL only, no business rules
                        │
      PostgreSQL      constraints · triggers · rules stored as data
──────────────────────────────────────────────────────────────────
```

### 1. Rules are data, not code

A rule is a row. A ruleset is a versioned collection. Exactly one ruleset per
merchant is `ACTIVE`, enforced by a partial unique index — not by application
logic that two admins can race.

A fraud attack starts at 02:00. Changing a threshold must not require a code
review, a build and a release. Storing rules as data also means you can answer
*"which rules were live on 14 March?"* and backtest a rule you have not written
yet.

### 2. Rules are interpreted, never `eval`'d

A condition is JSON, walked by code that knows six operators. It cannot call a
function, import a module, or read a file. A hostile operator is rejected when
the rule is **saved**, not when traffic hits it:

```jsonc
{ "feature": "amount_minor", "op": "__import__('os').system", "value": "rm -rf /" }
```
```
→ rejected at save time: unknown operator '__import__('os').system'
```

Storing conditions as strings and calling `eval` turns *"let the risk team edit
rules"* into *"let the risk team run arbitrary code on the payment server"*.

### 3. Domain functions receive their data, never fetch it

`decide()` is handed the rules and the features; it does not query for them.
That is why the unit tests run in milliseconds with no database at all, and why
the same function is exercised against fake data in unit tests and real rows in
integration tests.

---

## Card numbers are never stored

The engine does not need to **know** a card number. It only needs to recognise
that this is the **same** card as before — and a salted hash does that exactly
as well.

```
input       4111111111111111
stored      288ba8933c3aa75c55fb5431b4e7c4066f9172aa2ff82935f6e882aff2c57467
displayed   1111
```

**Salting is not optional.** Card numbers have low entropy — 16 digits, of
which the BIN is known and the last is a checksum — so an unsalted hash of
every possible card is precomputable. With a secret salt it is not.

**Identifiers are normalised before hashing**, or the scheme fails silently:

```
Sief@Gmail.com  ─┐
s.ief@gmail.com  ├──▶  sief@gmail.com  ──▶  one entity
sief+shop@…     ─┘
```

Hash them raw and you get three unrelated entities — and every velocity counter
reads zero for a returning customer.

**Logs are separately guarded.** A denylist scrubs `card_number`, `email`,
`ip_address`, `device_fingerprint` and the salt itself from every log line, and
CI fails the build if a PAN appears in the server log. Log lines outlive
databases: they are shipped to third parties, sit in files for years, and are
read by people with no database access.

---

## What it detects

**Velocity** — A normal person buys once, maybe twice. A stolen card gets tested
with small charges then drained fast. No individual payment looks suspicious;
the *pattern* does.

**Shared attributes across accounts** — Ten accounts, ten names, ten emails, all
sharing one device fingerprint. Each order is unremarkable alone. One human is
operating all ten. This is a graph problem hiding inside a payments problem, and
it is invisible unless you deliberately look **across** rows.

**Entity age and history** — A card first seen four seconds ago is a different
proposition from one first seen two years ago.

**Lists** — Allow / deny / watch, with TTL. A permanent block on an IP address
is a mistake: addresses are reassigned and a stranger inherits the punishment.
A deny entry beats any number of good signals, including 3-D Secure.

---

## Every decision is reproducible

Six weeks after you approved something, a chargeback arrives and someone asks
why. If you stored only `"APPROVED"`, you cannot answer. If you re-run the rules
today you get *today's* rules and *today's* velocity counters — a different
answer to a different question.

So each decision stores the exact feature snapshot and the exact ruleset
version, permanently. `decisions` is append-only, enforced by a database
trigger:

```sql
UPDATE decisions SET decision = 'APPROVE';
```
```
ERROR:  decisions is append-only; UPDATE is not permitted
```

There is one deliberate door — an explicit
`set_config('fraud.allow_decision_purge','on',true)` in the same transaction —
for retention purges and test fixtures. That is the difference between
*impossible* and *only possible on purpose*.

The same frozen snapshots make backtesting honest: a candidate ruleset is
replayed against the questions the engine **actually faced**, not today's
versions of them.

---

## Running it

Requires **Python ≥ 3.12**, [uv](https://docs.astral.sh/uv/), and **Docker**.

```bash
cp .env.example .env
uv sync --extra dev

docker compose up -d                       # Postgres 16 on localhost:5434
uv run python -m fraud_engine.db.migrate
uv run python scripts/generate.py          # ~1,400 synthetic transactions
uv run python -m fraud_engine.api.server   # API on localhost:4020
```

Then:

```bash
curl -s 'localhost:4020/v1/performance?merchant_code=DEMO-SHOP&days=120' | jq
curl -s 'localhost:4020/v1/review-queue' | jq

curl -s -X POST localhost:4020/v1/decide \
  -H 'content-type: application/json' \
  -d '{"merchant_code":"DEMO-SHOP","external_id":"test-1","amount_minor":25000}'
```

Interactive API docs at **`localhost:4020/docs`**.

### Tests

```bash
uv run pytest -q     # 144 tests
```

| Layer | Isolation strategy |
|---|---|
| Unit | No database at all — pure functions receive their data |
| Integration | Runs inside a transaction that is **always** rolled back |
| HTTP | Commits (a request uses its own pool connection), cleans up by prefix scope |

CI runs the suite **three times**: on an empty database, again to prove cleanup
is repeatable, then a third time against a database populated with ~1,400 demo
transactions.

> A suite that only passes on a virgin database is not a suite you can trust.

---

## About the data

**It is synthetic, and that is a deliberate choice.** Public fraud datasets are
PCA-anonymised — features are unnamed components `V1..V28`, so a *rule* written
against them is meaningless to a human and cannot be explained to a risk
committee. They suit a model demo, not a rule engine.

Generating the data means the ground truth is **exact**. Four fraud patterns are
injected, each labelled at creation:

| Pattern | Signature |
|---|---|
| **Card testing** | One card, 5–12 charges in minutes, CVV failing, amounts escalating |
| **Account takeover** | Established account, sudden new device + country + card |
| **Fraud ring** | Many accounts and emails, one shared device or card |
| **Bust-out** | Quiet account, long good history, then spending far above it |

Fraud is labelled `CHARGEBACK` with a realistic 30–75 day delay. Legitimate
transactions are labelled `ASSUMED_GOOD` — honest naming, because *"no
chargeback arrived"* is an assumption, not proof.

### Measured results

Seed 42 · 1,378 transactions · 2.47% fraud

```
TP=19   FP=0   FN=15   TN=1344

precision  1.000     recall  0.559     FPR  0.000     approval  0.986
latency    avg 2.5ms · p95 3ms · max 7ms · 0 breaches of the 250ms budget
```

**Recall is 56%, and that is reported rather than tuned away.** The engine
misses 15 frauds worth roughly $17,000 — mostly bust-out, which no current rule
catches because none compares an amount to the account's **own** history.

> A demo where the engine catches everything is a demo that is lying.

Two caveats on the numbers, stated because they would otherwise mislead:

- **Every detection rule shows lift 40.5.** That is arithmetic, not insight: all
  of them scored precision 1.0, and lift = precision ÷ base rate. Lift only
  discriminates when precision varies, which it does on real traffic and does
  not on clean synthetic data.
- **Precision 1.000 with zero false positives is unrealistically good.** Real
  traffic contains ambiguous transactions that synthetic generation does not
  reproduce. Treat these figures as evidence the pipeline works end to end, not
  as a claim about real-world performance.

### One finding worth reading

Rules are split into **detection** (positive weight, judged on precision) and
**protective** (negative weight, judged on how often they shielded fraud):

```
DETECTION rules            fired   on fraud   precision
─────────────────────────────────────────────────────────
GEO_MISMATCH                  30         30       1.000
LINK_CARD_ACCOUNTS            13         13       1.000
VEL_CARD_1H                   11         11       1.000

PROTECTIVE rules           fired   on fraud   error rate
─────────────────────────────────────────────────────────
THREE_DS_OK                  407          0       0.000
TRUSTED_CARD                   6          4       0.667
```

`THREE_DS_OK` fires 407 times and never on fraud — 3-D Secure authentication
works. `TRUSTED_CARD` fired on 4 frauds out of 6: **a rule that rewards a card
for having history is exploitable by anyone patient enough to build history
first.** That is exactly the bust-out pattern.

Reported as a single "precision" column, both would read 0% and someone would
delete `THREE_DS_OK` — raising the score on every authenticated customer, and
punishing the people who did the right thing.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/decide` | Score a payment. Idempotent by `external_id`. |
| `POST` | `/v1/labels` | Attach an outcome to a past transaction. |
| `GET` | `/v1/performance` | Confusion matrix, per-rule stats, label coverage. |
| `GET` | `/v1/backtest` | Replay history against a candidate ruleset. |
| `GET` | `/v1/review-queue` | Open cases, most urgent SLA first. |
| `POST` | `/v1/review-cases/{id}/resolve` | Record an analyst verdict. |
| `POST` | `/v1/lists` | Add an entity to allow / deny / watch. |
| `GET` | `/v1/features` | The feature registry. |
| `GET` | `/health` · `/ready` | Liveness (no DB) and readiness (DB). |

**Errors are typed**, not generic: `400` validation · `404` not found ·
`409` conflict · `422` unprocessable.

**`extra="forbid"` on the request model.** A typo like `card_numbr` returns 422
rather than deciding the payment with no card at all and reporting total
confidence about it.

**Optional and nullable are different**, and the schema draws the line
deliberately: `currency` may be omitted *or* sent as `null`; `channel` may be
omitted but **not** sent as `null`.

---

## Not included

Stated plainly, because a list of what was **not** built is what makes the rest
credible.

| | |
|---|---|
| **No authentication** | The API is unauthenticated. A real deployment needs API keys or mTLS before anything else. |
| **No ML model** | A model trained on synthetic data proves nothing about fraud. Rules first; a model belongs on real labelled traffic. |
| **No Redis or feature store** | Velocity is computed from Postgres and measured at p95 = 3ms. Caching would be optimisation without evidence — add it when measurement demands it. |
| **No scheduler** | Nothing expires review cases past SLA on a timer. |
| **Not PCI-DSS compliant** | Card numbers are never stored, but scope, key management and network segmentation are not addressed. |
| **Single-region, single-writer** | No sharding, no read replicas. |

---

## Stack

**Python 3.12** · **FastAPI** · **Pydantic v2** · **PostgreSQL 16** ·
raw SQL via **psycopg 3** *(no ORM — the SQL is the point)* ·
**pytest** · **ruff** · **uv** · **Docker** · **GitHub Actions**

---

## Licence

**All Rights Reserved** — see [LICENSE](LICENSE).

© 2026 Sief Alsheyab

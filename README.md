# fraud-engine

Real-time payment fraud decision engine. A transaction arrives, and within a
latency budget the system returns **APPROVE / CHALLENGE / REVIEW / DECLINE**
with the exact reasons — plus a permanent, reproducible record of every input
value that produced that answer.

[![CI](https://github.com/seif-alsheyab/fraud-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/seif-alsheyab/fraud-engine/actions/workflows/ci.yml)

---

## The problem this solves

A payment arrives. You have roughly 200 milliseconds to answer one question:
*is the person holding this card the person who owns it?* You can never know.
You are always betting.

**Both ways of being wrong cost money, and they cost it differently.** Approve
a fraudster and you lose the goods, the money, and pay a chargeback fee.
Decline a real customer and you lose the sale — often the customer permanently.

A system that blocks everything has zero fraud and zero revenue. A system that
approves everything bleeds. Measured on 1,000 transactions with a realistic
0.5% fraud rate:

| Strategy | Recall | Precision | Approval rate | Fraud losses |
|---|---|---|---|---|
| Block everything | 1.000 | 0.005 | 0.000 | 0 bps |
| Approve everything | 0.000 | n/a | 1.000 | 197 bps |
| A real engine | 0.800 | 0.167 | 0.976 | 39 bps |

The job is not "stop fraud". It is finding the cheapest mixture of both
mistakes — and *whether 20 blocked customers is worth 1 caught fraud* is a
business decision the numbers inform but never answer.

---

## Architecture

HTTP ──────────────────────────────────────────────────────
api/ FastAPI, Pydantic validation, redacted logs
│
services/ orchestration: what must happen atomically
│
domain/ pure functions: rules, scoring, metrics, hashing
│
repositories/ SQL only, no business rules
│
PostgreSQL constraints, triggers, rules stored as data
─────────────────────────────────────────────────────────────

Three decisions shape everything:

**Rules are data, not code.** A rule is a row; a ruleset is a versioned
collection; exactly one ruleset per merchant is ACTIVE, enforced by a partial
unique index. A fraud attack starts at 02:00 — changing a threshold must not
require a code review, a build and a release. It also means you can answer
"which rules were live on 14 March?" and backtest a rule you have not written
yet.

**Rules are interpreted, never `eval`'d.** A condition is JSON walked by code
that knows six operators. It cannot call a function, import a module, or read
a file. A hostile operator is rejected when the rule is *saved*:

144 tests, and the Python metrics match the raw SQL rule-for-rule. The two-table split is now the sharpest artefact in the repo:

THREE_DS_OK    407 fired   0 on fraud   error rate 0.000   ← 3DS works
TRUSTED_CARD     6 fired   4 on fraud   error rate 0.667   ← being exploited

Same kind, opposite verdicts. A single precision column would have shown both as "0% precision" and told you nothing.

Now the final piece: CI, README, and push.

bash
cat > /tmp/fe_step12.sh <<'FE12EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$HOME/projects/fraud-engine" || { echo "ABORT: project dir missing"; exit 1; }

echo ">> pre-flight"
[ "$(gh api user --jq .login)" = "seif-alsheyab" ] || { echo "ABORT: wrong gh account"; exit 1; }
[ "$(git config user.email)" = "alsheyab.seif@gmail.com" ] || { echo "ABORT: identity drift"; exit 1; }
gh repo view seif-alsheyab/fraud-engine >/dev/null 2>&1 && { echo "ABORT: repo already exists"; exit 1; }
echo "   ok"

cat > .github/workflows/ci.yml <<'YMLEOF'
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest

    strategy:
      fail-fast: false
      # 3.12 is the minimum pyproject allows; 3.13 catches a dependency that
      # quietly needs a newer runtime than the manifest claims.
      matrix:
        python-version: ["3.12", "3.13"]

    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: fraud
          POSTGRES_PASSWORD: fraud_dev_pw
          POSTGRES_DB: fraud_engine
        ports:
          - 5432:5432
        # Without a health check the job races the database and fails
        # intermittently -- the worst kind of CI failure, because it looks
        # like a flaky test rather than a missing dependency.
        options: >-
          --health-cmd "pg_isready -U fraud -d fraud_engine"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 20

    env:
      # CI reaches the service container on 5432. Local development uses 5434
      # to avoid colliding with other Postgres instances on the machine.
      DATABASE_URL: postgresql://fraud:fraud_dev_pw@localhost:5432/fraud_engine
      # Test-only. A real deployment rotates this per environment; a leaked
      # salt makes every pseudonymised identifier brute-forceable.
      ENTITY_HASH_SALT: ci_only_salt_at_least_16_chars
      APP_ENV: test
      LOG_LEVEL: error
      DECISION_LATENCY_BUDGET_MS: 250

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Set Python ${{ matrix.python-version }}
        run: uv python pin ${{ matrix.python-version }}

      # --frozen installs exactly what uv.lock pins and fails if the lock and
      # the manifest disagree. Without it CI would silently resolve newer
      # versions and test something different from local.
      - name: Install dependencies
        run: uv sync --extra dev --frozen

      - name: Lint
        run: uv run ruff check src/ tests/ scripts/

      - name: Run migrations
        run: uv run python -m fraud_engine.db.migrate

      - name: Migrations are idempotent
        run: uv run python -m fraud_engine.db.migrate

      - name: Test suite
        run: uv run pytest -q

      # Running twice proves the suites clean up after themselves. A suite
      # that only passes on a virgin database is not a suite you can trust.
      - name: Test suite again (proves cleanup is repeatable)
        run: uv run pytest -q

      - name: Generate synthetic data
        run: uv run python scripts/generate.py --seed 42 --days 90 --volume 800 --fraud-rate 0.02

      # The stronger guarantee: the suite must pass on a POPULATED database.
      # An empty-database pass only shows the tests coexist with emptiness.
      - name: Test suite against populated data
        run: uv run pytest -q

      - name: Smoke test the running server
        run: |
          uv run python -m fraud_engine.api.server > /tmp/server.log 2>&1 &
          SERVER_PID=$!
          for i in $(seq 1 25); do
            curl -sf http://127.0.0.1:4020/health >/dev/null && break
            sleep 1
          done
          echo "health   -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4020/health)"
          echo "ready    -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4020/ready)"
          echo "features -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4020/v1/features)"

          # The minimal payload: the exact case that returned 500 before the
          # currency fix. Regression-guarded in CI, not just in a unit test.
          curl -sf -X POST http://127.0.0.1:4020/v1/decide \
            -H 'content-type: application/json' \
            -d '{"merchant_code":"DEMO-SHOP","external_id":"ci-minimal","amount_minor":25000}' \
            | head -c 200
          echo

          # A card number must never reach a log line.
          curl -s -o /dev/null -X POST http://127.0.0.1:4020/v1/decide \
            -H 'content-type: application/json' \
            -d '{"merchant_code":"DEMO-SHOP","external_id":"ci-pan","amount_minor":5000,
                 "card_number":"4111111111111111"}'
          if grep -q '4111111111111111' /tmp/server.log; then
            echo "FAIL: card number found in the server log"; exit 1
          fi
          echo "confirmed: no PAN in the log"

          kill -TERM $SERVER_PID
          wait $SERVER_PID 2>/dev/null || true
YMLEOF

cat > LICENSE <<'EOF'
MIT License

Copyright (c) 2026 Sief Alsheyab

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
EOF

cat > README.md <<'READMEEOF'
# fraud-engine

Real-time payment fraud decision engine. A transaction arrives, and within a
latency budget the system returns **APPROVE / CHALLENGE / REVIEW / DECLINE**
with the exact reasons — plus a permanent, reproducible record of every input
value that produced that answer.

[![CI](https://github.com/seif-alsheyab/fraud-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/seif-alsheyab/fraud-engine/actions/workflows/ci.yml)

---

## The problem this solves

A payment arrives. You have roughly 200 milliseconds to answer one question:
*is the person holding this card the person who owns it?* You can never know.
You are always betting.

**Both ways of being wrong cost money, and they cost it differently.** Approve
a fraudster and you lose the goods, the money, and pay a chargeback fee.
Decline a real customer and you lose the sale — often the customer permanently.

A system that blocks everything has zero fraud and zero revenue. A system that
approves everything bleeds. Measured on 1,000 transactions with a realistic
0.5% fraud rate:

| Strategy | Recall | Precision | Approval rate | Fraud losses |
|---|---|---|---|---|
| Block everything | 1.000 | 0.005 | 0.000 | 0 bps |
| Approve everything | 0.000 | n/a | 1.000 | 197 bps |
| A real engine | 0.800 | 0.167 | 0.976 | 39 bps |

The job is not "stop fraud". It is finding the cheapest mixture of both
mistakes — and *whether 20 blocked customers is worth 1 caught fraud* is a
business decision the numbers inform but never answer.

---

## Architecture

HTTP ──────────────────────────────────────────────────────
api/ FastAPI, Pydantic validation, redacted logs
│
services/ orchestration: what must happen atomically
│
domain/ pure functions: rules, scoring, metrics, hashing
│
repositories/ SQL only, no business rules
│
PostgreSQL constraints, triggers, rules stored as data
─────────────────────────────────────────────────────────────


Three decisions shape everything:

**Rules are data, not code.** A rule is a row; a ruleset is a versioned
collection; exactly one ruleset per merchant is ACTIVE, enforced by a partial
unique index. A fraud attack starts at 02:00 — changing a threshold must not
require a code review, a build and a release. It also means you can answer
"which rules were live on 14 March?" and backtest a rule you have not written
yet.

**Rules are interpreted, never `eval`'d.** A condition is JSON walked by code
that knows six operators. It cannot call a function, import a module, or read
a file. A hostile operator is rejected when the rule is *saved*:

condition: {"feature": "amount_minor", "op": "import('os').system", ...}
→ rejected: unknown operator 'import('os').system'


Storing conditions as strings and calling `eval` turns "let the risk team edit
rules" into "let the risk team run arbitrary code on the payment server".

**Domain functions receive their data, never fetch it.** `decide()` is handed
the rules and features; it does not query for them. That is why the unit tests
run in milliseconds with no database, and why the same function is exercised
against fake data in unit tests and real rows in integration tests.

---

## Card numbers are never stored

The engine does not need to *know* a card number. It only needs to recognise
that this is the **same** card as before, and a salted hash does that exactly
as well:

input 4111111111111111
stored 288ba8933c3aa75c55fb5431b4e7c4066f9172aa2ff82935f6e882aff2c57467
displayed 1111


Salting is not optional. Card numbers have low entropy — 16 digits, of which
the BIN is known and the last is a checksum — so an unsalted hash of every
possible card is precomputable. With a secret salt it is not.

Identifiers are **normalised before hashing**, or the whole scheme fails
silently: `Sief@Gmail.com`, `s.ief@gmail.com` and `sief+shop@gmail.com` are one
mailbox. Hash them raw and you get three unrelated entities, and every velocity
counter reads zero for a returning customer.

Logs are separately guarded. A denylist scrubs `card_number`, `email`,
`ip_address`, `device_fingerprint` and the salt itself from every log line, and
CI fails if a PAN appears in the server log. Log lines outlive databases.

---

## What it detects

**Velocity.** A normal person buys once, maybe twice. A stolen card gets tested
with small charges then drained fast. No individual payment looks suspicious —
the pattern does.

**Shared attributes across accounts.** Ten accounts, ten names, ten emails, all
sharing one device fingerprint. Each order is unremarkable alone; one human is
operating all ten. This is a graph problem hiding inside a payments problem,
and it is invisible unless you deliberately look *across* rows.

**Entity age and history.** A card first seen four seconds ago is a different
proposition from one first seen two years ago.

**Lists.** Allow / deny / watch, with TTL — because a permanent block on an IP
address is a mistake: addresses are reassigned and a stranger inherits the
punishment. A deny entry beats any number of good signals, including 3DS.

---

## Every decision is reproducible

Six weeks after you approved something, a chargeback arrives and someone asks
why. If you stored only `"APPROVED"`, you cannot answer. If you re-run the
rules today you get *today's* rules and *today's* velocity counters — a
different answer to a different question.

So each decision stores the exact feature snapshot and the exact ruleset
version, forever. `decisions` is append-only, enforced by a database trigger:

```sql
UPDATE decisions SET decision='APPROVE';
ERROR:  decisions is append-only; UPDATE is not permitted
```

There is one deliberate door for retention purges and test fixtures — an
explicit `set_config('fraud.allow_decision_purge','on',true)` in the same
transaction. That is the difference between *impossible* and *only possible on
purpose*.

The same snapshots make backtesting honest: a candidate ruleset is replayed
against the questions the engine actually faced, not today's versions of them.

---

## Running it

Requires Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), and Docker.

```bash
git clone https://github.com/seif-alsheyab/fraud-engine.git
cd fraud-engine
cp .env.example .env
uv sync --extra dev

docker compose up -d                          # Postgres 16 on localhost:5434
uv run python -m fraud_engine.db.migrate
uv run python scripts/generate.py             # ~1,400 synthetic transactions
uv run python -m fraud_engine.api.server      # API on localhost:4020
```

Then:

```bash
curl -s 'localhost:4020/v1/performance?merchant_code=DEMO-SHOP&days=120' | jq
curl -s 'localhost:4020/v1/review-queue' | jq
curl -s -X POST localhost:4020/v1/decide -H 'content-type: application/json' \
  -d '{"merchant_code":"DEMO-SHOP","external_id":"test-1","amount_minor":25000}'
```

Interactive API docs at `localhost:4020/docs`.

### Tests

```bash
uv run pytest -q     # 144 tests
```

Unit tests touch no database. Integration tests run inside a transaction that
is always rolled back. HTTP tests commit (a request uses its own pool
connection and cannot see uncommitted rows) and clean up by prefix scope.

CI runs the suite **three times**: empty database, again to prove cleanup is
repeatable, then a third time against a database populated with 1,400 demo
transactions. A suite that only passes on a virgin database is not a suite you
can trust.

---

## About the data

**It is synthetic, and that is a deliberate choice.** Public fraud datasets are
PCA-anonymised — features are unnamed components `V1..V28`, so a *rule* written
against them is meaningless to a human and cannot be explained to a risk
committee. They suit a model demo, not a rule engine.

Generating the data means the ground truth is exact. Four fraud patterns are
injected, each labelled at creation:

| Pattern | Signature |
|---|---|
| Card testing | one card, 5–12 charges in minutes, CVV failing, amounts escalating |
| Account takeover | established account, sudden new device + country + card |
| Fraud ring | many accounts and emails, one shared device or card |
| Bust-out | quiet account, long good history, then spending far above it |

Fraud is labelled `CHARGEBACK` with a realistic 30–75 day delay. Legitimate
transactions are labelled `ASSUMED_GOOD` — honest naming, because "no
chargeback arrived" is an assumption, not proof.

### Measured results (seed 42, 1,378 transactions, 2.47% fraud)

TP=19 FP=0 FN=15 TN=1344
precision=1.000 recall=0.559 FPR=0.000 approval=0.986
latency: avg 2.5ms, p95 3ms, max 7ms, 0 breaches of the 250ms budget


**Recall is 56%, and that is reported rather than tuned away.** The engine
misses 15 frauds worth ~$17,000 — mostly bust-out, which no current rule
catches because none compares an amount to the account's *own* history. A demo
where the engine catches everything is a demo that is lying.

Two caveats on the numbers, stated because they would otherwise mislead:

- **Every detection rule shows lift 40.5.** That is arithmetic, not insight:
  all of them scored precision 1.0, and lift = precision ÷ base rate. Lift only
  discriminates when precision varies, which it does on real traffic and does
  not on clean synthetic data.
- **Precision 1.000 with zero false positives is unrealistically good.** Real
  traffic contains ambiguous transactions that synthetic generation does not
  reproduce. Treat these figures as evidence the pipeline works end to end, not
  as a claim about real-world performance.

### One finding worth reading

Rules are split into **detection** (positive weight, judged on precision) and
**protective** (negative weight, judged on how often they shielded fraud):

DETECTION rules fired on fraud precision
GEO_MISMATCH 30 30 1.000
LINK_CARD_ACCOUNTS 13 13 1.000
VEL_CARD_1H 11 11 1.000

PROTECTIVE rules fired on fraud error rate
THREE_DS_OK 407 0 0.000
TRUSTED_CARD 6 4 0.667


`THREE_DS_OK` fires 407 times and never on fraud — 3DS authentication works.
`TRUSTED_CARD` fired on 4 frauds out of 6: a rule that rewards a card for
having history is exploitable by anyone patient enough to build history first.
That is exactly the bust-out pattern.

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
| `GET` | `/health`, `/ready` | Liveness (no DB) and readiness (DB). |

`extra="forbid"` on the request model: a typo like `card_numbr` returns 422
rather than deciding the payment with no card and reporting confidence about
it.

`currency` may be omitted or sent as null; `channel` may be omitted but **not**
sent as null. Optional and nullable are different, and the schema draws the
line deliberately.

---

## Not included

Stated plainly, because a list of what was *not* built is what makes the rest
credible:

- **No authentication or authorisation.** The API is unauthenticated. A real
  deployment needs API keys or mTLS before anything else.
- **No machine-learning model.** A model trained on synthetic data proves
  nothing about fraud. Rules first; a model belongs on real labelled traffic.
- **No Redis or feature store.** Velocity is computed from Postgres and
  measured at p95 = 3ms, so caching would be optimisation without evidence.
  Add it when measurement demands it, not before.
- **No scheduler.** Nothing expires review cases past SLA on a timer.
- **Not PCI-DSS compliant, and not claimed to be.** Card numbers are never
  stored, but scope, key management and network segmentation are not addressed.
- **Single-region, single-writer.** No sharding, no read replicas.

---

## Stack

Python 3.12 · FastAPI · Pydantic v2 · PostgreSQL 16 · raw SQL via psycopg 3
(no ORM — the SQL is the point) · pytest · ruff · uv · Docker · GitHub Actions

## Licence

MIT

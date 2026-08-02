"""Synthetic transaction generator with known ground truth.

STATED PLAINLY: this data is synthetic. No real fraud dataset is used, and
none is claimed. That is a deliberate choice, not a shortcut:

  * Public fraud datasets (the Kaggle credit-card set being the usual one)
    are PCA-anonymised. Features are unnamed components V1..V28, so a RULE
    written against them means nothing to a human and cannot be explained to
    a risk committee. They suit a model demo, not a rule engine.
  * Generating the data means the ground truth is EXACT. Every transaction
    is fraud or legitimate by construction, so precision and recall are
    measured against certainty rather than a proxy.
  * The fraud patterns below are the ones that actually occur, so the rules
    that catch them are the rules a real team writes.

FRAUD PATTERNS INJECTED
  1. Card testing      one stolen card, many small charges in minutes,
                       usually with CVV failures, escalating in value.
  2. Account takeover  an established account suddenly transacting from a
                       new device and country, on a new card.
  3. Fraud ring        many accounts, many emails, ONE shared device or
                       card. Invisible per transaction; obvious across them.
  4. Bust-out          a quiet, well-behaved account that suddenly spends
                       far above its own history.

Each fraudulent transaction is labelled FRAUD at generation time, with a
realistic 30-75 day delay to mimic chargeback arrival. Legitimate ones are
labelled with source ASSUMED_GOOD -- honest about the fact that "no
chargeback arrived" is an assumption, not proof.
"""

import argparse
import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from fraud_engine.config import get_settings
from fraud_engine.db.pool import close_pool, connection, open_pool

SCOPE = "DEMO"

FIRST = ["ahmad", "maha", "omar", "layla", "sara", "khalid", "nour", "yousef",
         "rana", "tariq", "hana", "zaid", "dina", "sami", "lina"]
LAST = ["alsheyab", "haddad", "khoury", "nasser", "aziz", "farah", "salem",
        "mansour", "darwish", "qasem"]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "proton.me"]
COUNTRIES = ["JO", "AE", "SA", "EG", "KW", "QA", "GB", "US", "DE"]

# Real BIN ranges are proprietary; these are structurally plausible and
# clearly fictional. is_prepaid matters: prepaid cards are bought with cash
# and carry no identity, so they are over-represented in fraud.
BINS = [
    ("411111", "Demo Issuer A", "JO", "VISA", "CREDIT", False),
    ("424242", "Demo Issuer B", "AE", "VISA", "DEBIT", False),
    ("510510", "Demo Issuer C", "SA", "MASTERCARD", "CREDIT", False),
    ("545454", "Demo Issuer D", "GB", "MASTERCARD", "DEBIT", False),
    ("400000", "Demo Prepaid X", "US", "VISA", "PREPAID", True),
    ("520000", "Demo Prepaid Y", "GB", "MASTERCARD", "PREPAID", True),
]

RULES = [
    ("VEL_CARD_1H", "Card used 4+ times in an hour",
     {"feature": "velocity_card_1h", "op": "gte", "value": 4}, 35, None),
    ("VEL_CARD_24H", "Card used 10+ times in a day",
     {"feature": "velocity_card_24h", "op": "gte", "value": 10}, 25, None),
    ("NO_CVV", "CVV did not match",
     {"feature": "cvv_match", "op": "eq", "value": False}, 30, None),
    ("NO_AVS", "AVS did not match",
     {"feature": "avs_match", "op": "eq", "value": "NONE"}, 15, None),
    ("PREPAID_HIGH", "Prepaid card above 500",
     {"all": [{"feature": "is_prepaid_card", "op": "eq", "value": True},
              {"feature": "amount_minor", "op": "gte", "value": 50000}]}, 30, None),
    ("GEO_MISMATCH", "IP country differs from billing country",
     {"feature": "ip_billing_country_match", "op": "eq", "value": False}, 20, None),
    ("NEW_CARD_HIGH", "Brand new card, high value",
     {"all": [{"feature": "card_age_days", "op": "lte", "value": 0},
              {"feature": "amount_minor", "op": "gte", "value": 80000}]}, 25, None),
    ("LINK_CARD_ACCOUNTS", "One card across 3+ accounts",
     {"feature": "accounts_per_card_30d", "op": "gte", "value": 3}, 50, None),
    ("LINK_DEVICE_ACCOUNTS", "One device across 4+ accounts",
     {"feature": "accounts_per_device_30d", "op": "gte", "value": 4}, 45, None),
    ("THREE_DS_OK", "3-D Secure authenticated",
     {"feature": "three_ds_status", "op": "eq", "value": "AUTHENTICATED"}, -35, None),
    ("TRUSTED_CARD", "Card with 10+ prior transactions",
     {"feature": "card_seen_count", "op": "gte", "value": 10}, -20, None),
    ("DENY_LIST", "Entity on the deny list",
     {"feature": "on_deny_list", "op": "eq", "value": True}, 0, "DECLINE"),
]


class Generator:
    def __init__(self, seed: int, days: int, volume: int, fraud_rate: float) -> None:
        # Seeded: the same seed reproduces the same dataset exactly, so a
        # backtest result can be re-derived rather than taken on trust.
        self.rng = random.Random(seed)
        self.days = days
        self.volume = volume
        self.fraud_rate = fraud_rate
        self.now = datetime.now(UTC)
        self.salt = get_settings().entity_hash_salt
        self.rows: list[dict] = []

    # ---- helpers -------------------------------------------------------
    def person(self) -> tuple[str, str]:
        name = f"{self.rng.choice(FIRST)}.{self.rng.choice(LAST)}"
        return name, f"{name}{self.rng.randint(1, 999)}@{self.rng.choice(DOMAINS)}"

    def card(self, prepaid: bool = False) -> tuple[str, str]:
        pool = [b for b in BINS if b[5] == prepaid] or BINS
        bin_row = self.rng.choice(pool)
        return bin_row[0], bin_row[0] + "".join(
            str(self.rng.randint(0, 9)) for _ in range(10)
        )

    def when(self, days_ago: float) -> datetime:
        return self.now - timedelta(days=days_ago)

    def txn(self, **kw) -> dict:
        """One transaction with sane defaults, overridden per pattern."""
        base = {
            "amount_minor": self.rng.randint(1500, 40000),
            "currency": "USD",
            "cvv_match": True,
            "avs_match": "FULL",
            "three_ds_status": "NOT_USED",
            "is_card_present": False,
            "channel": "WEB",
            "is_fraud": False,
            "pattern": "legitimate",
        }
        base.update(kw)
        self.rows.append(base)
        return base

    # ---- legitimate ----------------------------------------------------
    def legitimate_customers(self, count: int) -> None:
        """Ordinary people buying things at ordinary intervals."""
        for _ in range(count):
            name, email = self.person()
            bin_v, pan = self.card()
            device = f"dev-{self.rng.randint(100000, 999999)}"
            country = self.rng.choice(COUNTRIES)
            account = f"acct-{name}-{self.rng.randint(100, 999)}"
            ip = f"{self.rng.randint(10, 200)}.{self.rng.randint(0, 255)}." \
                 f"{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}"

            # A returning customer: several purchases spread over the period.
            for _ in range(self.rng.randint(1, 6)):
                self.txn(
                    card_number=pan, card_bin=bin_v, email=email, account_id=account,
                    device_fingerprint=device, ip_address=ip,
                    ip_country=country, billing_country=country,
                    shipping_country=country,
                    three_ds_status=self.rng.choice(
                        ["NOT_USED", "NOT_USED", "AUTHENTICATED"]
                    ),
                    occurred_at=self.when(self.rng.uniform(0.5, self.days)),
                )

    # ---- fraud patterns ------------------------------------------------
    def card_testing(self) -> None:
        """One stolen card, a burst of charges, CVV failing.

        The signature is TIME: five to twelve attempts inside an hour, each
        individually small. No single charge is remarkable.
        """
        bin_v, pan = self.card()
        start = self.rng.uniform(1, self.days)
        attempts = self.rng.randint(5, 12)
        for i in range(attempts):
            name, email = self.person()
            self.txn(
                card_number=pan, card_bin=bin_v, email=email,
                account_id=f"acct-test-{self.rng.randint(1000, 9999)}",
                device_fingerprint=f"dev-{self.rng.randint(100000, 999999)}",
                ip_address=f"185.{self.rng.randint(0,255)}.{self.rng.randint(0,255)}.{self.rng.randint(1,254)}",
                ip_country="NL", billing_country=self.rng.choice(["US", "GB"]),
                # Amounts climb: probe small, then drain.
                amount_minor=self.rng.randint(500, 2000) if i < attempts - 2
                             else self.rng.randint(40000, 150000),
                cvv_match=self.rng.random() > 0.4,
                avs_match=self.rng.choice(["NONE", "NONE", "PARTIAL"]),
                occurred_at=self.when(start - i * (1 / 24 / 12)),  # ~5 min apart
                is_fraud=True, pattern="card_testing",
            )

    def account_takeover(self) -> None:
        """A real account, then a sudden change in device, country and card.

        The account has genuine history, which is what makes this hard: the
        entity is old and trusted right up until it is not.
        """
        name, email = self.person()
        account = f"acct-{name}-{self.rng.randint(100, 999)}"
        home = self.rng.choice(["JO", "AE", "SA"])
        good_device = f"dev-{self.rng.randint(100000, 999999)}"
        good_bin, good_pan = self.card()

        for _ in range(self.rng.randint(4, 8)):
            self.txn(
                card_number=good_pan, card_bin=good_bin, email=email,
                account_id=account, device_fingerprint=good_device,
                ip_address=f"37.{self.rng.randint(0,255)}.{self.rng.randint(0,255)}.{self.rng.randint(1,254)}",
                ip_country=home, billing_country=home, shipping_country=home,
                amount_minor=self.rng.randint(2000, 25000),
                occurred_at=self.when(self.rng.uniform(self.days * 0.4, self.days)),
            )

        # Takeover: new device, new country, new card, bigger amounts.
        bad_device = f"dev-{self.rng.randint(100000, 999999)}"
        bad_bin, bad_pan = self.card(prepaid=True)
        for i in range(self.rng.randint(2, 4)):
            self.txn(
                card_number=bad_pan, card_bin=bad_bin, email=email,
                account_id=account, device_fingerprint=bad_device,
                ip_address=f"91.{self.rng.randint(0,255)}.{self.rng.randint(0,255)}.{self.rng.randint(1,254)}",
                ip_country="RU", billing_country=home,
                shipping_country=self.rng.choice(["GB", "US"]),
                amount_minor=self.rng.randint(60000, 200000),
                cvv_match=True, avs_match="PARTIAL",
                occurred_at=self.when(self.rng.uniform(0.5, 3) - i * 0.02),
                is_fraud=True, pattern="account_takeover",
            )

    def fraud_ring(self) -> None:
        """Many identities, one device or one card behind them.

        This is the pattern a single transaction CANNOT reveal. Each order
        has a different name, email and account, and looks completely
        ordinary. Only looking across rows exposes the shared attribute.
        """
        shared_device = f"dev-{self.rng.randint(100000, 999999)}"
        share_card = self.rng.random() < 0.5
        shared_bin, shared_pan = self.card()
        members = self.rng.randint(4, 9)

        for _ in range(members):
            name, email = self.person()
            bin_v, pan = (shared_bin, shared_pan) if share_card else self.card()
            for _ in range(self.rng.randint(1, 3)):
                self.txn(
                    card_number=pan, card_bin=bin_v, email=email,
                    account_id=f"acct-{name}-{self.rng.randint(100, 999)}",
                    device_fingerprint=shared_device,
                    ip_address=f"45.{self.rng.randint(0,255)}.{self.rng.randint(0,255)}.{self.rng.randint(1,254)}",
                    ip_country="VN", billing_country=self.rng.choice(["US", "GB", "DE"]),
                    amount_minor=self.rng.randint(15000, 90000),
                    cvv_match=True, avs_match=self.rng.choice(["FULL", "PARTIAL"]),
                    occurred_at=self.when(self.rng.uniform(1, self.days * 0.7)),
                    is_fraud=True, pattern="fraud_ring",
                )

    def bust_out(self) -> None:
        """A quiet account that suddenly spends far above its own history.

        Every signal looks good -- same card, same device, same country, CVV
        and AVS matching. Only the amount, relative to this account's OWN
        past, is wrong. Deliberately included as a pattern the current rules
        do NOT catch: an honest system shows its blind spots.
        """
        name, email = self.person()
        account = f"acct-{name}-{self.rng.randint(100, 999)}"
        device = f"dev-{self.rng.randint(100000, 999999)}"
        bin_v, pan = self.card()
        country = self.rng.choice(["JO", "AE"])
        ip = f"37.{self.rng.randint(0,255)}.{self.rng.randint(0,255)}.{self.rng.randint(1,254)}"

        for _ in range(self.rng.randint(6, 12)):
            self.txn(
                card_number=pan, card_bin=bin_v, email=email, account_id=account,
                device_fingerprint=device, ip_address=ip,
                ip_country=country, billing_country=country, shipping_country=country,
                amount_minor=self.rng.randint(1000, 8000),
                occurred_at=self.when(self.rng.uniform(self.days * 0.3, self.days)),
            )

        for i in range(self.rng.randint(2, 4)):
            self.txn(
                card_number=pan, card_bin=bin_v, email=email, account_id=account,
                device_fingerprint=device, ip_address=ip,
                ip_country=country, billing_country=country, shipping_country=country,
                amount_minor=self.rng.randint(150000, 400000),
                occurred_at=self.when(self.rng.uniform(0.5, 2) - i * 0.05),
                is_fraud=True, pattern="bust_out",
            )

    def build(self) -> list[dict]:
        target_fraud = int(self.volume * self.fraud_rate)
        while sum(1 for r in self.rows if r["is_fraud"]) < target_fraud:
            roll = self.rng.random()
            if roll < 0.35:
                self.card_testing()
            elif roll < 0.60:
                self.fraud_ring()
            elif roll < 0.85:
                self.account_takeover()
            else:
                self.bust_out()

        remaining = max(0, self.volume - len(self.rows))
        self.legitimate_customers(max(1, remaining // 3))

        self.rows.sort(key=lambda r: r["occurred_at"])
        return self.rows


async def wipe(conn) -> None:
    """Remove any previous DEMO run. Children before parents."""
    await conn.execute("SELECT set_config('fraud.allow_decision_purge','on',true)")
    scoped = """SELECT t.id FROM transactions t
                  JOIN merchants m ON m.id = t.merchant_id
                 WHERE m.code LIKE 'DEMO%'"""
    demo_merchants = "SELECT id FROM merchants WHERE code LIKE 'DEMO%'"
    demo_rulesets = f"SELECT id FROM rulesets WHERE merchant_id IN ({demo_merchants})"

    # Order is forced by the foreign keys: children before parents.
    for sql in [
        f"""DELETE FROM review_cases WHERE decision_id IN (
              SELECT d.id FROM decisions d WHERE d.transaction_id IN ({scoped}))""",
        f"DELETE FROM labels WHERE transaction_id IN ({scoped})",
        f"DELETE FROM decisions WHERE transaction_id IN ({scoped})",
        f"DELETE FROM transactions WHERE id IN ({scoped})",
        f"DELETE FROM list_entries WHERE merchant_id IN ({demo_merchants})",
        f"DELETE FROM rules WHERE ruleset_id IN ({demo_rulesets})",
        f"DELETE FROM rulesets WHERE merchant_id IN ({demo_merchants})",
        "DELETE FROM merchants WHERE code LIKE 'DEMO%'",
    ]:
        await conn.execute(sql)


async def setup_merchant(conn) -> dict:
    for b in BINS:
        await conn.execute(
            """INSERT INTO card_bins (bin, issuer_name, issuer_country, brand,
                                      card_type, is_prepaid)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (bin) DO NOTHING""", b
        )

    cur = await conn.execute(
        """INSERT INTO merchants (code, name, vertical, country, currency)
           VALUES ('DEMO-SHOP','Demo Electronics','DIGITAL','JO','USD') RETURNING *"""
    )
    merchant = await cur.fetchone()

    cur = await conn.execute(
        """INSERT INTO rulesets (merchant_id, version, name, status,
                                 challenge_at, review_at, decline_at)
           VALUES (%s,1,'baseline','ACTIVE',40,60,85) RETURNING *""",
        (merchant["id"],),
    )
    ruleset = await cur.fetchone()

    for code, name, cond, weight, hard in RULES:
        await conn.execute(
            """INSERT INTO rules (ruleset_id, code, name, condition, weight, hard_action)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (ruleset["id"], code, name, Jsonb(cond), weight, hard),
        )

    return {"merchant": merchant, "ruleset": ruleset}


async def run(seed: int, days: int, volume: int, fraud_rate: float) -> None:
    from fraud_engine.services.decision_service import decide_payment

    gen = Generator(seed, days, volume, fraud_rate)
    rows = gen.build()

    fraud_count = sum(1 for r in rows if r["is_fraud"])
    print(f"generated {len(rows)} transactions, {fraud_count} fraudulent "
          f"({fraud_count / len(rows) * 100:.2f}%)")
    by_pattern: dict[str, int] = {}
    for r in rows:
        by_pattern[r["pattern"]] = by_pattern.get(r["pattern"], 0) + 1
    for k in sorted(by_pattern):
        print(f"  {k:<20} {by_pattern[k]}")

    await open_pool()
    try:
        async with connection() as conn:
            print("\nwiping previous DEMO data")
            await wipe(conn)
            ctx = await setup_merchant(conn)
            merchant = ctx["merchant"]

        print("deciding each transaction through the real service layer")
        for i, r in enumerate(rows, start=1):
            payload = {
                "merchant_code": merchant["code"],
                "external_id": f"DEMO-{seed}-{i - 1:06d}",
                "amount_minor": r["amount_minor"],
                "currency": r["currency"],
                "card_number": r.get("card_number"),
                "card_bin": r.get("card_bin"),
                "email": r.get("email"),
                "device_fingerprint": r.get("device_fingerprint"),
                "ip_address": r.get("ip_address"),
                "account_id": r.get("account_id"),
                "ip_country": r.get("ip_country"),
                "billing_country": r.get("billing_country"),
                "shipping_country": r.get("shipping_country"),
                "avs_match": r.get("avs_match"),
                "cvv_match": r.get("cvv_match"),
                "three_ds_status": r.get("three_ds_status"),
                "is_card_present": r.get("is_card_present", False),
                "channel": r.get("channel", "WEB"),
                "occurred_at": r["occurred_at"],
            }
            # Each decision gets its own connection/transaction, exactly as a
            # live request would. Slower than a bulk insert, and the point:
            # the demo data is produced by the real pipeline, so velocity and
            # linking accumulate the way they do in production.
            async with connection() as conn:
                result = await decide_payment(conn, payload, now=r["occurred_at"])
                r["decision"] = result["decision"]
                r["score"] = result["score"]
                r["transaction_id"] = result["transaction_id"]

            if i % 200 == 0:
                print(f"  {i}/{len(rows)}")

        print("attaching labels (the truth, arriving weeks later)")
        async with connection() as conn:
            for r in rows:
                if r["is_fraud"]:
                    label, source = "FRAUD", "CHARGEBACK"
                    delay = gen.rng.randint(30, 75)
                else:
                    # Honest naming: no chargeback arriving is an ASSUMPTION,
                    # not proof the transaction was good.
                    label, source = "LEGITIMATE", "ASSUMED_GOOD"
                    delay = gen.rng.randint(45, 90)
                await conn.execute(
                    """INSERT INTO labels (transaction_id, label, source, reason_code,
                                           amount_minor, labelled_at, days_to_label)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (transaction_id, source) DO NOTHING""",
                    (r["transaction_id"], label, source,
                     "10.4" if label == "FRAUD" else None,
                     r["amount_minor"], r["occurred_at"] + timedelta(days=delay), delay),
                )

        # A candidate ruleset, so `/v1/backtest` has something to compare.
        async with connection() as conn:
            cur = await conn.execute(
                """INSERT INTO rulesets (merchant_id, version, name, status,
                                         challenge_at, review_at, decline_at)
                   VALUES (%s,2,'stricter candidate','DRAFT',30,45,70) RETURNING *""",
                (merchant["id"],),
            )
            candidate = await cur.fetchone()
            for code, name, cond, weight, hard in RULES:
                bumped = weight + 10 if weight > 0 else weight
                await conn.execute(
                    """INSERT INTO rules (ruleset_id, code, name, condition, weight, hard_action)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (candidate["id"], code, name, Jsonb(cond), bumped, hard),
                )

        print("\n" + "=" * 62)
        print(" RESULT")
        print("=" * 62)
        print(f"merchant code        : {merchant['code']}")
        print(f"merchant id          : {merchant['id']}")
        print(f"candidate ruleset id : {candidate['id']}")
        print()
        print("try:")
        print(f"  curl -s 'localhost:4020/v1/performance"
              f"?merchant_code={merchant['code']}&days=120' | jq")
        print(f"  curl -s 'localhost:4020/v1/backtest?merchant_code={merchant['code']}"
              f"&candidate_ruleset_id={candidate['id']}&days=120' | jq '.delta'")
        print("  curl -s 'localhost:4020/v1/review-queue' | jq")
    finally:
        await close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate synthetic transactions with known ground truth."
    )
    ap.add_argument("--seed", type=int, default=42,
                    help="Same seed reproduces the same dataset exactly.")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--volume", type=int, default=1200)
    ap.add_argument("--fraud-rate", type=float, default=0.02,
                    help="Target share of fraudulent transactions.")
    args = ap.parse_args()

    if not 0 < args.fraud_rate < 0.5:
        print("fraud-rate must be between 0 and 0.5", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(args.seed, args.days, args.volume, args.fraud_rate))


if __name__ == "__main__":
    main()

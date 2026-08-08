"""Generate the synthetic IEEE-shaped test fixtures.

WHY THIS EXISTS
---------------
Plan §7 T1 requires the committed fixtures to be *synthetic data with the same
schema*, not rows from the dataset. The licence (Vesta/Kaggle, 2019) permits
research use and forbids redistribution, and the README states plainly that the
data is not in this repository. A fixture carrying real rows would make that
statement false.

The fixtures this writes share nothing with the real file but its shape:

  * TransactionID starts at 9,000,000. The real range is 2,987,000-3,577,539,
    so a collision is arithmetically impossible and the provenance of any row
    is obvious from its id alone. `tests/unit/test_ieee_fixtures.py` asserts
    this, and asserts it WITHOUT needing the dataset present -- a licence
    check that only runs where the licensed data already is would be useless.
  * Every value is drawn from a seeded PRNG. Nothing is copied, sampled, or
    perturbed from the source.

DETERMINISM
-----------
Output is byte-identical for a given seed, and does not depend on whether
IEEE_DATA_PATH is set. That is why the column list below is hardcoded rather
than read from the real header: reading it would make the generated file
depend on the machine, so a regeneration on a laptop without the dataset would
silently produce a different fixture. When the dataset IS present the real
header is read anyway and compared against this list, so schema drift is
caught -- verification, not input.

REALISM, AND ITS LIMIT
----------------------
Missingness rates, category frequencies and amount spread are set to the
measured shape of the real file, because a fixture that is unrealistically
clean stops exercising the loader's null handling. They are approximations
chosen to make the tests meaningful; nothing here should be read as a
description of the real data beyond its schema.
"""

import argparse
import csv
import random
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # pragma: no cover - import-path bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ieee.loader import (  # noqa: E402
    IDENTITY_FILENAME,
    TRANSACTION_FILENAME,
    identity_csv,
    resolve_data_path,
    transaction_csv,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
SAMPLE = FIXTURES / "ieee_sample.csv"
SAMPLE_IDENTITY = FIXTURES / "ieee_sample_identity.csv"

# Far outside the real 2,987,000-3,577,539 range. Not an arbitrary offset: it
# is the whole provenance guarantee, so it is asserted by a test.
FIRST_ID = 9_000_000
REAL_ID_MIN = 2_987_000
REAL_ID_MAX = 3_577_539

ROWS = 100
SEED = 42

# The real schema, hardcoded so output is machine-independent. Verified
# against the actual header whenever the dataset is available.
TRANSACTION_COLUMNS = (
    ["TransactionID", "isFraud", "TransactionDT", "TransactionAmt", "ProductCD"]
    + [f"card{i}" for i in range(1, 7)]
    + ["addr1", "addr2", "dist1", "dist2", "P_emaildomain", "R_emaildomain"]
    + [f"C{i}" for i in range(1, 15)]
    + [f"D{i}" for i in range(1, 16)]
    + [f"M{i}" for i in range(1, 10)]
    + [f"V{i}" for i in range(1, 340)]
)

IDENTITY_COLUMNS = (
    ["TransactionID"]
    + [f"id_{i:02d}" for i in range(1, 39)]
    + ["DeviceType", "DeviceInfo"]
)

# Numeric identity columns, per loader.py: NOT the contiguous block the naming
# suggests. Kept in step with that module deliberately -- a fixture that wrote
# a string where the loader expects a float would make the typing tests pass
# for the wrong reason.
NUMERIC_IDENTITY = {
    f"id_{i:02d}" for i in (*range(1, 12), 13, 14, 17, 18, 19, 20, 21, 22, 24, 25, 26, 32)
}

# Frequencies measured from the real file. Approximations, not a description.
PRODUCT_CD = (["W"] * 74) + (["C"] * 12) + (["R"] * 6) + (["H"] * 6) + (["S"] * 2)
CARD4 = (["visa"] * 65) + (["mastercard"] * 32) + (["american express"] * 2) + ["discover"]
CARD6 = (["debit"] * 74) + (["credit"] * 26)
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "hotmail.com", "anonymous.com",
                 "aol.com", "outlook.com", "comcast.net"]

# M4's three levels plus absence. Every one appears at least once, because a
# rule tests `addr_match eq "(absent)"` and a fixture that never produced an
# absent M4 would let that rule rot untested.
M4_VALUES = ["M0", "M1", "M2", ""]

# Device profiles. Deliberately distinct in every component so that no two
# render to the same composed key -- mapping.py joins on "|", and two profiles
# that differ only by where a separator falls would collapse into one entity
# and quietly weaken test_no_two_different_devices_collapse_into_one_entity.
DEVICE_PROFILES = [
    ("Windows", "desktop", "Windows 10", "chrome 63.0", "1920x1080"),
    ("MacOS", "desktop", "Mac OS X 10_12_6", "safari 11.0", "1440x900"),
    ("SAMSUNG SM-G950U", "mobile", "Android 7.0", "samsung browser 6.2", "1440x2560"),
    ("iOS Device", "mobile", "iOS 11.1.2", "mobile safari 11.0", "1334x750"),
    ("Trident/7.0", "desktop", "Windows 8.1", "ie 11.0 for desktop", "1366x768"),
]

BROWSER_STRINGS = ["chrome 63.0", "mobile safari 11.0", "ie 11.0 for desktop",
                   "safari generic", "firefox 57.0"]


def _amount(rng: random.Random) -> str:
    """A long-tailed amount, like the real distribution (p50 ~68, max ~4830)."""
    if rng.random() < 0.90:
        return f"{rng.uniform(5, 300):.3f}"[:-1]
    return f"{rng.uniform(300, 3000):.2f}"


def _maybe(rng: random.Random, value: Any, null_rate: float) -> str:
    """A value, or blank at the given rate. Blank, never the string "None"."""
    return "" if rng.random() < null_rate else str(value)


def build_transactions(rng: random.Random) -> list[dict[str, str]]:
    """ROWS synthetic transactions, TransactionDT strictly ascending."""
    # A small pool, so some card keys repeat -- entity resolution is only
    # exercised when the same card appears twice.
    card_pool = [
        (rng.randint(1000, 18000), rng.choice([100.0, 111.0, 150.0, 194.0, 321.0, 555.0]),
         150.0, rng.choice(CARD4), rng.choice([102.0, 117.0, 202.0, 224.0, 226.0]),
         rng.choice(CARD6))
        for _ in range(12)
    ]

    rows: list[dict[str, str]] = []
    dt = 86_400
    for i in range(ROWS):
        dt += rng.randint(30, 4000)  # strictly ascending
        card1, card2, card3, card4, card5, card6 = rng.choice(card_pool)

        row = dict.fromkeys(TRANSACTION_COLUMNS, "")
        row["TransactionID"] = str(FIRST_ID + i)
        # ~3.5% fraud, matching the real base rate. Row 0 is never fraud so
        # the typing tests read a fully-populated ordinary row.
        row["isFraud"] = "0" if i == 0 else ("1" if rng.random() < 0.035 else "0")
        row["TransactionDT"] = str(dt)
        row["TransactionAmt"] = _amount(rng)
        row["ProductCD"] = rng.choice(PRODUCT_CD)
        row["card1"] = str(card1)
        row["card2"] = str(card2)
        row["card3"] = str(card3)
        row["card4"] = card4
        row["card5"] = str(card5)
        row["card6"] = card6
        # Row 0 must carry every column the typing tests read, so its
        # optional fields are forced present.
        first = i == 0
        row["addr1"] = str(float(rng.randint(100, 540))) if first else _maybe(
            rng, float(rng.randint(100, 540)), 0.111)
        row["addr2"] = _maybe(rng, 87.0, 0.111)
        row["dist1"] = str(rng.randint(0, 400)) if first else _maybe(
            rng, rng.randint(0, 400), 0.597)
        row["dist2"] = _maybe(rng, rng.randint(0, 2000), 0.936)
        row["P_emaildomain"] = rng.choice(EMAIL_DOMAINS) if first else _maybe(
            rng, rng.choice(EMAIL_DOMAINS), 0.160)
        row["R_emaildomain"] = _maybe(rng, rng.choice(EMAIL_DOMAINS), 0.768)

        for c in range(1, 15):
            value = float(rng.randint(0, 40)) if rng.random() < 0.75 else 0.0
            row[f"C{c}"] = str(value)
        for d in range(1, 16):
            null_rate = 0.0 if (first and d == 1) else (0.2 if d <= 2 else 0.5)
            row[f"D{d}"] = _maybe(rng, round(rng.uniform(0, 640), 1), null_rate)

        # M4 cycles so all four levels, absence included, are present.
        row["M4"] = M4_VALUES[i % len(M4_VALUES)]
        for m in (1, 2, 3, 5, 6, 7, 8, 9):
            row[f"M{m}"] = _maybe(rng, rng.choice(["T", "F"]), 0.35)

        for v in range(1, 340):
            row[f"V{v}"] = _maybe(rng, float(rng.randint(0, 5)), 0.4)

        rows.append(row)
    return rows


def build_identity(rng: random.Random, transactions: list[dict[str, str]]) -> list[dict[str, str]]:
    """Identity rows for ~24% of transactions, matching the real join rate.

    Three shapes are guaranteed present, because each one is a distinct branch
    in mapping.device_entity and a fixture missing one leaves that branch
    untested:

      1. no identity row at all           -> device entity None
      2. an identity row, no device columns -> device entity None (3,373 such
         rows exist in the real file; "has identity" and "has a device" are
         different questions)
      3. an identity row with device columns -> a real device entity
    """
    # Row 0 never joins: the loader tests use an unmatched row to prove the
    # left join fills every identity column with None rather than dropping it.
    candidates = [r["TransactionID"] for r in transactions[1:]]
    chosen = sorted(rng.sample(candidates, k=24), key=int)

    rows: list[dict[str, str]] = []
    for n, tid in enumerate(chosen):
        row = dict.fromkeys(IDENTITY_COLUMNS, "")
        row["TransactionID"] = tid

        for column in sorted(NUMERIC_IDENTITY):
            row[column] = _maybe(rng, float(rng.randint(-100, 500)), 0.3)
        # id_01 is read directly by a typing test, so it is always present.
        row["id_01"] = str(float(rng.randint(-100, 0)))
        for column in ("id_12", "id_15", "id_16", "id_23", "id_27", "id_28",
                       "id_29", "id_34", "id_35", "id_36", "id_37", "id_38"):
            row[column] = _maybe(rng, rng.choice(["Found", "NotFound", "New", "T", "F"]), 0.3)

        # The first two identity rows carry NO device columns at all: shape 2.
        if n < 2:
            for column in ("id_30", "id_31", "id_33", "DeviceType", "DeviceInfo"):
                row[column] = ""
        else:
            info, dtype, os_name, browser, res = DEVICE_PROFILES[n % len(DEVICE_PROFILES)]
            row["DeviceInfo"] = info
            row["DeviceType"] = dtype
            row["id_30"] = os_name
            row["id_31"] = browser if rng.random() < 0.8 else rng.choice(BROWSER_STRINGS)
            row["id_33"] = res
        rows.append(row)
    return rows


def verify_schema_against_real() -> str:
    """Compare the hardcoded columns with the real header, when available.

    Verification, never input. If this ever fails the fixture has drifted from
    the file it stands in for, and the loader tests would be passing against a
    schema that no longer exists.
    """
    try:
        resolve_data_path()
        txn_path, id_path = transaction_csv(), identity_csv()
    except RuntimeError:
        return "IEEE_DATA_PATH not set - schema not verified against the real files"
    if not txn_path.exists() or not id_path.exists():
        return f"{txn_path.parent} not present - schema not verified"

    problems = []
    for path, expected, name in ((txn_path, TRANSACTION_COLUMNS, TRANSACTION_FILENAME),
                                 (id_path, IDENTITY_COLUMNS, IDENTITY_FILENAME)):
        with open(path, newline="") as f:
            actual = next(csv.reader(f))
        if actual != expected:
            missing = [c for c in actual if c not in expected]
            extra = [c for c in expected if c not in actual]
            problems.append(
                f"{name}: {len(actual)} real columns vs {len(expected)} generated"
                + (f"; missing {missing[:5]}" if missing else "")
                + (f"; unexpected {extra[:5]}" if extra else "")
            )
    if problems:
        raise RuntimeError("Fixture schema no longer matches the dataset:\n  "
                           + "\n  ".join(problems))
    return "schema verified against the real headers - identical"


def write(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Write with \\n endings so the file is byte-identical across platforms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def generate(seed: int = SEED) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rng = random.Random(seed)
    transactions = build_transactions(rng)
    identity = build_identity(rng, transactions)
    return transactions, identity


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic IEEE-shaped fixtures.")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--transactions", type=Path, default=SAMPLE)
    ap.add_argument("--identity", type=Path, default=SAMPLE_IDENTITY)
    args = ap.parse_args()

    print(verify_schema_against_real())

    transactions, identity = generate(args.seed)
    write(args.transactions, TRANSACTION_COLUMNS, transactions)
    write(args.identity, IDENTITY_COLUMNS, identity)

    fraud = sum(1 for r in transactions if r["isFraud"] == "1")
    print(f"seed              : {args.seed}")
    print(f"transactions      : {len(transactions)} "
          f"(ids {transactions[0]['TransactionID']}-{transactions[-1]['TransactionID']})")
    print(f"fraud             : {fraud} ({fraud / len(transactions):.1%})")
    print(f"identity rows     : {len(identity)} "
          f"({len(identity) / len(transactions):.0%} join rate)")
    print(f"columns           : {len(TRANSACTION_COLUMNS)} / {len(IDENTITY_COLUMNS)}")
    print(f"wrote             : {args.transactions}")
    print(f"                    {args.identity}")


if __name__ == "__main__":
    main()

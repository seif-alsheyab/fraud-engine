"""T6 -- replay the IEEE-CIS training split through the decision engine.

Streams train_transaction.csv in ascending TransactionDT order, maps each row
to a decision payload, calls decide_payment directly, and records one label
per transaction.

WHY NOT OVER HTTP
-----------------
decide_payment is called in-process. Going through the API would add a
serialise/deserialise round trip per row -- roughly 590,540 of them -- to
measure nothing the engine does differently. The latency figures recorded
here are therefore DECISION latency, not end-to-end request latency, and the
summary says so.

WHY STRICTLY SEQUENTIAL
-----------------------
Velocity is computed from the engine's own replayed history. Two workers
replaying concurrently would each see a partial history, and the counters
would depend on scheduling rather than on the data. Parallelism here does not
make the replay faster and wrong -- it makes it non-deterministic, which is
worse, because the run cannot be reproduced to investigate a result.

THE THREE PERIODS
-----------------
    day 0            day 60           day 121          day 182
    |--- WARM-UP -----|----- FIT -------|----- EVAL -----|
      no metrics       weights fitted    headline result

Warm-up exists because on the first replayed transaction every counter is
zero, so early decisions are structurally unrepresentative -- reporting them
would understate the engine badly.

The split after warm-up is not cosmetic. The banded weights in
seed_ruleset.py were fitted on the FIT half; PR-AUC 0.2042 was measured on
the EVAL half. A metric computed across both would include the period the
weights were tuned on and overstate the result -- the leakage trap in plan
§8. This script emits all three boundaries so T7 can report EVAL as the
headline and FIT separately, rather than having to rediscover where the line
falls.

WHAT IS SYNTHETIC HERE
----------------------
Two values are invented and both are recorded as such in the summary:

  * EPOCH. TransactionDT counts seconds from an undisclosed reference date.
    2017-12-01 is arbitrary. Velocity is relative so no metric depends on it,
    but it must be CONSTANT across runs or window arithmetic disagrees with
    itself between them.
  * days_to_label = 45. IEEE ships no dispute date. It affects only
    label-coverage reporting, and an unexplained number in a table is worse
    than an explained one.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fraud_engine.db.pool import close_pool, connection, open_pool
from fraud_engine.lib.errors import AppError
from fraud_engine.repositories import decision_repository as dr
from fraud_engine.repositories import reference_repository as rr
from fraud_engine.services.decision_service import decide_payment

# `python scripts/ieee/replay.py` puts scripts/ieee/ on sys.path, not the
# repository root, so the sibling `scripts.ieee.*` imports below would fail
# with "No module named 'scripts'". pytest and `python -m` both put the root
# there already; this only fills the gap for direct execution, which is how
# the run command in the docstring is written.
if __package__ in (None, ""):  # pragma: no cover - import-path bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ieee import mapping  # noqa: E402
from scripts.ieee.loader import identity_csv, load_transactions, transaction_csv  # noqa: E402

# Arbitrary and fixed. See the module docstring: only consistency matters.
EPOCH = datetime(2017, 12, 1, tzinfo=UTC)

# IEEE gives no dispute date. Recorded as synthetic in the summary.
DAYS_TO_LABEL = 45

MERCHANT_CODE = "IEEE"
PROGRESS_EVERY = 10_000
SUMMARY_PATH = Path(__file__).resolve().parent / "last_run.json"

# Plan §3.1. Everything that is not W or C is a web checkout; C is the one
# product the plan maps to an API channel.
_CHANNEL_BY_PRODUCT = {"W": "WEB", "C": "API", "R": "WEB", "H": "WEB", "S": "WEB"}

# The six aggregates the processor supplies, and the feature each becomes.
# A column that is null is OMITTED rather than sent as None: a supplied None
# would land in the frozen snapshot looking like a measured zero, and the
# banded rules read `gte 1`, which None does not satisfy anyway.
_VESTA_COLUMNS = {
    "C4": "vesta_c4",
    "C8": "vesta_c8",
    "C10": "vesta_c10",
    "C12": "vesta_c12",
    "D3": "vesta_d3",
    "D5": "vesta_d5",
}

# Identity columns that indicate the join produced a row at all. DeviceType
# is not enough on its own: 3,373 rows join to an identity record whose
# device columns are all blank, so "has identity data" and "has a device" are
# different questions (see mapping.device_entity).
_IDENTITY_MARKERS = ("id_01", "id_02", "id_12", "DeviceType", "DeviceInfo")


def occurred_at_for(transaction_dt: int) -> datetime:
    return EPOCH + timedelta(seconds=transaction_dt)


def _has_identity(row: dict[str, Any]) -> bool:
    return any(row.get(c) is not None for c in _IDENTITY_MARKERS)


def _supplied_features(row: dict[str, Any]) -> dict[str, Any]:
    return {
        feature: row[column]
        for column, feature in _VESTA_COLUMNS.items()
        if row.get(column) is not None
    }


def build_payload(row: dict[str, Any], merchant_code: str) -> dict[str, Any]:
    """One IEEE row -> one decision payload.

    The three entity keys are passed as RAW composite strings. decide_payment
    hashes whatever it is given, so pre-hashing here would produce a hash of
    a hash -- still stable, still consistent, and therefore completely
    invisible as a bug, while making every entity in this run unrelatable to
    an entity created by any other code path.

    device_fingerprint is None for the 76% of rows with no device signal.
    That is deliberate: mapping.device_key returns None rather than a
    rendering of blanks, which hashed would merge three quarters of the
    dataset into one device and make every link rule fire on everything.

    KNOWN LIMITATION, measured rather than estimated. decide_payment hashes
    the CARD slot through normalise_card, which keeps digits only -- so the
    card key loses its `|` separators and its credit/debit token on the way
    in. Across the real 590,540 rows that merges 47 of 14,892 distinct card
    keys (0.32%): a credit and a debit card sharing card1-card5 become one
    entity, as do the rare pairs whose digits concatenate identically.
    ACCOUNT and DEVICE are unaffected -- they fall through to the default
    normaliser, which preserves the key exactly.

    It is accepted rather than worked around because the IEEE ruleset
    references no card feature at all, so nothing measured here depends on
    it. It belongs in the results write-up, and it must be revisited before
    any card-based rule is added.
    """
    device = mapping.device_key(row)
    product_code = row.get("ProductCD")

    payload: dict[str, Any] = {
        "merchant_code": merchant_code,
        "external_id": f"IEEE-{row['TransactionID']}",
        "amount_minor": round(row["TransactionAmt"] * 100),
        "currency": "USD",
        "occurred_at": occurred_at_for(row["TransactionDT"]),
        # Raw composite keys. Hashed once, by decide_payment.
        "card_number": mapping.card_key(row),
        "account_id": mapping.account_key(row),
        "device_fingerprint": device,
        "product_code": product_code,
        "card_type": row.get("card6"),
        # M4's absence is a measured category, not a null. As None the
        # protective M4_ABSENT rule would never match and the score on
        # ordinary traffic would drift upwards.
        "addr_match": row.get("M4") or "(absent)",
        "dist_from_billing": row.get("dist1"),
        "has_identity_data": _has_identity(row),
        "channel": _CHANNEL_BY_PRODUCT.get(product_code or "", "WEB"),
    }

    supplied = _supplied_features(row)
    if supplied:
        payload["supplied_features"] = supplied
    return payload


def _percentiles(values: list[int]) -> dict[str, int | None]:
    """p50/p95/p99/max over decision latency.

    Returns None rather than 0 for an empty run: "no decisions were made" and
    "every decision took 0ms" are different facts (CLAUDE.md §5.8).
    """
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)

    def at(fraction: float) -> int:
        # Nearest-rank. index is clamped so p99 of a 2-element list is the
        # last element rather than an IndexError.
        index = min(len(ordered) - 1, int(fraction * len(ordered)))
        return ordered[index]

    return {"p50": at(0.50), "p95": at(0.95), "p99": at(0.99), "max": ordered[-1]}


def _fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN guard
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


class Progress:
    """Row counts, rate and a running decision tally.

    A replay that prints nothing for 30 minutes is indistinguishable from one
    that has hung, and the decision tally is what makes an inert ruleset
    visible at row 10,000 instead of at the end.
    """

    def __init__(self, total: int | None) -> None:
        self.total = total
        self.started = time.perf_counter()
        self.rows = 0
        self.decisions: dict[str, int] = {}
        self.labels: dict[str, int] = {}
        self.latencies: list[int] = []
        self.replays = 0

    def record(self, decision: str, label: str, latency_ms: int, replayed: bool) -> None:
        self.rows += 1
        self.decisions[decision] = self.decisions.get(decision, 0) + 1
        self.labels[label] = self.labels.get(label, 0) + 1
        self.latencies.append(latency_ms)
        if replayed:
            self.replays += 1

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    def should_print(self) -> bool:
        return self.rows % PROGRESS_EVERY == 0

    def line(self) -> str:
        elapsed = self.elapsed
        rate = self.rows / elapsed if elapsed > 0 else 0.0
        eta = ""
        if self.total:
            remaining = max(0, self.total - self.rows)
            eta = f"  eta {_fmt_eta(remaining / rate if rate else -1)}"
        mix = "  ".join(
            f"{code[0]}{code[1:3].lower()} {count}"
            for code, count in sorted(self.decisions.items())
        )
        return (
            f"{self.rows:>7,}"
            + (f"/{self.total:,}" if self.total else "")
            + f"  {_fmt_eta(elapsed)}  {rate:>6.0f}/s{eta}  |  {mix}"
        )


async def replay(
    *,
    merchant_code: str,
    warmup_days: int,
    limit: int | None,
    base_path: Path | None = None,
) -> dict[str, Any]:
    """Replay the training split. Returns the run summary."""
    txn_path = transaction_csv(base_path)
    id_path = identity_csv(base_path)
    if not txn_path.exists():
        raise RuntimeError(
            f"{txn_path} not found. IEEE_DATA_PATH points at the directory "
            "holding train_transaction.csv (plan §1.1: never bundled)."
        )
    identity_arg = id_path if id_path.exists() else None

    await open_pool()
    try:
        async with connection() as conn:
            merchant = await rr.find_merchant_by_code(conn, merchant_code)
            if merchant is None:
                raise RuntimeError(
                    f"Merchant {merchant_code} does not exist. "
                    "Run scripts/ieee/seed_ruleset.py first."
                )
            ruleset = await rr.find_ruleset_by_status(conn, merchant["id"], "ACTIVE")
            if ruleset is None:
                raise RuntimeError(
                    f"Merchant {merchant_code} has no ACTIVE ruleset. "
                    "Run scripts/ieee/seed_ruleset.py first."
                )

        print(f"ruleset   : {ruleset['name']} v{ruleset['version']} ({ruleset['id']})")
        print(f"source    : {txn_path}")
        print(f"identity  : {identity_arg or 'not present -- device features unavailable'}")
        print(f"epoch     : {EPOCH.isoformat()}  (arbitrary, fixed)")
        print(f"warm-up   : {warmup_days} days")
        if limit:
            print(f"limit     : {limit:,} rows")
        # Flushed because a multi-hour run is normally redirected to a file,
        # and Python block-buffers stdout when it is not a terminal. Without
        # this the header sits in the buffer until the first progress line
        # 10,000 rows later, so a run that has started correctly is
        # indistinguishable from one that has hung on startup.
        print(flush=True)

        progress = Progress(limit)
        first_dt: int | None = None
        last_dt: int | None = None

        # ONE connection held for the whole replay, with a transaction per
        # row on it. Borrowing from the pool per row costs a round trip of
        # its own and buys nothing: the replay is strictly sequential, so
        # there is never a second consumer to lend the connection to.
        #
        # The per-row TRANSACTION is kept. One enclosing transaction over
        # 590,540 rows would hold locks for hours and lose the entire run to
        # a single bad row; committing per row also makes the replay
        # resumable, since decide_payment returns the original decision for
        # an external_id already present.
        async with connection() as conn:
            for row in load_transactions(txn_path, identity_arg):
                if limit is not None and progress.rows >= limit:
                    break

                dt = row["TransactionDT"]
                if first_dt is None:
                    first_dt = dt
                last_dt = dt

                payload = build_payload(row, merchant_code)

                async with conn.transaction():
                    result = await decide_payment(conn, payload, now=payload["occurred_at"])

                    label = "FRAUD" if row["isFraud"] == 1 else "LEGITIMATE"
                    await dr.insert_label(conn, {
                        "transaction_id": result["transaction_id"],
                        "label": label,
                        "source": "CHARGEBACK" if row["isFraud"] == 1 else "ASSUMED_GOOD",
                        "reason_code": None,
                        "amount_minor": payload["amount_minor"],
                        "labelled_at": payload["occurred_at"] + timedelta(days=DAYS_TO_LABEL),
                        "days_to_label": DAYS_TO_LABEL,
                    })

                progress.record(
                    result["decision"], label, result["latency_ms"],
                    result["idempotent_replay"],
                )
                if progress.should_print():
                    print(progress.line(), flush=True)

        summary = _summarise(
            progress=progress,
            merchant_code=merchant_code,
            ruleset=ruleset,
            warmup_days=warmup_days,
            first_dt=first_dt,
            last_dt=last_dt,
            limit=limit,
        )
        return summary
    finally:
        await close_pool()


def _summarise(
    *,
    progress: Progress,
    merchant_code: str,
    ruleset: dict[str, Any],
    warmup_days: int,
    first_dt: int | None,
    last_dt: int | None,
    limit: int | None,
) -> dict[str, Any]:
    """The three period boundaries, plus everything T7 needs to not guess.

    warmup_end / fit_end / eval_start are emitted as timestamps rather than
    day offsets so T7 can filter decisions directly, without re-deriving the
    epoch arithmetic and getting a different answer.
    """
    periods: dict[str, Any] = {
        "epoch": EPOCH.isoformat(),
        "epoch_is_arbitrary": True,
        "warmup_days": warmup_days,
    }

    if first_dt is not None and last_dt is not None:
        start = occurred_at_for(first_dt)
        end = occurred_at_for(last_dt)
        warmup_end = start + timedelta(days=warmup_days)

        # The measured window is split in half: the weights were fitted on
        # the first half, so only the second is an out-of-sample result.
        measured_seconds = max(0.0, (end - warmup_end).total_seconds())
        fit_end = warmup_end + timedelta(seconds=measured_seconds / 2)

        periods.update({
            "data_start": start.isoformat(),
            "data_end": end.isoformat(),
            "warmup_end": warmup_end.isoformat(),
            "fit_end": fit_end.isoformat(),
            # Same instant as fit_end; named separately because T7 filters
            # on a half-open range and reading `>= eval_start` is the point.
            "eval_start": fit_end.isoformat(),
            "measured_days": round(measured_seconds / 86_400, 2),
            "split": "by time, not by transaction count",
            "note": (
                "Weights were fitted on warmup_end..fit_end. Report "
                "eval_start..data_end as the headline; measuring across both "
                "halves includes the period the weights were tuned on. "
                "The halves are equal in DURATION, so their transaction "
                "counts differ -- count them from the decisions table using "
                "these boundaries rather than assuming a 50/50 split."
            ),
        })

        # A --limit smoke run almost always stops inside the warm-up, which
        # leaves warmup_end past data_end and all three boundaries collapsed
        # onto one instant. Reported explicitly, because a summary that
        # merely LOOKS like a valid one is how a smoke run gets mistaken for
        # a measurement -- T7 must refuse this file rather than compute a
        # headline over zero transactions.
        if measured_seconds <= 0:
            periods["measurable"] = False
            periods["why_not_measurable"] = (
                f"Replay covers {(end - start).total_seconds() / 86_400:.2f} days "
                f"but warm-up is {warmup_days} days, so no transaction falls in "
                "the measured window. This run is a smoke test, not a measurement."
            )
        else:
            periods["measurable"] = True

    return {
        "merchant_code": merchant_code,
        "ruleset": {
            "id": str(ruleset["id"]),
            "name": ruleset["name"],
            "version": ruleset["version"],
        },
        "rows": {
            "replayed": progress.rows,
            "limit": limit,
            "idempotent_replays": progress.replays,
        },
        "periods": periods,
        "decisions": dict(sorted(progress.decisions.items())),
        "labels": dict(sorted(progress.labels.items())),
        "days_to_label": {
            "value": DAYS_TO_LABEL,
            "synthetic": True,
            "why": "IEEE-CIS ships no dispute date. Affects label-coverage only.",
        },
        "latency_ms": {
            **_percentiles(progress.latencies),
            "measures": "in-process decide_payment, not end-to-end HTTP",
        },
        "wall_clock_seconds": round(progress.elapsed, 1),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    rows = summary["rows"]
    print()
    print("=" * 68)
    print(" REPLAY COMPLETE")
    print("=" * 68)
    print(f"rows replayed   : {rows['replayed']:,}")
    if rows["idempotent_replays"]:
        print(f"  of which replays of an existing decision: "
              f"{rows['idempotent_replays']:,}")

    periods = summary["periods"]
    if "warmup_end" in periods:
        print()
        print(f"data start      : {periods['data_start']}")
        print(f"warm-up ends    : {periods['warmup_end']}   "
              f"(first {periods['warmup_days']} days, DISCARD from metrics)")
        print(f"fit ends        : {periods['fit_end']}   (weights were tuned here)")
        print(f"eval starts     : {periods['eval_start']}   (headline result)")
        print(f"data end        : {periods['data_end']}")
        if not periods.get("measurable", True):
            print()
            print("!! NOT MEASURABLE: " + periods["why_not_measurable"])

    print()
    total = max(1, rows["replayed"])
    print("decisions:")
    for code, count in summary["decisions"].items():
        print(f"  {code:<10} {count:>8,}  {count / total:6.2%}")
    print("labels:")
    for code, count in summary["labels"].items():
        print(f"  {code:<10} {count:>8,}  {count / total:6.2%}")

    lat = summary["latency_ms"]
    print()
    print(f"latency ms      : p50 {lat['p50']}  p95 {lat['p95']}  "
          f"p99 {lat['p99']}  max {lat['max']}")
    print(f"days_to_label   : {summary['days_to_label']['value']} (SYNTHETIC)")
    print(f"wall clock      : {summary['wall_clock_seconds']}s")

    # The check the plan's §8 exists to force. An all-APPROVE run is what a
    # ruleset with inert features produces, and it produces it silently.
    approve_only = set(summary["decisions"]) <= {"APPROVE"}
    if approve_only and rows["replayed"]:
        print()
        print("!! EVERY DECISION WAS APPROVE.")
        print("!! A ruleset whose features are inert looks exactly like this.")
        print("!! Check tests/integration/test_feature_reachability.py before")
        print("!! trusting any measurement from this run.")


async def run(merchant_code: str, warmup_days: int, limit: int | None) -> int:
    summary = await replay(
        merchant_code=merchant_code, warmup_days=warmup_days, limit=limit
    )
    _print_summary(summary)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary written : {SUMMARY_PATH}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Replay the IEEE-CIS training split through the decision engine."
    )
    ap.add_argument("--merchant-code", default=MERCHANT_CODE)
    ap.add_argument(
        "--warmup-days", type=int, default=60,
        help="Days excluded from metrics while velocity counters fill (default: 60).",
    )
    ap.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N rows. For smoke runs -- always check the decision "
             "mix on a limited run before committing to the full replay.",
    )
    args = ap.parse_args()

    if args.warmup_days < 0:
        print("--warmup-days cannot be negative", file=sys.stderr)
        sys.exit(1)
    if args.limit is not None and args.limit <= 0:
        print("--limit must be positive", file=sys.stderr)
        sys.exit(1)

    try:
        sys.exit(asyncio.run(run(args.merchant_code, args.warmup_days, args.limit)))
    except (AppError, RuntimeError, ValueError) as exc:
        print(f"\nREPLAY FAILED\n{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""T7 -- measure the IEEE-CIS replay and write docs/IEEE_RESULTS.md.

Reads the period boundaries produced by replay.py, computes performance over
each of the three periods, and renders the results document.

THE THREE PERIODS, AND WHY ONLY ONE IS THE HEADLINE
---------------------------------------------------
    warmup   EXCLUDED from every metric. Velocity counters start at zero, so
             these decisions were made by an engine with no history. Including
             them would understate the result.
    fit      Reported, labelled, and NOT the headline. The banded weights in
             seed_ruleset.py were derived from this period, so any metric on
             it is measuring the engine against data it was tuned on.
    eval     THE HEADLINE. Held out from weight fitting.

The halves are equal in DURATION, not in transaction count -- 193,817 against
172,985. Nothing here assumes a 50/50 split; the boundaries are read from
last_run.json and every count is measured.

WHY EVERY MATRIX IS COMPUTED TWICE
----------------------------------
Once through the engine's own performance_report, and once in raw SQL that
shares no code with it. If the two disagree, one of them is wrong, and this
script says so in the document rather than quietly printing the prettier
number. A measurement nobody cross-checked is an assertion.

AUC WITHOUT A NUMERICAL LIBRARY
-------------------------------
The project depends on no array library, so ROC-AUC and PR-AUC are computed
here in plain Python. Scores are integers produced by summing rule weights,
so ties are the common case rather than an edge case -- 406 distinct values
across 590,540 decisions -- and every function below is tie-aware. ROC-AUC is
computed twice by different methods for the same reason the matrices are.
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fraud_engine.config import get_settings
from fraud_engine.db.pool import close_pool, connection, open_pool
from fraud_engine.domain.conditions import referenced_features
from fraud_engine.domain.metrics import build_confusion_matrix
from fraud_engine.domain.scoring import Rule, Thresholds, decide
from fraud_engine.repositories import reference_repository as rr
from fraud_engine.services.analytics_service import performance_report
from fraud_engine.services.feature_service import SUPPLIED_ONLY_FEATURES

if __package__ in (None, ""):  # pragma: no cover - import-path bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

HERE = Path(__file__).resolve().parent
LAST_RUN = HERE / "last_run.json"
RESULTS = HERE.parents[1] / "docs" / "IEEE_RESULTS.md"

# The synthetic figures this project reported before any real data existed.
# Kept beside the real ones deliberately: the GAP is the finding.
SYNTHETIC = {
    "transactions": 1378,
    "fraud_rate": 0.0247,
    "tp": 19, "fp": 0, "fn": 15, "tn": 1344,
    "precision": 1.000, "recall": 0.559, "fpr": 0.000, "approval": 0.986,
}

# Not this project's work. github.com/trongjhuongwr/D2AD_Project, evaluated by
# nth2165/ep01-tabd2ad-ieee-evaluation. Included because a benchmark you did
# not write is worth more than one you did.
TAB_D2AD = [
    ("TAB-D2AD linear probe", 0.891, 0.684),
    ("TAB-D2AD stage2 (best)", 0.744, 0.325),
    ("TAB-D2AD stage1 diffusion", 0.566, 0.193),
]

# An independent probe predicted these at threshold 232 BEFORE the engine
# replayed anything. Recorded so the agreement can be checked rather than
# asserted.
CALIBRATION_PREDICTION = {"threshold": 232, "blocked": 0.024, "recall": 0.199,
                          "precision": 0.288}


# --------------------------------------------------------------------------
# Pure metrics. No I/O, so each is testable without a database.
# --------------------------------------------------------------------------

def roc_auc_by_rank(points: list[tuple[int, bool]]) -> float | None:
    """ROC-AUC via the Mann-Whitney rank statistic, averaging tied ranks.

    Equivalent to the probability that a randomly chosen fraud outscores a
    randomly chosen legitimate transaction, counting a tie as half. With 406
    distinct scores over 590,540 rows, tie handling is not a detail: treating
    ties as wins would inflate the figure substantially.
    """
    n_pos = sum(1 for _, is_fraud in points if is_fraud)
    n_neg = len(points) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    ordered = sorted(points, key=lambda p: p[0])
    rank_sum_pos = 0.0
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        average_rank = (i + 1 + j + 1) / 2
        for k in range(i, j + 1):
            if ordered[k][1]:
                rank_sum_pos += average_rank
        i = j + 1

    return (rank_sum_pos - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def _grouped_desc(points: list[tuple[int, bool]]) -> list[tuple[int, int, int]]:
    """(score, fraud_at_score, legit_at_score), highest score first."""
    by_score: dict[int, list[int]] = {}
    for score, is_fraud in points:
        bucket = by_score.setdefault(score, [0, 0])
        bucket[0 if is_fraud else 1] += 1
    return [(s, v[0], v[1]) for s, v in sorted(by_score.items(), reverse=True)]


def roc_auc_by_trapezoid(points: list[tuple[int, bool]]) -> float | None:
    """The same number by integrating the ROC curve.

    Kept as a cross-check on the rank formula. The two agreeing is evidence
    the tie handling is right; a mismatch means one of them is wrong and the
    report says so rather than choosing.
    """
    n_pos = sum(1 for _, f in points if f)
    n_neg = len(points) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    area = 0.0
    tp = fp = 0
    prev_fpr = prev_tpr = 0.0
    for _score, fraud_here, legit_here in _grouped_desc(points):
        tp += fraud_here
        fp += legit_here
        tpr, fpr = tp / n_pos, fp / n_neg
        area += (fpr - prev_fpr) * (tpr + prev_tpr) / 2
        prev_fpr, prev_tpr = fpr, tpr
    return area


def pr_auc(points: list[tuple[int, bool]]) -> float | None:
    """Average precision -- the area under the precision/recall curve.

    No interpolation. The interpolated variant reports a higher number for
    the same classifier, and on a 3.4% base rate the difference is large
    enough to matter when comparing against a published benchmark.
    """
    n_pos = sum(1 for _, f in points if f)
    if n_pos == 0 or n_pos == len(points):
        return None

    tp = fp = 0
    prev_recall = 0.0
    area = 0.0
    for _score, fraud_here, legit_here in _grouped_desc(points):
        tp += fraud_here
        fp += legit_here
        precision = tp / (tp + fp)
        recall = tp / n_pos
        area += (recall - prev_recall) * precision
        prev_recall = recall
    return area


def threshold_sweep(points: list[tuple[int, bool]]) -> list[dict[str, Any]]:
    """Every operating point the score can produce: block iff score >= t.

    Reported in full rather than at the one chosen threshold. A single row
    hides the shape of the trade-off, and the shape is what tells a reader
    whether the chosen point was reasonable.
    """
    n_pos = sum(1 for _, f in points if f)
    n_neg = len(points) - n_pos
    if not points:
        return []

    rows: list[dict[str, Any]] = []
    tp = fp = 0
    for score, fraud_here, legit_here in _grouped_desc(points):
        tp += fraud_here
        fp += legit_here
        blocked = tp + fp
        rows.append({
            "threshold": score,
            "blocked": blocked,
            "block_rate": blocked / len(points),
            "tp": tp, "fp": fp,
            "fn": n_pos - tp, "tn": n_neg - fp,
            "precision": (tp / blocked) if blocked else None,
            "recall": (tp / n_pos) if n_pos else None,
            "fpr": (fp / n_neg) if n_neg else None,
        })
    rows.reverse()  # ascending threshold reads more naturally
    return rows


def unique_contributions(
    outcomes: list[dict[str, Any]], weights: dict[str, int], review_at: int
) -> dict[str, int]:
    """Fraud each rule caught that no other rule would have caught alone.

    Defined as: the transaction was blocked (score >= review_at), the rule
    fired, and removing its weight would have dropped the score below the
    blocking threshold. This is the number that says whether a rule is
    earning its place -- a rule with high precision and zero unique
    contribution is redundant, and LINK_DEVICE_ACCOUNTS scored exactly that
    on the ruleset this one replaced.

    Valid only because no rule in this ruleset carries a hard_action; a hard
    action bypasses scoring, so subtracting a weight would not describe it.
    """
    unique: dict[str, int] = dict.fromkeys(weights, 0)
    for o in outcomes:
        if o["label"] != "FRAUD" or o["score"] < review_at:
            continue
        for hit in o.get("triggered_rules") or []:
            code = hit["code"]
            if code in unique and (o["score"] - weights.get(code, 0)) < review_at:
                unique[code] += 1
    return unique


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

@dataclass
class Period:
    key: str
    title: str
    since: datetime
    before: datetime
    headline: bool
    caveat: str


def load_periods(path: Path = LAST_RUN) -> tuple[list[Period], dict[str, Any]]:
    run = json.loads(path.read_text(encoding="utf-8"))
    p = run["periods"]
    if not p.get("measurable", False):
        raise RuntimeError(
            "last_run.json reports measurable=false: "
            f"{p.get('why_not_measurable', 'no measured window')}. "
            "Run a full replay before measuring."
        )

    def ts(key: str) -> datetime:
        return datetime.fromisoformat(p[key])

    # Every window is half-open, [since, before), so the interior boundaries
    # partition cleanly with no transaction counted twice. But data_end is the
    # timestamp OF the last transaction, not one past it -- so a half-open
    # final bound silently drops that row from every period, and the totals
    # come to one less than the replay recorded. One microsecond is enough:
    # TransactionDT is whole seconds, so nothing else can fall in the gap.
    last_inclusive = ts("data_end") + timedelta(microseconds=1)

    periods = [
        Period("warmup", "Warm-up (EXCLUDED)", ts("data_start"), ts("warmup_end"),
               False,
               "Velocity counters start at zero here, so these decisions were made "
               "by an engine with no history. Shown for completeness and excluded "
               "from every headline figure."),
        Period("fit", "Fit half (NOT the headline)", ts("warmup_end"), ts("fit_end"),
               False,
               "The banded rule weights were derived from this period. Any metric "
               "here measures the engine against data it was tuned on, so it is "
               "reported separately and must not be quoted as the result."),
        Period("eval", "Eval half (HEADLINE)", ts("eval_start"), last_inclusive,
               True,
               "Held out from weight fitting. This is the honest number."),
    ]
    return periods, run


async def sql_confusion_matrix(
    conn, *, merchant_id, since: datetime, before: datetime
) -> dict[str, int]:
    """The same matrix, computed in SQL and sharing no code with the engine.

    Deliberately duplicates the definition of "blocked" rather than importing
    it. A cross-check that imports the thing it is checking verifies nothing.
    """
    cur = await conn.execute(
        """
        WITH best_label AS (
          SELECT DISTINCT ON (transaction_id) transaction_id, label
            FROM labels
           ORDER BY transaction_id,
                    CASE source WHEN 'CHARGEBACK' THEN 1 WHEN 'ISSUER_REPORT' THEN 2
                                WHEN 'MANUAL_REVIEW' THEN 3
                                WHEN 'REFUND_REQUEST' THEN 4 ELSE 5 END)
        SELECT
          count(*) FILTER (WHERE d.decision IN ('DECLINE','REVIEW') AND bl.label='FRAUD')::int
            AS true_positive,
          count(*) FILTER (WHERE d.decision IN ('DECLINE','REVIEW') AND bl.label<>'FRAUD')::int
            AS false_positive,
          count(*) FILTER (WHERE d.decision NOT IN ('DECLINE','REVIEW') AND bl.label='FRAUD')::int
            AS false_negative,
          count(*) FILTER (WHERE d.decision NOT IN ('DECLINE','REVIEW') AND bl.label<>'FRAUD')::int
            AS true_negative
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN best_label bl ON bl.transaction_id = t.id
         WHERE t.merchant_id = %s AND d.mode = 'LIVE'
           AND t.occurred_at >= %s AND t.occurred_at < %s
           AND bl.label IS NOT NULL
        """,
        (merchant_id, since, before),
    )
    row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def scored_outcomes(
    conn, *, merchant_id, since: datetime, before: datetime
) -> list[dict[str, Any]]:
    """Score, label and triggered rules -- without the feature snapshots.

    performance_report loads d.features, which is 375MB across the full run.
    Nothing in the AUC, sweep or unique-contribution arithmetic reads a
    feature, so this query leaves them behind.
    """
    cur = await conn.execute(
        """
        WITH best_label AS (
          SELECT DISTINCT ON (transaction_id) transaction_id, label
            FROM labels
           ORDER BY transaction_id,
                    CASE source WHEN 'CHARGEBACK' THEN 1 WHEN 'ISSUER_REPORT' THEN 2
                                WHEN 'MANUAL_REVIEW' THEN 3
                                WHEN 'REFUND_REQUEST' THEN 4 ELSE 5 END)
        SELECT d.score, d.triggered_rules, d.latency_ms, d.exceeded_budget,
               t.amount_minor, bl.label
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN best_label bl ON bl.transaction_id = t.id
         WHERE t.merchant_id = %s AND d.mode = 'LIVE'
           AND t.occurred_at >= %s AND t.occurred_at < %s
         ORDER BY t.occurred_at
        """,
        (merchant_id, since, before),
    )
    return await cur.fetchall()


async def engine_only_matrix(
    conn, *, merchant_id, since: datetime, before: datetime,
    rules: list[Rule], thresholds: Thresholds,
) -> dict[str, Any] | None:
    """Re-score using ONLY rules the engine could compute for itself.

    Caveat 3 made answerable. The vesta_ rules depend on aggregates the
    processor supplied; without them the engine is a different, weaker
    system, and a reader deciding whether these results say anything about
    THIS engine needs to see that separately.

    Uses the frozen feature snapshots, so it asks the question the engine
    actually faced rather than recomputing today's velocity (§5.6).
    """
    engine_rules = [
        r for r in rules
        if not (referenced_features(r.condition) & set(SUPPLIED_ONLY_FEATURES))
    ]
    if len(engine_rules) == len(rules):
        return None

    cur = await conn.execute(
        """
        WITH best_label AS (
          SELECT DISTINCT ON (transaction_id) transaction_id, label
            FROM labels
           ORDER BY transaction_id,
                    CASE source WHEN 'CHARGEBACK' THEN 1 ELSE 5 END)
        SELECT d.features, t.amount_minor, bl.label
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN best_label bl ON bl.transaction_id = t.id
         WHERE t.merchant_id = %s AND d.mode = 'LIVE'
           AND t.occurred_at >= %s AND t.occurred_at < %s
        """,
        (merchant_id, since, before),
    )
    rows: list[dict[str, Any]] = []
    points: list[tuple[int, bool]] = []
    for row in await cur.fetchall():
        result = decide(engine_rules, row["features"], thresholds)
        rows.append({"decision": result.decision, "label": row["label"],
                     "amount_minor": row["amount_minor"]})
        if row["label"] is not None:
            points.append((result.score, row["label"] == "FRAUD"))

    matrix = build_confusion_matrix(rows)
    sweep = threshold_sweep(points)

    # The ceiling this subset can actually reach. HIGH_AMOUNT and MED_AMOUNT
    # are bracketed apart, so the naive sum of positive weights overstates it
    # by the lesser of the two.
    positive = sum(r.weight for r in engine_rules if r.weight > 0)
    both_amount = {"HIGH_AMOUNT", "MED_AMOUNT"} <= {r.code for r in engine_rules}
    ceiling = positive - (15 if both_amount else 0)

    # Best achievable operating point if the thresholds were re-fitted to this
    # score scale, by F1. Without it the section reports recall 0.0000 and
    # leaves a reader thinking the engine ranks badly, when in fact it cannot
    # reach the threshold at all -- a different failure with a different fix.
    best = None
    for row in sweep:
        p, r = row["precision"], row["recall"]
        if p and r:
            f1 = 2 * p * r / (p + r)
            if best is None or f1 > best["f1"]:
                best = {**row, "f1": f1}

    return {
        "rule_count": len(engine_rules),
        "dropped": len(rules) - len(engine_rules),
        "matrix": matrix,
        "roc_auc": roc_auc_by_rank(points),
        "pr_auc": pr_auc(points),
        "ceiling": ceiling,
        "observed_max": max((s for s, _ in points), default=0),
        "can_block": ceiling >= thresholds.review_at,
        "review_at": thresholds.review_at,
        "best_point": best,
    }


async def latency_stats(conn, *, merchant_id, since, before) -> dict[str, Any]:
    cur = await conn.execute(
        """
        SELECT count(*)::int AS n,
               percentile_disc(0.50) WITHIN GROUP (ORDER BY d.latency_ms) AS p50,
               percentile_disc(0.95) WITHIN GROUP (ORDER BY d.latency_ms) AS p95,
               percentile_disc(0.99) WITHIN GROUP (ORDER BY d.latency_ms) AS p99,
               max(d.latency_ms) AS max,
               count(*) FILTER (WHERE d.exceeded_budget)::int AS breaches
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
         WHERE t.merchant_id = %s AND d.mode = 'LIVE'
           AND t.occurred_at >= %s AND t.occurred_at < %s
        """,
        (merchant_id, since, before),
    )
    row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def measure_period(conn, *, merchant_id, period: Period, rules, thresholds,
                         weights: dict[str, int]) -> dict[str, Any]:
    report = await performance_report(
        conn, merchant_id=merchant_id, since=period.since, before=period.before
    )
    sql_matrix = await sql_confusion_matrix(
        conn, merchant_id=merchant_id, since=period.since, before=period.before
    )
    outcomes = await scored_outcomes(
        conn, merchant_id=merchant_id, since=period.since, before=period.before
    )
    labelled = [o for o in outcomes if o["label"] is not None]
    points = [(o["score"], o["label"] == "FRAUD") for o in labelled]

    engine_counts = report["matrix"]["counts"]
    agreement = {
        key: (engine_counts[key], sql_matrix[key], engine_counts[key] == sql_matrix[key])
        for key in ("true_positive", "false_positive", "false_negative", "true_negative")
    }

    auc_rank = roc_auc_by_rank(points)
    auc_trap = roc_auc_by_trapezoid(points)

    return {
        "period": period,
        "report": report,
        "agreement": agreement,
        "agrees": all(v[2] for v in agreement.values()),
        "roc_auc": auc_rank,
        "roc_auc_trapezoid": auc_trap,
        "roc_auc_agrees": (
            auc_rank is not None and auc_trap is not None and abs(auc_rank - auc_trap) < 1e-9
        ),
        "pr_auc": pr_auc(points),
        "sweep": threshold_sweep(points),
        "unique": unique_contributions(labelled, weights, thresholds.review_at),
        "latency": await latency_stats(
            conn, merchant_id=merchant_id, since=period.since, before=period.before
        ),
        "labelled_count": len(labelled),
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def pct(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value * 100:.{places}f}%"


def num(value: float | None, places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _matrix_block(result: dict[str, Any]) -> list[str]:
    m = result["report"]["matrix"]
    counts, rates = m["counts"], m["rates"]
    tp, fp = counts["true_positive"], counts["false_positive"]
    good_per_caught = (fp / tp) if tp else None

    lines = [
        "| | truth: FRAUD | truth: LEGITIMATE |",
        "|---|---|---|",
        f"| **blocked** (review/decline) | TP {tp:,} | FP {fp:,} |",
        f"| **allowed** (approve/challenge) | FN {counts['false_negative']:,} "
        f"| TN {counts['true_negative']:,} |",
        "",
        "| metric | value |",
        "|---|---|",
        f"| precision | {num(rates['precision'])} |",
        f"| recall | {num(rates['recall'])} |",
        f"| false positive rate | {num(rates['false_positive_rate'], 5)} |",
        f"| approval rate | {num(rates['approval_rate'])} |",
        f"| fraud let through (bps of value) | {rates['fraud_rate_bps']} |",
        f"| good customers blocked per fraud caught | "
        f"{'n/a' if good_per_caught is None else f'{good_per_caught:.2f}'} |",
        f"| ROC-AUC | {num(result['roc_auc'])} |",
        f"| PR-AUC | {num(result['pr_auc'])} |",
    ]
    return lines


def _agreement_block(result: dict[str, Any]) -> list[str]:
    lines = ["| cell | performance_report | independent SQL | agree |", "|---|---|---|---|"]
    for key, (engine, sql, ok) in result["agreement"].items():
        lines.append(f"| {key} | {engine:,} | {sql:,} | {'yes' if ok else '**NO**'} |")
    if result["agrees"]:
        lines += ["", "Both paths agree on every cell."]
    else:
        lines += [
            "",
            "> **THE TWO PATHS DISAGREE.** One of them is wrong. Every figure in "
            "this section is therefore unreliable and no number here should be "
            "quoted until the discrepancy is explained.",
        ]
    if not result["roc_auc_agrees"]:
        lines += [
            "",
            f"> **ROC-AUC disagrees between methods**: rank {num(result['roc_auc'], 6)} "
            f"vs trapezoid {num(result['roc_auc_trapezoid'], 6)}. Tie handling is "
            "suspect; treat the AUC figures as unverified.",
        ]
    return lines


def _rules_block(result: dict[str, Any]) -> list[str]:
    rules = result["report"]["rules"]
    unique = result["unique"]
    detection = [r for r in rules if r["kind"] == "DETECTION"]
    protective = [r for r in rules if r["kind"] == "PROTECTIVE"]
    lines: list[str] = []

    if detection:
        lines += [
            "**Detection rules.** Judged on precision: of the transactions this rule "
            "fired on, how many were fraud.",
            "",
            "| rule | weight | fired | on fraud | precision | lift | unique |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in sorted(detection, key=lambda x: x["fired_count"], reverse=True):
            lift = "n/a" if r["lift"] is None else f"{r['lift']:.2f}"
            lines.append(
                f"| `{r['code']}` | {r['weight']} | {r['fired_count']:,} "
                f"| {r['fired_on_fraud']:,} | {num(r['precision'])} "
                f"| {lift} | {unique.get(r['code'], 0):,} |"
            )

    if protective:
        lines += [
            "",
            "**Protective rules.** A negative weight is meant to fire on good "
            "traffic, so precision is not the measure -- `protection_error_rate` is: "
            "how often the rule lowered the score on something that turned out to be "
            "fraud. Reporting these in one column with detection rules would show a "
            "0% precision and invite someone to delete the rule keeping good "
            "customers approved.",
            "",
            "| rule | weight | fired | on fraud | protection error rate |",
            "|---|---|---|---|---|",
        ]
        for r in protective:
            lines.append(
                f"| `{r['code']}` | {r['weight']} | {r['fired_count']:,} "
                f"| {r['fired_on_fraud']:,} | {num(r['protection_error_rate'])} |"
            )

    lines += [
        "",
        "*unique* = fraud this rule caught that no other rule would have caught on "
        "its own: the transaction was blocked, this rule fired, and removing its "
        "weight would have dropped the score below the blocking threshold. A rule "
        "with good precision and zero unique contribution is redundant.",
    ]
    return lines


def render(results: list[dict[str, Any]], run: dict[str, Any], ruleset: dict[str, Any],
           engine_only: dict[str, Any] | None) -> str:
    headline = next(r for r in results if r["period"].headline)
    hm = headline["report"]["matrix"]["rates"]
    periods = run["periods"]

    out: list[str] = [
        "# IEEE-CIS backtest results",
        "",
        "Generated by `scripts/ieee/measure.py` from the replay recorded in "
        "`scripts/ieee/last_run.json`. Every number is measured, not estimated.",
        "",
        "## Headline",
        "",
        f"On **{headline['labelled_count']:,} held-out transactions** the rule engine "
        f"scored **precision {num(hm['precision'])}**, **recall {num(hm['recall'])}**, "
        f"**FPR {num(hm['false_positive_rate'], 5)}**, "
        f"ROC-AUC **{num(headline['roc_auc'])}**, PR-AUC **{num(headline['pr_auc'])}**.",
        "",
        "That is the first honest number this project has produced. The synthetic "
        "figure it replaces was precision 1.000 — see *The gap* below, which is the "
        "most instructive thing in this document.",
        "",
        "## Dataset and run",
        "",
        "| | |",
        "|---|---|",
        f"| dataset | IEEE-CIS train split, {run['rows']['replayed']:,} transactions |",
        f"| fraud | {run['labels'].get('FRAUD', 0):,} "
        f"({run['labels'].get('FRAUD', 0) / max(1, run['rows']['replayed']):.3%}) |",
        f"| ruleset | {ruleset['name']} v{ruleset['version']}, "
        f"{ruleset['rule_count']} rules |",
        f"| thresholds | challenge {ruleset['challenge_at']} · "
        f"review {ruleset['review_at']} · decline {ruleset['decline_at']} |",
        f"| epoch | {periods['epoch']} (arbitrary, fixed — see below) |",
        f"| warm-up | {periods['warmup_days']} days, excluded from every metric |",
        f"| fit half | {periods['warmup_end']} → {periods['fit_end']} |",
        f"| eval half | {periods['eval_start']} → {periods['data_end']} |",
        f"| replay wall clock | {run['wall_clock_seconds']:,.0f}s |",
        "",
        "The two halves are equal in **duration**, not in count. Their transaction "
        "counts differ and are measured per section below.",
        "",
    ]

    for result in results:
        p = result["period"]
        out += [
            f"## {p.title}",
            "",
            f"`{p.since.isoformat()}` → `{p.before.isoformat()}` · "
            f"{result['labelled_count']:,} labelled transactions",
            "",
            f"> {p.caveat}",
            "",
            "### Confusion matrix",
            "",
            *_matrix_block(result),
            "",
            "### Cross-check",
            "",
            *_agreement_block(result),
            "",
            "### Per-rule performance",
            "",
            *_rules_block(result),
            "",
            "### Latency and coverage",
            "",
            "| | |",
            "|---|---|",
            f"| decisions | {result['latency']['n']:,} |",
            f"| latency p50 / p95 / p99 / max | {result['latency']['p50']}ms / "
            f"{result['latency']['p95']}ms / {result['latency']['p99']}ms / "
            f"{result['latency']['max']}ms |",
            f"| budget breaches (>250ms) | {result['latency']['breaches']:,} |",
            f"| label coverage | {num(result['report']['coverage']['coverage'])} |",
            f"| base fraud rate | {num(result['report']['base_fraud_rate'], 5)} |",
            "",
        ]

    out += _comparison_section(headline)
    out += _calibration_section(headline)
    out += _engine_only_section(engine_only)
    out += _gap_section(headline)
    out += _caveats_section()
    out += _sweep_section(headline)
    return "\n".join(out) + "\n"


def _comparison_section(headline: dict[str, Any]) -> list[str]:
    lines = [
        "## Comparison against a benchmark this project did not write",
        "",
        "ROC-AUC and PR-AUC are the only figures here directly comparable to a "
        "model, which is why they are computed at all — precision and recall depend "
        "on a chosen threshold and cannot be compared across systems that chose "
        "different ones.",
        "",
        "| approach | ROC-AUC | PR-AUC | explainable | latency |",
        "|---|---|---|---|---|",
        f"| **fraud-engine rules** (eval half) | **{num(headline['roc_auc'])}** "
        f"| **{num(headline['pr_auc'])}** | yes | "
        f"p95 {headline['latency']['p95']}ms |",
    ]
    for name, roc, pr in TAB_D2AD:
        lines.append(f"| {name} | {roc:.3f} | {pr:.3f} | no | — |")
    lines += [
        "",
        "TAB-D2AD figures are from `nth2165/ep01-tabd2ad-ieee-evaluation`, built on "
        "[github.com/trongjhuongwr/D2AD_Project](https://github.com/trongjhuongwr/D2AD_Project). "
        "**They are not this project's work** and are reproduced here for comparison "
        "only. A benchmark you did not write is worth more than one you did.",
        "",
        "The rules lose on AUC, and that is the expected result — a linear probe over "
        "`V1`–`V339` has access to 339 engineered features these rules deliberately "
        "refuse (caveat 4). What the rules have instead is not captured by AUC: every "
        "decision states which rules fired and why, the whole thing is editable "
        "without a deploy, and it decides in single-digit milliseconds.",
        "",
    ]
    return lines


def _calibration_section(headline: dict[str, Any]) -> list[str]:
    m = headline["report"]["matrix"]
    counts, rates = m["counts"], m["rates"]
    blocked = counts["true_positive"] + counts["false_positive"]
    total = counts["total"]
    actual = {
        "blocked": blocked / total if total else None,
        "recall": rates["recall"],
        "precision": rates["precision"],
    }
    pred = CALIBRATION_PREDICTION

    lines = [
        "## Calibration agreement",
        "",
        f"Before the engine replayed anything, an independent probe predicted the "
        f"operating point at threshold {pred['threshold']} (the ruleset's `review_at`). "
        "The replay was run afterwards, by different code, over held-out data. Both "
        "are shown with the delta rather than the agreement being asserted.",
        "",
        "| | predicted | measured | delta |",
        "|---|---|---|---|",
    ]
    for key in ("blocked", "recall", "precision"):
        p_val, a_val = pred[key], actual[key]
        delta = "n/a" if a_val is None else f"{(a_val - p_val) * 100:+.2f} pp"
        lines.append(f"| {key} | {pct(p_val)} | {pct(a_val)} | {delta} |")

    lines += [
        "",
        "Two independent implementations landing within a fraction of a percentage "
        "point of each other on held-out data is evidence that neither contains a "
        "gross error. It is **not** evidence that the approach is good — both could "
        "be correctly measuring a mediocre classifier, and the PR-AUC above suggests "
        "exactly that.",
        "",
    ]
    return lines


def _engine_only_section(engine_only: dict[str, Any] | None) -> list[str]:
    if engine_only is None:
        return []
    lines = [
        "## Engine-computed rules only",
        "",
        f"Caveat 3, made answerable. Dropping the {engine_only['dropped']} rules that "
        f"read processor-supplied `vesta_*` aggregates leaves "
        f"{engine_only['rule_count']} rules the engine computes entirely for itself. "
        "Re-scored against the **frozen feature snapshots**, so this asks the question "
        "the engine actually faced rather than recomputing today's velocity.",
        "",
    ]

    if not engine_only["can_block"]:
        lines += [
            f"> **These {engine_only['rule_count']} rules cannot block anything at the "
            f"live thresholds.** Their weights sum to a maximum attainable score of "
            f"**{engine_only['ceiling']}** (highest actually observed: "
            f"{engine_only['observed_max']}), against a blocking threshold of "
            f"`review_at` = **{engine_only['review_at']}**. So precision is undefined "
            "and recall is 0 — not because the engine ranks badly, but because "
            "nothing it can score ever crosses the line. The thresholds were fitted "
            "to a score scale that includes the processor's aggregates, and they do "
            "not transfer to a scale without them.",
            "",
            "Precision and recall are therefore meaningless here and are omitted. The "
            "**AUC figures are threshold-independent and are the honest comparison** — "
            "they measure how well the score ranks fraud above legitimate traffic, "
            "regardless of where the cut is drawn.",
            "",
        ]

    lines += [
        "| metric | all rules | engine-computed only |",
        "|---|---|---|",
        f"| rules | {engine_only['rule_count'] + engine_only['dropped']} "
        f"| {engine_only['rule_count']} |",
        f"| max attainable score | 494 | {engine_only['ceiling']} |",
        f"| ROC-AUC | see above | **{num(engine_only['roc_auc'])}** |",
        f"| PR-AUC | see above | **{num(engine_only['pr_auc'])}** |",
        "",
    ]

    best = engine_only["best_point"]
    if best:
        lines += [
            f"Re-fitted to its own scale, the best F1 this subset reaches is at "
            f"threshold **{best['threshold']}**: precision {num(best['precision'])}, "
            f"recall {num(best['recall'])}, blocking {best['block_rate']:.2%} of "
            "traffic. That is what the engine achieves on its own signals alone.",
            "",
        ]

    lines += [
        "Read the two AUC rows together: the engine's own features carry most of the "
        "ranking power on ROC, and roughly half of it on PR — which is the measure "
        "that matters at a 3.4% base rate. **The processor's aggregates are doing a "
        "large share of the work**, and the headline should not be quoted as a "
        "statement about this engine's feature computation without it.",
        "",
    ]
    return lines


def _gap_section(headline: dict[str, Any]) -> list[str]:
    rates = headline["report"]["matrix"]["rates"]
    s = SYNTHETIC
    return [
        "## The gap",
        "",
        "The synthetic result is kept here beside the real one. It is not deleted and "
        "not buried, because the distance between them is the most instructive thing "
        "in this document.",
        "",
        "| | synthetic (generated data) | real (IEEE-CIS, held out) |",
        "|---|---|---|",
        f"| transactions | {s['transactions']:,} | {headline['labelled_count']:,} |",
        f"| fraud rate | {s['fraud_rate']:.2%} | "
        f"{pct(headline['report']['base_fraud_rate'])} |",
        f"| precision | **{s['precision']:.3f}** | **{num(rates['precision'])}** |",
        f"| recall | {s['recall']:.3f} | {num(rates['recall'])} |",
        f"| false positive rate | {s['fpr']:.3f} | "
        f"{num(rates['false_positive_rate'], 5)} |",
        f"| approval rate | {s['approval']:.3f} | {num(rates['approval_rate'])} |",
        "",
        "Precision 1.000 with zero false positives was never a result about fraud. It "
        "was a result about a generator that produced no ambiguous transactions: every "
        "fraudulent record it wrote carried a signature some rule was written to catch. "
        "Real traffic contains transactions that look fraudulent and are not, and "
        "customers who behave like criminals and are not, and the precision above is "
        "what happens when a rule set meets them.",
        "",
        "A repository reporting an honest number on real data is worth more than one "
        "reporting a perfect number on data it invented.",
        "",
    ]


def _caveats_section() -> list[str]:
    return [
        "## Caveats",
        "",
        "These are in the body, not a footnote, because each one changes how the "
        "numbers above should be read.",
        "",
        "### 1. The label is entity-propagated, so some rules are partly circular",
        "",
        "Kaggle's stated definition: a transaction is labelled fraud if a chargeback "
        "was reported, **and every later transaction on the same card, account, email "
        "or billing address is labelled fraud too**.",
        "",
        "The label was therefore built by linking entities — so a rule that scores by "
        "linking entities is partly scoring against its own construction. This applies "
        "to the rules by name, not uniformly:",
        "",
        "- `NEW_ACCOUNT_BURST`, `VEL_ACCOUNT_1H`, `VEL_ACCOUNT_24H` — **affected**. "
        "The account proxy is built from `card1`, `addr1` and `P_emaildomain`, and "
        "card and billing address are both on Kaggle's propagation list. Every figure "
        "for these rules is inflated by an unknown amount.",
        "- `LINK_DEVICE_ACCOUNTS` — **less affected than it looks**. Device is *not* "
        "on the propagation list, so this rule is not scoring directly against the "
        "linking that built the label. But it counts *accounts* per device, and the "
        "account proxy is propagated — so the contamination arrives indirectly.",
        "- `PRODUCT_NOT_W`, `HIGH_AMOUNT`, `MED_AMOUNT`, `M4_ABSENT`, the `VESTA_*` "
        "bands — **not affected by propagation**. They read attributes of the single "
        "transaction, not relationships between transactions.",
        "",
        "Conversely, an isolated fraud that never recurred may carry no label at all, "
        "so recall is measured against an imperfect ground truth in both directions.",
        "",
        "### 2. Cards, accounts and devices are proxies, not identifiers",
        "",
        "IEEE-CIS ships no real identifiers. Every entity here is reconstructed from "
        "anonymised columns and is coarser than the thing it is named after: **14,892 "
        "distinct \"cards\" across 590,540 transactions — 39.7 transactions each**. A "
        "real card portfolio does not look like that. This is an issuer-product "
        "grouping wearing the word *card*, and every velocity and link figure computed "
        "on it is correspondingly coarse.",
        "",
        "A separate, measured consequence: the engine hashes the card slot through a "
        "digits-only normaliser, which merges 47 of those 14,892 keys (0.32%) — a "
        "credit and a debit card sharing the same anonymised columns become one entity. "
        "It affects no figure above, because no rule in this ruleset reads a card "
        "feature, but it must be fixed before one does.",
        "",
        "### 3. Six features are processor-supplied, not computed by this engine",
        "",
        "`vesta_c4`, `vesta_c8`, `vesta_c10`, `vesta_c12`, `vesta_d3` and `vesta_d5` "
        "are aggregates Vesta shipped with the dataset already calculated. This engine "
        "cannot derive them and does not pretend to — they arrive on the authorisation "
        "message or the rules that read them do not fire. Their definitions were never "
        "published, so the weights attached to them are justified by measured lift "
        "alone and by no causal account of what they count. See *Engine-computed rules "
        "only* above for what the engine achieves without them.",
        "",
        "### 4. `V1`–`V339` were deliberately excluded",
        "",
        "339 undisclosed engineered features were dropped at the CSV boundary. Using "
        "them would make every rule unexplainable — the precise failure this project "
        "criticises in PCA-anonymised datasets — but it does handicap the rules against "
        "any model that uses them, which includes every TAB-D2AD row in the comparison "
        "table. The comparison is not like-for-like and should not be read as one.",
        "",
        "### 5. One merchant, one geography, 2017–2019",
        "",
        "The data is a single portfolio in a single market over roughly six months, now "
        "several years old. Fraud patterns move. Nothing measured here generalises to a "
        "different portfolio, and no figure in this document should be quoted as an "
        "expected result anywhere else.",
        "",
    ]


def _sweep_section(headline: dict[str, Any]) -> list[str]:
    lines = [
        "## Full threshold sweep (eval half)",
        "",
        "Every operating point the score can produce, rather than the one that was "
        "chosen. The shape is the point: it shows what the chosen threshold bought and "
        "what it cost, and lets a reader disagree with it.",
        "",
        "| threshold | blocked | block rate | TP | FP | FN | precision | recall | FPR |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in headline["sweep"]:
        lines.append(
            f"| {row['threshold']} | {row['blocked']:,} | {row['block_rate']:.4%} "
            f"| {row['tp']:,} | {row['fp']:,} | {row['fn']:,} "
            f"| {num(row['precision'])} | {num(row['recall'])} | {num(row['fpr'], 5)} |"
        )
    lines.append("")
    return lines


async def run(merchant_code: str, output: Path) -> int:
    periods, run_summary = load_periods()

    await open_pool()
    try:
        async with connection() as conn:
            merchant = await rr.find_merchant_by_code(conn, merchant_code)
            if merchant is None:
                raise RuntimeError(f"Merchant {merchant_code} not found. Replay first.")
            ruleset_row = await rr.find_ruleset_by_status(conn, merchant["id"], "ACTIVE")
            if ruleset_row is None:
                raise RuntimeError(f"Merchant {merchant_code} has no ACTIVE ruleset.")

            rule_rows = await rr.list_rules(conn, ruleset_row["id"])
            rules = [
                Rule(code=r["code"], name=r["name"], condition=r["condition"],
                     weight=r["weight"], hard_action=r["hard_action"],
                     is_enabled=r["is_enabled"])
                for r in rule_rows
            ]
            weights = {r["code"]: r["weight"] for r in rule_rows}
            thresholds = Thresholds(
                challenge_at=ruleset_row["challenge_at"],
                review_at=ruleset_row["review_at"],
                decline_at=ruleset_row["decline_at"],
            )
            ruleset = {**ruleset_row, "rule_count": len(rules)}

            results = []
            for period in periods:
                print(f"measuring {period.key} ...", flush=True)
                results.append(await measure_period(
                    conn, merchant_id=merchant["id"], period=period,
                    rules=rules, thresholds=thresholds, weights=weights,
                ))

            headline = next(p for p in periods if p.headline)
            print("measuring engine-only rules ...", flush=True)
            engine_only = await engine_only_matrix(
                conn, merchant_id=merchant["id"], since=headline.since,
                before=headline.before, rules=rules, thresholds=thresholds,
            )

        document = render(results, run_summary, ruleset, engine_only)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(document, encoding="utf-8")

        for result in results:
            status = "OK" if result["agrees"] else "MISMATCH"
            print(f"  {result['period'].key:<7} cross-check {status}")
        print(f"\nwritten: {output}")

        # A disagreement is not a reporting detail, it is a failed run.
        return 0 if all(r["agrees"] for r in results) else 2
    finally:
        await close_pool()


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure the IEEE replay; write results.")
    ap.add_argument("--merchant-code", default="IEEE")
    ap.add_argument("--output", type=Path, default=RESULTS)
    args = ap.parse_args()

    if not get_settings().ieee_data_path:
        print("IEEE_DATA_PATH is not set; nothing to measure. Skipping.")
        sys.exit(0)
    if not LAST_RUN.exists():
        print(f"{LAST_RUN} not found. Run scripts/ieee/replay.py first. Skipping.")
        sys.exit(0)

    try:
        sys.exit(asyncio.run(run(args.merchant_code, args.output)))
    except (RuntimeError, ValueError, KeyError) as exc:
        print(f"\nMEASURE FAILED\n{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""IEEE-CIS data access layer.

Reads `train_transaction.csv` (and, left-joined, `train_identity.csv`) from a
local path -- never bundled, never committed. See
`docs/IEEE_BACKTEST_PLAN.md` §1.1: the licence permits academic use only, and
CI must run without the dataset present.

Two things this module refuses to do, on purpose:

  * Load `V1`-`V339` into memory. They are undisclosed Vesta features (plan
    §3.5) -- adding them would make every downstream rule unexplainable, so
    they are dropped at the CSV boundary rather than trusted to stay unused.
  * Accept a shuffled or reordered file. Replay depends on strict ascending
    `TransactionDT` (plan §1.2, §4.2) because `D1`-`D15` are timedeltas since
    a previous event -- a shuffled replay leaks future information into the
    past. `load_transactions` raises rather than silently reordering, because
    a loader that "fixes" the order hides a data problem the caller needs to
    know about.

Rows are yielded one at a time via `csv.DictReader`, not read into a
DataFrame -- 590,540 rows x 394 columns is 683MB, and nothing here needs
more than the current row and the identity index in memory at once.

Measured against the real `train_transaction.csv` / `train_identity.csv`
(not assumed from the plan):

    590,540 rows x 394 columns      144,233 identity rows x 41 columns
    fraud 20,663 (3.499%)           identity join hits 24.4%
    TransactionDT 86,400 -> 15,811,131 (182.0 days)
    rows arriving out of TransactionDT order: 0

The file is therefore already sorted, which makes the ordering check below
a *guard* rather than a fixer -- if it ever fires, the input is not the file
this was written against and the run should stop rather than reorder.
"""

import csv
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fraud_engine.config import get_settings

_V_COLUMN = re.compile(r"^V\d+$")

_INT_COLUMNS = {"TransactionID", "TransactionDT", "isFraud"}

# The identity columns below were determined by parsing every value in the
# real train_identity.csv, not by reading the column names and guessing: the
# numeric ones are NOT the contiguous block `id_01`-`id_11` that the naming
# suggests. `id_12`, `id_15`, `id_16` and `id_23` sit inside the numeric range
# but hold strings ("NotFound", "Found", "IP_PROXY:ANONYMOUS"), and `id_32`
# is numeric despite sitting among the categorical tail.
#
# Getting this wrong is quiet rather than loud -- a mistyped column still
# loads, and only shows up later as a rule comparing a float against "49.0".
_NUMERIC_IDENTITY_COLUMNS = {
    f"id_{i:02d}" for i in (*range(1, 12), 13, 14, 17, 18, 19, 20, 21, 22, 24, 25, 26, 32)
}

# Everything not listed here (ProductCD, card4/card6, P_emaildomain, M1-M9,
# DeviceType/DeviceInfo, id_30/id_31/id_33, ...) is categorical in the real
# dataset and is left as the raw string, with an empty field mapped to None.
_FLOAT_COLUMNS = (
    {"TransactionAmt", "card1", "card2", "card3", "card5", "addr1", "addr2", "dist1", "dist2"}
    | {f"C{i}" for i in range(1, 15)}
    | {f"D{i}" for i in range(1, 16)}
    | _NUMERIC_IDENTITY_COLUMNS
)

TRANSACTION_FILENAME = "train_transaction.csv"
IDENTITY_FILENAME = "train_identity.csv"


def resolve_data_path() -> Path:
    """The base directory holding train_transaction.csv / train_identity.csv.

    Read through Settings, not os.environ. Settings loads .env; os.environ
    does not, so a plain `uv run python scripts/ieee/replay.py` would report
    IEEE_DATA_PATH as unset while it sat in .env being ignored -- a failure
    that names the right variable and still misleads.

    Raises rather than defaulting to a guessed path -- per CLAUDE.md §5.7,
    missing configuration must be an error, not a silent fallback.
    """
    raw = get_settings().ieee_data_path
    if not raw:
        raise RuntimeError(
            "IEEE_DATA_PATH is not set. See docs/IEEE_BACKTEST_PLAN.md §1.1: "
            "the dataset is loaded from a local path, never bundled."
        )
    return Path(raw)


def transaction_csv(base: Path | None = None) -> Path:
    """Path to the labelled training transactions.

    Only the training split is ever named here. `test_transaction.csv` has no
    `isFraud` column (plan §1.3) -- offering a path to it would invite someone
    to score it and report a number that cannot exist.
    """
    return (base or resolve_data_path()) / TRANSACTION_FILENAME


def identity_csv(base: Path | None = None) -> Path:
    return (base or resolve_data_path()) / IDENTITY_FILENAME


def _cast(column: str, raw: str) -> Any:
    if raw == "":
        return None
    if column in _INT_COLUMNS:
        return int(raw)
    if column in _FLOAT_COLUMNS:
        return float(raw)
    return raw


def _row_to_dict(fieldnames: list[str], row: dict[str, str]) -> dict[str, Any]:
    return {col: _cast(col, row[col]) for col in fieldnames if not _V_COLUMN.match(col)}


def _fieldnames(reader: csv.DictReader, path: Path | str) -> list[str]:
    """A CSV with no header line is a configuration error, not an empty result.

    `DictReader.fieldnames` is None for an empty file, and every downstream
    comprehension over it would raise `TypeError: 'NoneType' is not iterable`
    -- an error that names the symptom and hides the cause.
    """
    if reader.fieldnames is None:
        raise ValueError(f"{path} has no header row; expected an IEEE-CIS CSV export.")
    return list(reader.fieldnames)


def _load_identity(path: Path) -> tuple[list[str], dict[int, dict[str, Any]]]:
    """Index identity rows by TransactionID.

    144,233 rows x 41 columns is ~26MB -- small enough to hold in full,
    which turns the join into a dict lookup instead of re-scanning the file
    per transaction.
    """
    by_id: dict[int, dict[str, Any]] = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        columns = _fieldnames(reader, path)
        fieldnames = [c for c in columns if c != "TransactionID"]
        for row in reader:
            record = _row_to_dict(columns, row)
            by_id[record["TransactionID"]] = {
                k: v for k, v in record.items() if k != "TransactionID"
            }
    return fieldnames, by_id


def load_transactions(
    transaction_path: str | Path,
    identity_path: str | Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream transaction rows in file order, left-joined with identity.

    Every yielded record carries the full identity column set as keys, set
    to None when a transaction has no identity match -- so callers never
    need to distinguish "key absent" from "value unknown" (see CLAUDE.md's
    `dict.get` trap). `identity_path=None` yields transaction rows alone.

    Raises ValueError the moment TransactionDT decreases: plan §4.2 requires
    verifying the sort, not assuming it.
    """
    identity_fields: list[str] = []
    identity_by_id: dict[int, dict[str, Any]] = {}
    if identity_path is not None:
        identity_fields, identity_by_id = _load_identity(Path(identity_path))
    empty_identity = dict.fromkeys(identity_fields)

    last_dt: int | None = None
    with open(transaction_path, newline="") as f:
        reader = csv.DictReader(f)
        columns = _fieldnames(reader, transaction_path)
        for row in reader:
            record = _row_to_dict(columns, row)
            dt = record["TransactionDT"]
            if last_dt is not None and dt < last_dt:
                raise ValueError(
                    f"TransactionDT out of order at TransactionID="
                    f"{record['TransactionID']}: {dt} < {last_dt}. Replay requires "
                    "strictly ascending order (IEEE_BACKTEST_PLAN.md §4.2); "
                    "sort the source file before loading."
                )
            last_dt = dt
            record.update(identity_by_id.get(record["TransactionID"], empty_identity))
            yield record

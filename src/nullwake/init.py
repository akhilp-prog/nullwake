"""`nullwake init`: draft starter contracts from what the warehouse already knows.

Reads table definitions (columns, types, NOT NULL, declared primary keys) and writes one DataContract YAML per table.
Nothing is guessed silently: every inferred setting carries a comment saying where it came from, and limits that only
the team can know (how late is too late) are marked TODO with a conservative default, so the first run does not page
anyone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .connectors import ColumnInfo, Connector

TIMESTAMP_TYPES = {
    "timestamp with time zone", "timestamp without time zone", "timestamp", "timestamptz",
    "timestamp_ntz", "timestamp_ltz", "timestamp_tz", "datetime", "date",
}
# Columns that record when a row arrived. They move forward with every load, so they suit freshness and volume.
ARRIVAL = re.compile(r"(^|_)(loaded|load|ingested|ingest|inserted|insert|synced|extracted|batched|received|arrived)"
                     r"(_at|_time|_ts|_on)?$|^_(fivetran_synced|airbyte_extracted_at|sdc_batched_at|dlt_load_id)$")
CREATED = re.compile(r"(^|_)(created|creation|event|occurred|recorded)(_at|_time|_ts|_on|_timestamp)?$")
UPDATED = re.compile(r"(^|_)(updated|modified|changed)(_at|_time|_ts|_on)?$")


@dataclass
class Hints:
    """What another tool (dbt) already knows about a table, used to sharpen a draft."""
    source: str = "dbt"                                     # named in comments so reviewers know where it came from
    owner: str | None = None
    not_null: set[str] = field(default_factory=set)        # lower-case column names
    unique: set[str] = field(default_factory=set)
    unique_key: list[str] = field(default_factory=list)
    freshness_column: str | None = None
    max_age_minutes: int | None = None
    description: str | None = None


@dataclass
class Draft:
    name: str
    dataset: str
    yaml: str
    notes: list[str] = field(default_factory=list)


def contract_name(table: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "_", table.lower()).strip("_-")[:60] or "table"
    if not base[0].isalnum():
        base = "t" + base
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name


def pick_timestamp(columns: list[ColumnInfo]) -> tuple[ColumnInfo | None, str]:
    """The column that best shows when data last arrived, and why it was chosen."""
    stamps = [c for c in columns if c.type in TIMESTAMP_TYPES]
    if not stamps:
        return None, "no timestamp or date column"
    ranked = sorted(stamps, key=lambda c: (
        0 if ARRIVAL.search(c.name.lower()) else 1 if CREATED.search(c.name.lower()) else
        2 if UPDATED.search(c.name.lower()) else 3, c.type == "date"))
    best = ranked[0]
    lname = best.name.lower()
    kind = ("load time" if ARRIVAL.search(lname) else "creation time" if CREATED.search(lname) else
            "update time" if UPDATED.search(lname) else "only timestamp column" if len(stamps) == 1 else
            "first timestamp column")
    return best, kind


def _q(value: str) -> str:
    """YAML-safe scalar for identifiers and types."""
    return value if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ .$@+-]*", value) and value.strip() == value \
        and value.lower() not in {"yes", "no", "true", "false", "on", "off", "null", "y", "n"} else repr(value)


def draft_contract(connection: str, dataset: str, columns: list[ColumnInfo], primary_key: list[str], owner: str,
                   taken: set[str], interval_minutes: int = 60, hints: Hints | None = None,
                   name_hint: str | None = None) -> Draft:
    h = hints or Hints(source="")
    table = dataset.split(".")[-1]
    name = contract_name(name_hint or table, taken)
    notes: list[str] = []
    if h.owner:
        owner = h.owner
    origin = f"the table definition and {h.source}" if h.source else "the table definition"
    out = [
        f"# Drafted by `nullwake init` from {origin}. Review it, set the TODOs, then apply it.",
        "apiVersion: nullwake/v1",
        "kind: DataContract",
        "metadata:",
        f"  name: {name}",
        f"  owner: {_q(owner)}" + (f"  # from {h.source} meta" if h.owner else
                                   "  # TODO: the team or person who fixes this table" if "example" in owner else ""),
        "spec:",
        f"  connection: {_q(connection)}",
        f"  dataset: {_q(dataset)}",
        f"  schedule: {{intervalMinutes: {interval_minutes}}}",
        "  schema:",
        "    allowAdditionalColumns: true  # new columns are fine; renamed, dropped or retyped ones fail",
        "    columns:",
    ]
    known = {c.name.lower() for c in columns}
    for c in columns:
        lname = c.name.lower()
        nn = ", nullable: false" if not c.nullable or lname in h.not_null else ""
        uq = ", unique: true" if lname in h.unique else ""
        why = [w for w, hit in (("not_null", c.nullable and lname in h.not_null), ("unique", bool(uq))) if hit]
        tail = f"  # {h.source} test: {', '.join(why)}" if why else ""
        out.append(f"      - {{name: {_q(c.name)}, type: {_q(c.type)}{nn}{uq}}}{tail}")
    for missing in sorted((h.not_null | h.unique) - known):
        notes.append(f"{h.source} tests column {missing}, which the warehouse table does not have")
    key = [k for k in h.unique_key if k.lower() in known]
    if primary_key:
        out.append(f"  uniqueKey: [{', '.join(_q(k) for k in primary_key)}]  # declared primary key")
    elif key:
        out.append(f"  uniqueKey: [{', '.join(_q(k) for k in key)}]  # from {h.source}")
    elif h.unique:
        pass  # a single-column unique test is already on the column
    else:
        notes.append("no declared primary key: add uniqueKey if rows should be unique")
        out.append("  # uniqueKey: [id]  # TODO: no primary key is declared; list the columns that identify a row")
    ts, why = pick_timestamp(columns)
    if h.freshness_column and h.freshness_column.lower() in known:
        ts = next(c for c in columns if c.name.lower() == h.freshness_column.lower())
        why = f"loaded_at_field in {h.source}"
    age = h.max_age_minutes or 1440
    todo = "" if h.max_age_minutes else "  # TODO: how late is too late?"
    if ts is None:
        notes.append(f"no freshness check: {why}")
        out.append("  # freshness: no timestamp or date column found, so there is no freshness check")
    else:
        if h.max_age_minutes:
            note = f"  # {h.source} freshness error_after; column: {why}" if "loaded_at" in why else \
                f"  # {h.source} freshness; {ts.name} chosen as the {why}"
        else:
            note = f"{todo} ({ts.name} chosen as the {why})"
        out.append(f"  freshness: {{column: {_q(ts.name)}, maxAgeMinutes: {age}}}{note}")
        if UPDATED.search(ts.name.lower()) and not ARRIVAL.search(ts.name.lower()) and "loaded_at" not in why:
            notes.append(f"freshness uses {ts.name} (an update time); no volume check, because update times move "
                         "with edits as well as new rows")
        else:
            out.append(f"  volume: {{column: {_q(ts.name)}, windowHours: 24}}"
                       "  # rows per day compared with the previous 7 days")
    return Draft(name=name, dataset=dataset, yaml="\n".join(out) + "\n", notes=notes)


def run_init(conn: Connector, connection: str, schemas: list[str], tables: list[str], out_dir: Path, owner: str,
             force: bool, interval_minutes: int = 60, dbt_tables: list | None = None,
             ) -> tuple[list[tuple[Path, Draft]], list[str]]:
    """Draft and write contracts. Returns (written files, skipped messages).

    `dbt_tables` are `nullwake.dbt.DbtTable`s: their hints sharpen the drafts and their names name the contracts."""
    jobs: list[tuple[str, Hints | None, str | None]] = [(t, None, None) for t in tables]
    for schema in schemas:
        jobs += [(f"{schema}.{t}", None, None) for t in conn.tables(schema)]
    jobs += [(t.dataset, t.hints, t.name) for t in dbt_tables or []]
    taken: set[str] = set()  # names used in this run: two schemas with an `orders` table give orders, orders_2
    written, skipped = [], []
    out_dir.mkdir(parents=True, exist_ok=True)
    for dataset, hints, name_hint in jobs:
        columns = conn.columns(dataset)
        if not columns:
            skipped.append(f"{dataset}: table not found or no columns visible to this role"
                           + (" (has the dbt model been built?)" if hints else ""))
            continue
        draft = draft_contract(connection, dataset, columns, conn.primary_key(dataset), owner, taken,
                               interval_minutes, hints, name_hint)
        path = out_dir / f"{draft.name}.yaml"
        if path.exists() and not force:
            skipped.append(f"{dataset}: {path} already exists (use --force to overwrite)")
            continue
        path.write_text(draft.yaml)
        written.append((path, draft))
    return written, skipped

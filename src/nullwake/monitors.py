"""Check evaluation. Each check returns a Result; no warehouse rows are retained, only aggregates."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .connectors import Connector, ConnectorError

KINDS = ("freshness", "volume", "schema", "not_null", "unique", "assertion")


@dataclass
class Result:
    status: str  # pass | fail | error
    message: str
    value: float | None = None
    observed: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] | None = None  # replacement monitor state, if changed


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def human_minutes(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} days"


def check_freshness(conn: Connector, dataset: str, params: dict, state: dict) -> Result:
    age, count = conn.row(conn.dialect.freshness_sql(dataset, params["column"]))
    limit = float(params["maxAgeMinutes"])
    if not count or age is None:
        return Result("fail", f"{dataset} has no rows with {params['column']} set", None, {"rows": count or 0})
    minutes = max(_num(age), 0.0) / 60
    observed = {"ageMinutes": round(minutes, 2), "maxAgeMinutes": limit}
    if minutes > limit:
        return Result("fail", f"Data is {human_minutes(minutes)} old; contract allows {human_minutes(limit)}",
                      round(minutes, 2), observed)
    return Result("pass", f"Last update {human_minutes(minutes)} ago (limit {human_minutes(limit)})",
                  round(minutes, 2), observed)


def volume_bounds(history: list[float], tolerance_pct: float) -> tuple[float, float, float]:
    median = statistics.median(history)
    mad = statistics.median([abs(x - median) for x in history])
    spread = max(3 * 1.4826 * mad, median * tolerance_pct / 100)
    return median, max(median - spread, 0.0), median + spread


def check_volume(conn: Connector, dataset: str, params: dict, state: dict) -> Result:
    windows = int(params.get("lookbackWindows", 7))
    hours = int(params.get("windowHours", 24))
    row = conn.row(conn.dialect.volume_sql(dataset, params["column"], hours, windows))
    counts = [int(v or 0) for v in row]
    current, history = counts[0], counts[1:]
    median, low, high = volume_bounds([float(h) for h in history], float(params.get("tolerancePct", 50)))
    observed = {"current": current, "history": history, "median": median, "low": round(low, 1),
                "high": round(high, 1), "windowHours": hours}
    min_rows = int(params.get("minRows", 1))
    label = f"{current:,} rows in the last {hours}h"
    if current < min_rows:
        return Result("fail", f"{label}; at least {min_rows:,} expected", current, observed)
    if sum(1 for h in history if h > 0) < 3:
        return Result("pass", f"{label}; not enough history yet for a baseline", current, observed)
    if current < low or current > high:
        direction = "below" if current < low else "above"
        return Result("fail", f"{label}, {direction} the expected {low:,.0f}–{high:,.0f} (median {median:,.0f})",
                      current, observed)
    return Result("pass", f"{label}, within {low:,.0f}–{high:,.0f}", current, observed)


def _diff(expected: list[dict], actual: list[dict], allow_extra: bool) -> tuple[list[str], list[str]]:
    actual_by = {c["name"].lower(): c for c in actual}
    problems, notes = [], []
    for col in expected:
        got = actual_by.get(col["name"])
        if got is None:
            problems.append(f"missing column {col['name']}")
        elif col.get("type") and got["type"] != col["type"]:
            problems.append(f"{col['name']} is {got['type']}, expected {col['type']}")
    extra = sorted(set(actual_by) - {c["name"] for c in expected})
    if extra:
        (notes if allow_extra else problems).append("new column(s): " + ", ".join(extra))
    return problems, notes


def check_schema(conn: Connector, dataset: str, params: dict, state: dict) -> Result:
    actual = [{"name": c.name.lower(), "type": c.type, "nullable": c.nullable} for c in conn.columns(dataset)]
    if not actual:
        return Result("error", f"{dataset} was not found or the connection role cannot see it", None, {})
    observed = {"columns": actual}
    expected = params.get("expected")
    if expected is not None:
        problems, notes = _diff(expected, actual, bool(params.get("allowAdditionalColumns", True)))
    else:
        baseline = state.get("baseline")
        if not baseline:
            return Result("pass", f"Baseline captured: {len(actual)} columns", len(actual), observed,
                          {**state, "baseline": actual})
        problems, notes = _diff(baseline, actual, allow_extra=False)
    if problems:
        return Result("fail", "Schema drift: " + "; ".join(problems), len(actual), observed)
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return Result("pass", f"Schema matches ({len(actual)} columns){suffix}", len(actual), observed)


def check_not_null(conn: Connector, dataset: str, params: dict, state: dict) -> Result:
    total, nulls = conn.row(conn.dialect.null_sql(dataset, params["column"]))
    total, nulls = int(total or 0), int(nulls or 0)
    pct = (nulls / total * 100) if total else 0.0
    observed = {"rows": total, "nulls": nulls, "nullPct": round(pct, 4)}
    if pct > float(params.get("maxNullPct", 0)):
        return Result("fail", f"{nulls:,} of {total:,} rows have NULL {params['column']} ({pct:.2f}%)", pct, observed)
    return Result("pass", f"No unexpected NULLs in {params['column']} ({total:,} rows)", pct, observed)


def check_unique(conn: Connector, dataset: str, params: dict, state: dict) -> Result:
    (dupes,) = conn.row(conn.dialect.duplicate_sql(dataset, params["columns"]))
    dupes = int(dupes or 0)
    key = ", ".join(params["columns"])
    if dupes:
        return Result("fail", f"{dupes:,} duplicate row(s) on ({key})", dupes, {"duplicates": dupes})
    return Result("pass", f"({key}) is unique", 0, {"duplicates": 0})


def check_assertion(conn: Connector, dataset: str, params: dict, state: dict) -> Result:
    row = conn.row(params["sql"])
    if len(row) != 1:
        raise ConnectorError("assertion SQL must return exactly one column")
    try:
        value = float(row[0] or 0)
    except (TypeError, ValueError):
        raise ConnectorError("assertion SQL must return a number") from None
    limit = float(params.get("maxValue", 0))
    observed = {"value": value, "maxValue": limit}
    if value > limit:
        return Result("fail", f"Assertion returned {value:,.0f}; allowed ≤ {limit:,.0f}", value, observed)
    return Result("pass", f"Assertion returned {value:,.0f} (≤ {limit:,.0f})", value, observed)


CHECKS = {"freshness": check_freshness, "volume": check_volume, "schema": check_schema,
          "not_null": check_not_null, "unique": check_unique, "assertion": check_assertion}


def evaluate(conn: Connector, kind: str, dataset: str, params: dict, state: dict | None = None) -> Result:
    try:
        return CHECKS[kind](conn, dataset, params, dict(state or {}))
    except ConnectorError as exc:
        return Result("error", str(exc))

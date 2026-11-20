"""Read a dbt manifest (target/manifest.json) into the tables and hints `nullwake init --dbt` drafts from.

Only what dbt states explicitly is used: `unique` / `not_null` tests, `dbt_utils.unique_combination_of_columns`,
an incremental model's `unique_key`, source `loaded_at_field` + `freshness.error_after` (or `warn_after`), and an
owner from `meta.owner` or the model's group. Columns and types still come from the warehouse, because the manifest
only lists the columns someone documented.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .init import Hints

SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PERIOD_MINUTES = {"minute": 1, "hour": 60, "day": 1440}
TABLE_TYPES = {"model", "snapshot", "seed"}


class ManifestError(ValueError):
    pass


@dataclass
class DbtTable:
    unique_id: str
    name: str              # model or source table name: used for the contract name
    dataset: str           # schema.table, or database.schema.table when the warehouse takes three parts
    tags: set[str]
    hints: Hints


def _max_age(freshness: dict | None) -> int | None:
    for level in ("error_after", "warn_after"):
        rule = (freshness or {}).get(level) or {}
        count, period = rule.get("count"), rule.get("period")
        if isinstance(count, int) and count > 0 and period in PERIOD_MINUTES:
            return count * PERIOD_MINUTES[period]
    return None


def _owner(node: dict, groups: dict) -> str | None:
    meta = {**(node.get("meta") or {}), **((node.get("config") or {}).get("meta") or {})}
    owner = meta.get("owner")
    if isinstance(owner, str) and owner.strip():
        return owner.strip()
    group = groups.get(f"group.{node.get('package_name')}.{node.get('group')}") if node.get("group") else None
    email = ((group or {}).get("owner") or {}).get("email")
    return email if isinstance(email, str) and email else None


def load_manifest(path: Path, three_part: bool) -> list[DbtTable]:
    """Tables (models, snapshots, seeds, sources) with their hints. `three_part` adds the database to the dataset."""
    try:
        manifest = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read dbt manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or "nodes" not in manifest:
        raise ManifestError(f"{path} is not a dbt manifest (no 'nodes'); run `dbt parse` and use target/manifest.json")
    nodes, sources, groups = manifest.get("nodes") or {}, manifest.get("sources") or {}, manifest.get("groups") or {}

    tables: dict[str, DbtTable] = {}

    def add(uid: str, node: dict, identifier: str, hints: Hints) -> None:
        schema, database = node.get("schema"), node.get("database")
        if not schema or not identifier:
            return
        dataset = f"{database}.{schema}.{identifier}" if three_part and database else f"{schema}.{identifier}"
        tags = set(node.get("tags") or []) | set((node.get("config") or {}).get("tags") or [])
        tables[uid] = DbtTable(uid, node.get("name") or identifier, dataset, tags, hints)

    for uid, node in nodes.items():
        config = node.get("config") or {}
        if node.get("resource_type") not in TABLE_TYPES or config.get("enabled") is False \
                or config.get("materialized") == "ephemeral":
            continue
        key = config.get("unique_key")
        key = [key] if isinstance(key, str) else [k for k in key if isinstance(k, str)] if isinstance(key, list) else []
        add(uid, node, node.get("alias") or node.get("name"),
            Hints(owner=_owner(node, groups), unique_key=[k.lower() for k in key if "," not in k and " " not in k],
                  static=node.get("resource_type") == "seed"))
    for uid, src in sources.items():
        if (src.get("config") or {}).get("enabled") is False:
            continue
        add(uid, src, src.get("identifier") or src.get("name"),
            Hints(owner=_owner(src, groups), freshness_column=src.get("loaded_at_field"),
                  max_age_minutes=_max_age(src.get("freshness"))))

    for node in nodes.values():
        if node.get("resource_type") != "test" or (node.get("config") or {}).get("enabled") is False:
            continue
        target = node.get("attached_node") or next(
            (d for d in reversed((node.get("depends_on") or {}).get("nodes") or []) if d in tables), None)
        if target not in tables:
            continue
        meta = node.get("test_metadata") or {}
        kwargs = meta.get("kwargs") or {}
        column = node.get("column_name") or kwargs.get("column_name")
        hints = tables[target].hints
        test = meta.get("name")
        if (node.get("config") or {}).get("where"):
            continue  # a filtered test is a weaker promise than the whole-table check nullwake would run
        if test == "not_null" and isinstance(column, str):
            hints.not_null.add(column.strip('"`').lower())
        elif test == "unique" and isinstance(column, str):
            hints.unique.add(column.strip('"`').lower())
        elif test == "unique_combination_of_columns" and isinstance(kwargs.get("combination_of_columns"), list):
            hints.unique_key = [str(c).lower() for c in kwargs["combination_of_columns"]]
        elif test == "accepted_values" and isinstance(column, str) and SIMPLE.match(column):
            values = _literals(kwargs.get("values"), kwargs.get("quote", True))
            if values:
                hints.checks.append((f"accepted_values_{column.lower()}"[:60],
                                     f"select count(*) from {tables[target].dataset} where {column} is not null "
                                     f"and {column} not in ({values})",
                                     f"dbt accepted_values: {column} is one of the allowed values", {column}))
        elif test == "relationships" and isinstance(column, str) and SIMPLE.match(column):
            field_ = kwargs.get("field")
            parent = next((d for d in (node.get("depends_on") or {}).get("nodes") or []
                           if d != target and d in tables), None)
            if parent and isinstance(field_, str) and SIMPLE.match(field_):
                hints.checks.append((f"relationships_{column.lower()}"[:60],
                                     f"select count(*) from {tables[target].dataset} c "
                                     f"left join {tables[parent].dataset} p on c.{column} = p.{field_} "
                                     f"where c.{column} is not null and p.{field_} is null",
                                     f"dbt relationships: every {column} exists in {tables[parent].name}.{field_}",
                                     {column}))
    return list(tables.values())


def _literals(values, quote) -> str | None:
    """SQL list of accepted values, or None if any value is not a plain string or number."""
    if not isinstance(values, list) or not values or len(values) > 200:
        return None
    out = []
    for v in values:
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            out.append(repr(v))
        elif isinstance(v, str) and quote is not False:
            out.append("'" + v.replace("'", "''") + "'")
        elif isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v):
            out.append(v)
        else:
            return None
    return ", ".join(out)


def select(tables: list[DbtTable], selectors: list[str]) -> list[DbtTable]:
    """Keep tables matching any selector: a model or source table name, `tag:x`, or `source:name`."""
    if not selectors:
        return tables
    out = []
    for t in tables:
        for s in selectors:
            if (s.startswith("tag:") and s[4:] in t.tags) or s == t.name or \
                    (s.startswith("source:") and t.unique_id.startswith("source.")
                     and t.unique_id.split(".")[2] == s[7:]):
                out.append(t)
                break
    return out

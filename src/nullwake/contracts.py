"""Config-as-code: parse and validate Nullwake YAML (Connection, DataContract, AlertChannel).

Example DataContract::

    apiVersion: nullwake/v1
    kind: DataContract
    metadata: {name: orders, owner: data-platform@example.com}
    spec:
      connection: warehouse
      dataset: analytics.fct_orders
      schedule: {intervalMinutes: 15}
      schema:
        allowAdditionalColumns: true
        columns:
          - {name: order_id, type: bigint, nullable: false, unique: true}
      freshness: {column: loaded_at, maxAgeMinutes: 90}
      volume: {column: loaded_at, windowHours: 24, tolerancePct: 50}
      checks:
        - {name: no_negative_amounts, sql: "select count(*) from analytics.fct_orders where amount < 0"}
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .connectors import CONNECTION_TYPES, DIALECTS, ConnectorError, guard_select, ident, split_dataset

API_VERSION = "nullwake/v1"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SEVERITIES = ("warning", "critical")
CHANNEL_TYPES = ("slack", "webhook")


class ContractError(ValueError):
    pass


@dataclass
class MonitorDef:
    name: str
    kind: str
    connection: str
    dataset: str
    params: dict
    severity: str
    interval_seconds: int


@dataclass
class ConnectionDef:
    name: str
    type: str
    spec: dict


@dataclass
class ContractDef:
    name: str
    owner: str
    description: str
    connection: str
    dataset: str
    spec: dict
    source_yaml: str
    monitors: list[MonitorDef] = field(default_factory=list)

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.source_yaml.encode()).hexdigest()


@dataclass
class ChannelDef:
    name: str
    type: str
    spec: dict
    min_severity: str


@dataclass
class Bundle:
    connections: list[ConnectionDef] = field(default_factory=list)
    contracts: list[ContractDef] = field(default_factory=list)
    channels: list[ChannelDef] = field(default_factory=list)


def _req(obj: dict, key: str, where: str, kind=str):
    if not isinstance(obj, dict) or key not in obj:
        raise ContractError(f"{where}: missing required field '{key}'")
    value = obj[key]
    if kind is not None and not isinstance(value, kind):
        raise ContractError(f"{where}.{key}: expected {kind.__name__ if isinstance(kind, type) else kind}")
    return value


def _num(obj: dict, key: str, where: str, default, low=0, high=None, integer=True):
    value = obj.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not float(value).is_integer()):
        raise ContractError(f"{where}.{key}: expected a {'whole ' if integer else ''}number")
    if value < low or (high is not None and value > high):
        raise ContractError(f"{where}.{key}: must be between {low} and {high if high is not None else 'any'}")
    return int(value) if integer else float(value)


def _severity(obj: dict, where: str, default: str) -> str:
    value = obj.get("severity", default)
    if value not in SEVERITIES:
        raise ContractError(f"{where}.severity: use one of {', '.join(SEVERITIES)}")
    return value


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise ContractError(f"{where}: names use lowercase letters, digits, '-' or '_' (max 63)")
    return value


def _ident(value: Any, where: str) -> str:
    try:
        return ident(value)
    except ConnectorError as exc:
        raise ContractError(f"{where}: {exc}") from None


def _env_name(spec: dict, key: str, where: str, required: bool = True) -> None:
    value = spec.get(key)
    if value is None and not required:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", value):
        raise ContractError(f"{where}.{key}: must name an environment variable (UPPER_CASE)")


def parse_connection(doc: dict, where: str) -> ConnectionDef:
    name = _name(_req(_req(doc, "metadata", where, dict), "name", f"{where}.metadata"), f"{where}.metadata.name")
    spec = _req(doc, "spec", where, dict)
    type_ = _req(spec, "type", f"{where}.spec")
    if type_ not in CONNECTION_TYPES:
        raise ContractError(f"{where}.spec.type: use one of {', '.join(CONNECTION_TYPES)}")
    for key, value in spec.items():
        if key.lower().endswith(("password", "secret", "token", "dsn", "key")) and not key.endswith("Env"):
            raise ContractError(f"{where}.spec.{key}: secrets must be referenced as <name>Env, never inline")
        if key.endswith("Env"):
            _env_name(spec, key, f"{where}.spec")
    if type_ == "postgres":
        _env_name(spec, "dsnEnv", f"{where}.spec")
    if type_ == "snowflake":
        for key in ("account", "user"):
            _req(spec, key, f"{where}.spec")
        if not spec.get("privateKeyFileEnv") and not spec.get("passwordEnv"):
            raise ContractError(f"{where}.spec: set privateKeyFileEnv (recommended) or passwordEnv")
    if type_ == "bigquery":
        _req(spec, "project", f"{where}.spec")
    return ConnectionDef(name, type_, {k: v for k, v in spec.items() if k != "type"})


def parse_channel(doc: dict, where: str) -> ChannelDef:
    name = _name(_req(_req(doc, "metadata", where, dict), "name", f"{where}.metadata"), f"{where}.metadata.name")
    spec = _req(doc, "spec", where, dict)
    type_ = _req(spec, "type", f"{where}.spec")
    if type_ not in CHANNEL_TYPES:
        raise ContractError(f"{where}.spec.type: use one of {', '.join(CHANNEL_TYPES)}")
    _env_name(spec, "urlEnv", f"{where}.spec")
    _env_name(spec, "signingSecretEnv", f"{where}.spec", required=False)
    min_sev = spec.get("minSeverity", "warning")
    if min_sev not in SEVERITIES:
        raise ContractError(f"{where}.spec.minSeverity: use one of {', '.join(SEVERITIES)}")
    return ChannelDef(name, type_, {k: v for k, v in spec.items() if k in ("urlEnv", "signingSecretEnv")}, min_sev)


def parse_contract(doc: dict, where: str, source_yaml: str, connection_types: dict[str, str]) -> ContractDef:
    meta = _req(doc, "metadata", where, dict)
    name = _name(_req(meta, "name", f"{where}.metadata"), f"{where}.metadata.name")
    where = f"contract {name}"
    spec = _req(doc, "spec", where, dict)
    connection = _name(_req(spec, "connection", f"{where}.spec"), f"{where}.spec.connection")
    if connection not in connection_types:
        raise ContractError(f"{where}.spec.connection: unknown connection '{connection}'")
    dialect = DIALECTS[connection_types[connection]]
    dataset = _req(spec, "dataset", f"{where}.spec")
    try:
        dialect.table(dataset)  # dialect-aware: BigQuery project IDs contain hyphens
    except ConnectorError as exc:
        raise ContractError(f"{where}.spec.dataset: {exc}") from None
    schedule = spec.get("schedule", {}) or {}
    interval = _num(schedule, "intervalMinutes", f"{where}.spec.schedule", 15, 1, 1440) * 60
    monitors: list[MonitorDef] = []

    def add(suffix: str, kind: str, params: dict, severity: str):
        monitors.append(MonitorDef(f"{name}.{suffix}", kind, connection, dataset, params, severity, interval))

    schema = spec.get("schema")
    if schema is not None:
        cols = _req(schema, "columns", f"{where}.spec.schema", list)
        expected, seen = [], set()
        for i, col in enumerate(cols):
            cw = f"{where}.spec.schema.columns[{i}]"
            cname = _ident(_req(col, "name", cw), f"{cw}.name").lower()
            if cname in seen:
                raise ContractError(f"{cw}.name: duplicate column '{cname}'")
            seen.add(cname)
            ctype = col.get("type")
            if ctype is not None and not isinstance(ctype, str):
                raise ContractError(f"{cw}.type: expected a string")
            nullable = col.get("nullable", True)
            if not isinstance(nullable, bool):
                raise ContractError(f"{cw}.nullable: expected true or false")
            expected.append({"name": cname, "type": dialect.normalize_type(ctype) if ctype else None,
                             "nullable": nullable})
            if not nullable:
                add(f"not_null.{cname}", "not_null", {"column": cname, "maxNullPct": 0.0}, "critical")
            if col.get("unique") is True:
                add(f"unique.{cname}", "unique", {"columns": [cname]}, "critical")
        allow = schema.get("allowAdditionalColumns", True)
        if not isinstance(allow, bool):
            raise ContractError(f"{where}.spec.schema.allowAdditionalColumns: expected true or false")
        add("schema", "schema", {"expected": expected, "allowAdditionalColumns": allow}, _severity(schema, f"{where}.spec.schema", "critical"))

    key = spec.get("uniqueKey")
    if key is not None:
        if not isinstance(key, list) or not key:
            raise ContractError(f"{where}.spec.uniqueKey: expected a list of columns")
        cols = [_ident(c, f"{where}.spec.uniqueKey").lower() for c in key]
        add("unique_key", "unique", {"columns": cols}, "critical")

    fresh = spec.get("freshness")
    if fresh is not None:
        fw = f"{where}.spec.freshness"
        _req(fresh, "maxAgeMinutes", fw, None)
        add("freshness", "freshness", {
            "column": _ident(_req(fresh, "column", fw), f"{fw}.column").lower(),
            "maxAgeMinutes": _num(fresh, "maxAgeMinutes", fw, 0, 1, 525600),
            "maxFutureSkewMinutes": _num(fresh, "maxFutureSkewMinutes", fw, 5, 0, 1440),
        }, _severity(fresh, fw, "critical"))

    vol = spec.get("volume")
    if vol is not None:
        vw = f"{where}.spec.volume"
        add("volume", "volume", {
            "column": _ident(_req(vol, "column", vw), f"{vw}.column").lower(),
            "windowHours": _num(vol, "windowHours", vw, 24, 1, 168),
            "lookbackWindows": _num(vol, "lookbackWindows", vw, 7, 3, 28),
            "tolerancePct": _num(vol, "tolerancePct", vw, 50, 1, 1000, integer=False),
            "minRows": _num(vol, "minRows", vw, 1, 0, 10**12),
        }, _severity(vol, vw, "warning"))

    checks = spec.get("checks", []) or []
    if not isinstance(checks, list):
        raise ContractError(f"{where}.spec.checks: expected a list")
    for i, chk in enumerate(checks):
        cw = f"{where}.spec.checks[{i}]"
        cname = _name(_req(chk, "name", cw), f"{cw}.name")
        try:
            sql = guard_select(_req(chk, "sql", cw))
        except ConnectorError as exc:
            raise ContractError(f"{cw}.sql: {exc}") from None
        add(f"check.{cname}", "assertion",
            {"sql": sql, "maxValue": _num(chk, "maxValue", cw, 0, 0, None, integer=False),
             "description": str(chk.get("description", ""))[:500]},
            _severity(chk, cw, "warning"))

    if not monitors:
        raise ContractError(f"{where}: define at least one of schema, freshness, volume, uniqueKey or checks")
    names = [m.name for m in monitors]
    if len(set(names)) != len(names):
        raise ContractError(f"{where}: duplicate check names")
    return ContractDef(name=name, owner=str(meta.get("owner", ""))[:254],
                       description=str(meta.get("description", ""))[:2000], connection=connection,
                       dataset=dataset, spec=spec, source_yaml=source_yaml, monitors=monitors)


def load_documents(paths: list[str | Path]) -> list[tuple[str, dict, str]]:
    docs: list[tuple[str, dict, str]] = []
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files.extend(sorted([*p.rglob("*.yaml"), *p.rglob("*.yml")]))
        elif p.is_file():
            files.append(p)
        else:
            raise ContractError(f"{p}: no such file or directory")
    for f in files:
        text = f.read_text(encoding="utf-8")
        chunks = [c for c in re.split(r"(?m)^---\s*$", text) if c.strip()]
        for i, chunk in enumerate(chunks):
            try:
                doc = yaml.safe_load(chunk)
            except yaml.YAMLError as exc:
                raise ContractError(f"{f} (document {i + 1}): invalid YAML: {exc}") from None
            if not isinstance(doc, dict):
                raise ContractError(f"{f} (document {i + 1}): expected a mapping")
            docs.append((f"{f}#{i + 1}", doc, chunk.strip() + "\n"))
    return docs


def parse_bundle(docs: list[tuple[str, dict, str]], known_connections: dict[str, str] | None = None) -> Bundle:
    bundle = Bundle()
    for where, doc, _ in docs:
        if doc.get("apiVersion") != API_VERSION:
            raise ContractError(f"{where}: apiVersion must be {API_VERSION}")
        kind = doc.get("kind")
        if kind == "Connection":
            bundle.connections.append(parse_connection(doc, where))
        elif kind == "AlertChannel":
            bundle.channels.append(parse_channel(doc, where))
        elif kind != "DataContract":
            raise ContractError(f"{where}: kind must be Connection, DataContract or AlertChannel")
    types = dict(known_connections or {})
    types.update({c.name: c.type for c in bundle.connections})
    for where, doc, source in docs:
        if doc.get("kind") == "DataContract":
            bundle.contracts.append(parse_contract(doc, where, source, types))
    for label, items in (("connection", bundle.connections), ("contract", bundle.contracts), ("channel", bundle.channels)):
        names = [i.name for i in items]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ContractError(f"duplicate {label} name(s): {', '.join(sorted(dupes))}")
    return bundle


def load_bundle(paths: list[str | Path], known_connections: dict[str, str] | None = None) -> Bundle:
    return parse_bundle(load_documents(paths), known_connections)

"""Warehouse connectors.

Every query Nullwake sends is generated from validated identifiers or is a user-supplied
assertion that must be a single SELECT. Postgres sessions are opened READ ONLY with a statement
timeout; Snowflake and BigQuery rely on a read-only role (see docs/security.md) plus the same
timeout and the single-SELECT guard.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
import logging

log = logging.getLogger("nullwake.connectors")


# Fixed wording per SQLSTATE: says what went wrong without repeating anything the database put in its message.
SAFE_REASONS = {
    "57014": "statement timeout", "25006": "read-only transaction", "42P01": "table does not exist",
    "42703": "column does not exist", "42501": "permission denied", "42601": "syntax error",
    "22": "data exception", "08": "connection failure", "53": "insufficient resources", "57": "operator intervention",
}


def redacted(name: str, exc: BaseException) -> "ConnectorError":
    """Warehouse error text can quote data (e.g. `invalid input syntax for type integer: "4111-..."`). Keep only the
    error class and SQLSTATE in anything that is stored or alerted on; the full text goes to this host's log only."""
    code = getattr(exc, "sqlstate", None) or getattr(exc, "sqlstate_code", None) or getattr(exc, "code", None)
    log.warning("warehouse error on %s: %s", name, str(exc).strip()[:2000])
    code = code if isinstance(code, str) else ""
    reason = SAFE_REASONS.get(code) or SAFE_REASONS.get(code[:2]) or exc.__class__.__name__
    return ConnectorError(f"{name}: query failed: {reason}" + (f" (SQLSTATE {code})" if code else ""))

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
CONNECTION_TYPES = ("postgres", "snowflake", "bigquery")


class ConnectorError(Exception):
    """Raised for configuration or warehouse errors; becomes an `error` check result."""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool


def ident(value: str) -> str:
    if not isinstance(value, str) or not IDENT.match(value):
        raise ConnectorError(f"invalid identifier {value!r}: use letters, digits, _ or $")
    return value


def split_dataset(dataset: str, max_parts: int = 3) -> list[str]:
    parts = dataset.split(".")
    if not 1 <= len(parts) <= max_parts:
        raise ConnectorError(f"invalid dataset {dataset!r}")
    return [ident(p) for p in parts]


_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def guard_select(sql: str) -> str:
    """Defense in depth for user assertions. The real boundary is the read-only session/role."""
    body = sql.strip().rstrip(";").strip()
    if not _SELECT_ONLY.match(body) or ";" in body:
        raise ConnectorError("assertion SQL must be a single SELECT (or WITH ... SELECT) statement")
    return body


def _env(spec: dict, key: str, required: bool = True) -> str | None:
    name = spec.get(key)
    if not name:
        if required:
            raise ConnectorError(f"connection spec is missing {key}")
        return None
    value = os.environ.get(name)
    if value is None and required:
        raise ConnectorError(f"environment variable {name} is not set")
    return value


class Dialect:
    name = "generic"
    aliases: dict[str, str] = {}

    def table(self, dataset: str) -> str:
        return ".".join(split_dataset(dataset))

    def column(self, name: str) -> str:
        return ident(name)

    def age_seconds(self, column: str) -> str:
        raise NotImplementedError

    def ago(self, seconds: int) -> str:
        raise NotImplementedError

    def normalize_type(self, value: str) -> str:
        t = re.sub(r"\s+", " ", value.strip().lower())
        t = re.sub(r"\(.*\)$", "", t).strip()
        return self.aliases.get(t, t)

    # Shared query builders -------------------------------------------------------------
    def freshness_sql(self, dataset: str, column: str) -> str:
        return f"SELECT {self.age_seconds('MAX(' + self.column(column) + ')')}, COUNT(*) FROM {self.table(dataset)}"

    def volume_sql(self, dataset: str, column: str, window_hours: int, lookback_windows: int) -> str:
        col, w = self.column(column), int(window_hours) * 3600
        buckets = []
        for i in range(lookback_windows + 1):
            upper = "" if i == 0 else f" AND {col} < {self.ago(w * i)}"
            buckets.append(f"SUM(CASE WHEN {col} >= {self.ago(w * (i + 1))}{upper} THEN 1 ELSE 0 END)")
        return f"SELECT {', '.join(buckets)} FROM {self.table(dataset)} WHERE {col} >= {self.ago(w * (lookback_windows + 1))}"

    def null_sql(self, dataset: str, column: str) -> str:
        return f"SELECT COUNT(*), COUNT(*) - COUNT({self.column(column)}) FROM {self.table(dataset)}"

    def duplicate_sql(self, dataset: str, columns: list[str]) -> str:
        cols = ", ".join(self.column(c) for c in columns)
        not_null = " AND ".join(f"{self.column(c)} IS NOT NULL" for c in columns)
        return (f"SELECT COALESCE(SUM(n - 1), 0) FROM (SELECT COUNT(*) AS n FROM {self.table(dataset)} "
                f"WHERE {not_null} GROUP BY {cols} HAVING COUNT(*) > 1) d")


class PostgresDialect(Dialect):
    name = "postgres"
    aliases = {
        "timestamptz": "timestamp with time zone", "timestamp": "timestamp without time zone",
        "int8": "bigint", "int": "integer", "int4": "integer", "int2": "smallint",
        "varchar": "character varying", "char": "character", "bool": "boolean",
        "float8": "double precision", "float4": "real", "decimal": "numeric",
    }

    def age_seconds(self, column: str) -> str:
        return f"EXTRACT(EPOCH FROM (now() - {column}))"

    def ago(self, seconds: int) -> str:
        return f"(now() - interval '{int(seconds)} seconds')"


class SnowflakeDialect(Dialect):
    name = "snowflake"
    aliases = {
        "int": "number", "integer": "number", "bigint": "number", "smallint": "number", "decimal": "number",
        "numeric": "number", "string": "text", "varchar": "text", "char": "text", "character": "text",
        "timestamp": "timestamp_ntz", "datetime": "timestamp_ntz", "double": "float", "real": "float",
        "float8": "float", "bool": "boolean",
    }

    def age_seconds(self, column: str) -> str:
        return f"DATEDIFF('second', {column}, CURRENT_TIMESTAMP())"

    def ago(self, seconds: int) -> str:
        return f"DATEADD('second', -{int(seconds)}, CURRENT_TIMESTAMP())"


class BigQueryDialect(Dialect):
    name = "bigquery"
    aliases = {"integer": "int64", "int": "int64", "bigint": "int64", "float": "float64", "boolean": "bool",
               "numeric": "numeric", "decimal": "numeric", "text": "string", "varchar": "string"}

    def table(self, dataset: str) -> str:
        return "`" + ".".join(split_dataset(dataset)) + "`"

    def age_seconds(self, column: str) -> str:
        return f"TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), {column}, SECOND)"

    def ago(self, seconds: int) -> str:
        return f"TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(seconds)} SECOND)"


DIALECTS = {"postgres": PostgresDialect(), "snowflake": SnowflakeDialect(), "bigquery": BigQueryDialect()}


class Connector:
    dialect: Dialect

    def __init__(self, name: str, spec: dict, timeout_seconds: int):
        self.name, self.spec, self.timeout = name, spec, int(timeout_seconds)

    def row(self, sql: str) -> tuple:
        raise NotImplementedError

    def columns(self, dataset: str) -> list[ColumnInfo]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PostgresConnector(Connector):
    dialect = DIALECTS["postgres"]

    def __init__(self, name: str, spec: dict, timeout_seconds: int):
        super().__init__(name, spec, timeout_seconds)
        import psycopg
        dsn = _env(spec, "dsnEnv")
        try:
            self.conn = psycopg.connect(dsn, application_name="nullwake", connect_timeout=10)
        except psycopg.Error as exc:
            raise ConnectorError(f"cannot connect to {name}: {exc.__class__.__name__}") from None
        self.conn.read_only = True

    def _run(self, sql: str, params: tuple = ()) -> list[tuple]:
        import psycopg
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {self.timeout * 1000}")
                cur.execute("SET LOCAL TIME ZONE 'UTC'")
                cur.execute(sql, params)
                return cur.fetchall()
        except psycopg.Error as exc:
            raise redacted(self.name, exc) from None

    def row(self, sql: str) -> tuple:
        rows = self._run(sql)
        if len(rows) != 1:
            raise ConnectorError(f"expected one row, got {len(rows)}")
        return rows[0]

    def columns(self, dataset: str) -> list[ColumnInfo]:
        parts = split_dataset(dataset, 2)
        schema, table = (parts if len(parts) == 2 else ["public", parts[0]])
        rows = self._run(
            "SELECT column_name, data_type, is_nullable = 'YES' FROM information_schema.columns "
            "WHERE table_schema = lower(%s) AND table_name = lower(%s) ORDER BY ordinal_position",
            (schema, table))
        return [ColumnInfo(r[0], self.dialect.normalize_type(r[1]), bool(r[2])) for r in rows]

    def close(self) -> None:
        self.conn.close()


class SnowflakeConnector(Connector):  # pragma: no cover - requires a Snowflake account
    dialect = DIALECTS["snowflake"]

    def __init__(self, name: str, spec: dict, timeout_seconds: int):
        super().__init__(name, spec, timeout_seconds)
        try:
            import snowflake.connector
        except ImportError:
            raise ConnectorError("install nullwake[snowflake] to use Snowflake connections") from None
        params: dict[str, Any] = {k: spec[k] for k in ("account", "user", "warehouse", "database", "role") if spec.get(k)}
        params["session_parameters"] = {"STATEMENT_TIMEOUT_IN_SECONDS": self.timeout, "QUERY_TAG": "nullwake"}
        if spec.get("privateKeyFileEnv"):
            params["private_key_file"] = _env(spec, "privateKeyFileEnv")
        else:
            params["password"] = _env(spec, "passwordEnv")
        try:
            self.conn = snowflake.connector.connect(**params)
        except Exception as exc:
            raise ConnectorError(f"cannot connect to {name}: {exc.__class__.__name__}") from None

    def row(self, sql: str) -> tuple:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        except Exception as exc:
            raise redacted(self.name, exc) from None
        if len(rows) != 1:
            raise ConnectorError(f"expected one row, got {len(rows)}")
        return tuple(rows[0])

    def columns(self, dataset: str) -> list[ColumnInfo]:
        parts = split_dataset(dataset)
        db, schema, table = ([self.spec.get("database", "")] * (3 - len(parts)) + parts)[-3:]
        prefix = f"{ident(db)}." if db else ""
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT column_name, data_type, is_nullable = 'YES' FROM {prefix}information_schema.columns "
                        "WHERE table_schema = UPPER(%s) AND table_name = UPPER(%s) ORDER BY ordinal_position",
                        (schema, table))
            return [ColumnInfo(r[0].lower(), self.dialect.normalize_type(r[1]), bool(r[2])) for r in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()


class BigQueryConnector(Connector):  # pragma: no cover - requires a GCP project
    dialect = DIALECTS["bigquery"]

    def __init__(self, name: str, spec: dict, timeout_seconds: int):
        super().__init__(name, spec, timeout_seconds)
        try:
            from google.cloud import bigquery
        except ImportError:
            raise ConnectorError("install nullwake[bigquery] to use BigQuery connections") from None
        self.bq = bigquery
        creds = _env(spec, "credentialsFileEnv", required=False)
        self.client = (bigquery.Client.from_service_account_json(creds, project=spec.get("project"))
                       if creds else bigquery.Client(project=spec.get("project")))

    def row(self, sql: str) -> tuple:
        config = self.bq.QueryJobConfig(labels={"tool": "nullwake"},
                                        maximum_bytes_billed=int(self.spec.get("maximumBytesBilled", 10 * 1024**3)))
        job = None
        try:
            job = self.client.query(sql, job_config=config)
            rows = list(job.result(timeout=self.timeout))
        except Exception as exc:
            if job is not None and not job.done():
                try:  # result(timeout=) only stops waiting; cancel so the job stops using slots and bytes
                    job.cancel()
                except Exception:
                    log.warning("could not cancel BigQuery job %s", getattr(job, "job_id", "?"))
            raise redacted(self.name, exc) from None
        if len(rows) != 1:
            raise ConnectorError(f"expected one row, got {len(rows)}")
        return tuple(rows[0].values())

    def columns(self, dataset: str) -> list[ColumnInfo]:
        parts = split_dataset(dataset)
        project, ds, table = ([self.spec.get("project", "")] * (3 - len(parts)) + parts)[-3:]
        sql = (f"SELECT column_name, data_type, is_nullable = 'YES' FROM `{ident(project)}.{ds}.INFORMATION_SCHEMA.COLUMNS` "
               f"WHERE table_name = @t ORDER BY ordinal_position")
        config = self.bq.QueryJobConfig(query_parameters=[self.bq.ScalarQueryParameter("t", "STRING", table)])
        rows = self.client.query(sql, job_config=config).result(timeout=self.timeout)
        return [ColumnInfo(r[0], self.dialect.normalize_type(r[1]), bool(r[2])) for r in rows]


CONNECTORS = {"postgres": PostgresConnector, "snowflake": SnowflakeConnector, "bigquery": BigQueryConnector}


def open_connector(name: str, type_: str, spec: dict, timeout_seconds: int) -> Connector:
    if type_ not in CONNECTORS:
        raise ConnectorError(f"unsupported connection type {type_!r}")
    return CONNECTORS[type_](name, spec, timeout_seconds)

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


def raw_errors_logged() -> bool:
    """Warehouse error text can quote row values. It reaches this host's log only when an operator opts in for
    debugging (NULLWAKE_LOG_WAREHOUSE_ERRORS=1); by default only the error class and SQLSTATE are logged."""
    return os.environ.get("NULLWAKE_LOG_WAREHOUSE_ERRORS", "").lower() in ("1", "true", "yes")


def _warn_unverified_tls(name: str, dsn: str) -> None:
    """A warehouse reached over a network should prove its identity: sslmode=require encrypts but does not check the
    certificate or host name. Local sockets, loopback and single-label hosts (a container network) are exempt."""
    try:
        from psycopg.conninfo import conninfo_to_dict
        info = conninfo_to_dict(dsn)
    except Exception:
        return
    host = str(info.get("host") or "")
    remote = host and not host.startswith("/") and "." in host and host not in ("127.0.0.1", "::1") \
        and not host.startswith("localhost")
    if remote and info.get("sslmode") != "verify-full":
        log.warning("connection %s reaches %s without sslmode=verify-full; the server's certificate and host name "
                    "are not verified (set sslmode=verify-full and sslrootcert)", name, host)


def redacted(name: str, exc: BaseException) -> "ConnectorError":
    """Warehouse error text can quote data (e.g. `invalid input syntax for type integer: "4111-..."`). Keep only the
    error class and SQLSTATE in anything that is stored or alerted on; the full text goes to this host's log only."""
    code = getattr(exc, "sqlstate", None) or getattr(exc, "sqlstate_code", None) or getattr(exc, "code", None)
    code = code if isinstance(code, str) and re.fullmatch(r"[0-9A-Z]{5}", code) else ""
    if raw_errors_logged():
        log.warning("warehouse error on %s: %s", name, str(exc).strip()[:2000])
    else:
        log.warning("warehouse error on %s: %s (SQLSTATE %s)", name, exc.__class__.__name__, code or "unknown")
    reason = SAFE_REASONS.get(code) or SAFE_REASONS.get(code[:2]) or exc.__class__.__name__
    return ConnectorError(f"{name}: query failed: {reason}" + (f" (SQLSTATE {code})" if code else ""))

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
BQ_PROJECT = re.compile(r"^(?:[a-z][a-z0-9.-]{0,61}:)?[a-z][a-z0-9-]{4,28}[a-z0-9]$")


def bq_project(value: str) -> str:
    """Google Cloud project IDs contain hyphens (e.g. acme-analytics), which plain identifiers do not allow."""
    if not isinstance(value, str) or not BQ_PROJECT.match(value):
        raise ConnectorError(f"invalid BigQuery project {value!r}")
    return value
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
    def freshness_sql(self, dataset: str, column: str, scan_hours: int | None = None) -> str:
        col = self.column(column)
        where = f" WHERE {col} >= {self.ago(int(scan_hours) * 3600)}" if scan_hours else ""
        return f"SELECT {self.age_seconds('MAX(' + col + ')')}, COUNT(*) FROM {self.table(dataset)}{where}"

    def volume_sql(self, dataset: str, column: str, window_hours: int, lookback_windows: int,
                   step_hours: int | None = None) -> str:
        """Row counts for the latest window, then for lookback_windows earlier windows of the same length. Earlier
        windows start step_hours apart: by default they are consecutive; 24 compares the same time on earlier
        days, 168 the same time on earlier weeks."""
        col, w = self.column(column), int(window_hours) * 3600
        step = int(step_hours or window_hours) * 3600
        buckets = []
        for i in range(lookback_windows + 1):
            upper = f" AND {col} <= {self.ago(0)}" if i == 0 else f" AND {col} < {self.ago(step * i)}"
            buckets.append(f"SUM(CASE WHEN {col} >= {self.ago(step * i + w)}{upper} THEN 1 ELSE 0 END)")
        return (f"SELECT {', '.join(buckets)} FROM {self.table(dataset)} "
                f"WHERE {col} >= {self.ago(step * lookback_windows + w)}")

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
        "timestamp": "timestamp_ntz", "datetime": "timestamp_ntz", "timestamp without time zone": "timestamp_ntz",
        "timestamp with time zone": "timestamp_tz", "timestamptz": "timestamp_tz", "double": "float",
        "double precision": "float", "real": "float", "float4": "float", "float8": "float", "bool": "boolean",
        "varbinary": "binary",
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
        parts = dataset.split(".")
        if len(parts) == 3:
            return "`" + ".".join([bq_project(parts[0])] + split_dataset(".".join(parts[1:]), 2)) + "`"
        return "`" + ".".join(split_dataset(dataset, 2)) + "`"

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

    def tables(self, schema: str) -> list[str]:
        """Tables and views in one schema (Postgres schema, Snowflake schema or database.schema, BigQuery dataset)."""
        raise NotImplementedError

    def primary_key(self, dataset: str) -> list[str]:
        """Declared primary-key columns, or [] if none is declared or the warehouse cannot tell us."""
        return []

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
        self._refuse_own_database()
        _warn_unverified_tls(name, dsn)

    def _refuse_own_database(self) -> None:
        """A connection that lands in Nullwake's own metadata database (users, sessions, tokens) is refused, whatever
        the DSN is called: counting queries there could leak account data to anyone who can apply contracts."""
        found = self._run("SELECT to_regclass('api_tokens') IS NOT NULL AND to_regclass('sessions') IS NOT NULL "
                          "AND to_regclass('alembic_version') IS NOT NULL")
        if found and found[0][0]:
            self.conn.close()
            raise ConnectorError(f"cannot connect to {self.name}: it points at Nullwake's own database; "
                                 "use a read-only role on the warehouse instead")

    def _run(self, sql: str, params: tuple = (), max_rows: int | None = None) -> list[tuple]:
        import psycopg
        try:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {self.timeout * 1000}")
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    if not max_rows:
                        cur.execute(sql, params)
                        return cur.fetchall()
                # A server-side cursor: Postgres sends only the rows asked for, however many the query produces.
                with self.conn.cursor(name="nullwake_bounded") as cur:
                    cur.execute(sql, params or None)
                    return cur.fetchmany(max_rows)
        except psycopg.Error as exc:
            raise redacted(self.name, exc) from None

    def row(self, sql: str) -> tuple:
        rows = self._run(sql, max_rows=2)  # two is enough to tell "one row" from "more than one"
        if len(rows) != 1:
            raise ConnectorError(f"expected one row, got {len(rows)}")
        return rows[0]

    def columns(self, dataset: str) -> list[ColumnInfo]:
        parts = split_dataset(dataset, 2)
        if len(parts) == 2:
            schema, table = parts[0].lower(), parts[1].lower()  # unquoted identifiers fold to lower case
        else:  # resolve a bare name the way the checks' own queries do: through the session's search_path
            found = self._run("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n "
                              "ON n.oid = c.relnamespace WHERE c.oid = to_regclass(%s)", (parts[0],))
            if not found:
                return []
            schema, table = found[0]  # exact catalog names (a quoted schema may be mixed case)
        rows = self._run(
            "SELECT column_name, data_type, is_nullable = 'YES' FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table))
        return [ColumnInfo(r[0], self.dialect.normalize_type(r[1]), bool(r[2])) for r in rows]

    def tables(self, schema: str) -> list[str]:
        rows = self._run("SELECT table_name FROM information_schema.tables WHERE table_schema = %s "
                         "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name", (ident(schema).lower(),))
        return [r[0] for r in rows]

    def primary_key(self, dataset: str) -> list[str]:
        found = self._run("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                          "AND a.attnum = ANY(i.indkey) WHERE i.indisprimary AND i.indrelid = to_regclass(%s) "
                          "ORDER BY array_position(i.indkey, a.attnum)", (".".join(split_dataset(dataset, 2)),))
        return [r[0] for r in found]

    def close(self) -> None:
        self.conn.close()


class SnowflakeConnector(Connector):
    """Snowflake via snowflake-connector-python. Sessions run in UTC (Snowflake's default is America/Los_Angeles,
    which would shift every freshness age on TIMESTAMP_NTZ columns by the UTC offset), carry a server-side statement
    timeout and a query tag, and use a dedicated read-only role (docs/security.md)."""
    dialect = DIALECTS["snowflake"]

    def __init__(self, name: str, spec: dict, timeout_seconds: int):
        super().__init__(name, spec, timeout_seconds)
        try:
            import snowflake.connector
        except ImportError:
            raise ConnectorError("install nullwake[snowflake] to use Snowflake connections") from None
        for key in ("account", "user"):
            if not spec.get(key):
                raise ConnectorError(f"connection spec is missing {key}")
        params: dict[str, Any] = {k: spec[k] for k in ("account", "user", "warehouse", "database", "schema", "role")
                                  if spec.get(k)}
        params.update(
            application="nullwake", login_timeout=30, network_timeout=self.timeout + 30,
            session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": self.timeout, "QUERY_TAG": "nullwake",
                                "TIMEZONE": "UTC"})
        if spec.get("privateKeyFileEnv"):
            params["private_key_file"] = _env(spec, "privateKeyFileEnv")
            passphrase = _env(spec, "privateKeyPassphraseEnv", required=False)
            if passphrase:
                params["private_key_file_pwd"] = passphrase
        else:
            params["password"] = _env(spec, "passwordEnv")
        try:
            self.conn = snowflake.connector.connect(**params)
        except Exception as exc:
            log.warning("cannot connect to Snowflake connection %s: %s", name,
                        str(exc).strip()[:2000] if raw_errors_logged() else exc.__class__.__name__)
            raise ConnectorError(f"cannot connect to {name}: {exc.__class__.__name__}") from None

    def _run(self, sql: str, params: tuple | None = None, max_rows: int | None = None) -> list[tuple]:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                return [tuple(r) for r in (cur.fetchmany(max_rows) if max_rows else cur.fetchall())]
        except Exception as exc:
            raise redacted(self.name, exc) from None

    def row(self, sql: str) -> tuple:
        rows = self._run(sql, max_rows=2)  # two is enough to tell "one row" from "more than one"
        if len(rows) != 1:
            raise ConnectorError(f"expected one row, got {len(rows)}")
        return rows[0]

    def columns(self, dataset: str) -> list[ColumnInfo]:
        parts = split_dataset(dataset)
        default_schema = self.spec.get("schema") or "PUBLIC"
        db, schema, table = ([self.spec.get("database", ""), default_schema][:3 - len(parts)] + parts)[-3:] \
            if len(parts) < 3 else parts
        prefix = f"{ident(db)}." if db else ""
        rows = self._run(f"SELECT column_name, data_type, is_nullable = 'YES' FROM {prefix}information_schema.columns "
                         "WHERE table_schema = UPPER(%s) AND table_name = UPPER(%s) ORDER BY ordinal_position",
                         (ident(schema), ident(table)))
        return [ColumnInfo(r[0].lower(), self.dialect.normalize_type(r[1]), bool(r[2])) for r in rows]

    def tables(self, schema: str) -> list[str]:
        parts = split_dataset(schema, 2)
        db = parts[0] if len(parts) == 2 else self.spec.get("database", "")
        prefix = f"{ident(db)}." if db else ""
        rows = self._run(f"SELECT table_name FROM {prefix}information_schema.tables WHERE table_schema = UPPER(%s) "
                         "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name", (parts[-1],))
        return [r[0] for r in rows]

    def primary_key(self, dataset: str) -> list[str]:
        try:  # Snowflake records (but does not enforce) primary keys; SHOW returns column_name at index 4
            rows = self._run(f"SHOW PRIMARY KEYS IN TABLE {'.'.join(split_dataset(dataset))}")
            return [str(r[4]).lower() for r in sorted(rows, key=lambda r: r[5])]
        except ConnectorError:
            return []

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
            rows = list(job.result(timeout=self.timeout, max_results=2))
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
        raw = dataset.split(".")
        if len(raw) == 3:
            project, (ds, table) = bq_project(raw[0]), split_dataset(".".join(raw[1:]), 2)
        else:
            parts = split_dataset(dataset, 2)
            if len(parts) != 2:
                raise ConnectorError(f"BigQuery dataset must be dataset.table or project.dataset.table, got {dataset!r}")
            project, (ds, table) = bq_project(self.spec.get("project", "")), parts
        sql = (f"SELECT column_name, data_type, is_nullable = 'YES' FROM `{project}.{ds}.INFORMATION_SCHEMA.COLUMNS` "
               f"WHERE table_name = @t ORDER BY ordinal_position")
        config = self.bq.QueryJobConfig(
            query_parameters=[self.bq.ScalarQueryParameter("t", "STRING", table)], labels={"tool": "nullwake"},
            maximum_bytes_billed=int(self.spec.get("maximumBytesBilled", 10 * 1024**3)))
        rows = self.client.query(sql, job_config=config).result(timeout=self.timeout)
        return [ColumnInfo(r[0], self.dialect.normalize_type(r[1]), bool(r[2])) for r in rows]

    def _rows(self, sql: str, **params: str) -> list[tuple]:
        config = self.bq.QueryJobConfig(
            query_parameters=[self.bq.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()],
            labels={"tool": "nullwake"}, maximum_bytes_billed=int(self.spec.get("maximumBytesBilled", 10 * 1024**3)))
        try:
            return [tuple(r.values()) for r in self.client.query(sql, job_config=config).result(timeout=self.timeout)]
        except Exception as exc:
            raise redacted(self.name, exc) from None

    def _project_dataset(self, schema: str) -> tuple[str, str]:
        raw = schema.split(".")
        if len(raw) == 2:
            return bq_project(raw[0]), ident(raw[1])
        return bq_project(self.spec.get("project", "")), ident(schema)

    def tables(self, schema: str) -> list[str]:
        project, ds = self._project_dataset(schema)
        return [r[0] for r in self._rows(f"SELECT table_name FROM `{project}.{ds}.INFORMATION_SCHEMA.TABLES` "
                                         "WHERE table_type IN ('BASE TABLE', 'VIEW') ORDER BY table_name")]

    def primary_key(self, dataset: str) -> list[str]:
        raw = dataset.split(".")
        project, ds = self._project_dataset(".".join(raw[:-1]))
        try:
            rows = self._rows(f"SELECT k.column_name FROM `{project}.{ds}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE` k "
                              f"JOIN `{project}.{ds}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS` c USING (constraint_name) "
                              "WHERE c.constraint_type = 'PRIMARY KEY' AND k.table_name = @t "
                              "ORDER BY k.ordinal_position", t=ident(raw[-1]))
        except ConnectorError:
            return []
        return [r[0] for r in rows]


CONNECTORS = {"postgres": PostgresConnector, "snowflake": SnowflakeConnector, "bigquery": BigQueryConnector}


def open_connector(name: str, type_: str, spec: dict, timeout_seconds: int) -> Connector:
    if type_ not in CONNECTORS:
        raise ConnectorError(f"unsupported connection type {type_!r}")
    return CONNECTORS[type_](name, spec, timeout_seconds)

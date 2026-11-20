"""A least-privilege database role for the running services.

`nullwake migrate` runs as the database owner (it changes tables). The web and worker services only read and write
rows, so they connect as a separate role that can do exactly that: no DDL, no superuser, no access to other
databases, and read-only access to the migration version table. A compromised web or worker process then holds a
credential that cannot alter the schema, read other databases, or create roles.
"""
from __future__ import annotations

import re

ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def ensure_app_role(engine, role: str, password: str) -> None:
    """Create or update `role` with DML-only rights on the current database's public schema. Idempotent."""
    from psycopg import sql
    if not ROLE_NAME.match(role):
        raise ValueError("NULLWAKE_APP_DB_USER must be a lower-case identifier")
    if len(password) < 16:
        raise ValueError("NULLWAKE_APP_DB_PASSWORD must be at least 16 characters")
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            db, owner = cur.fetchone()
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            verb = "ALTER" if cur.fetchone() else "CREATE"
            # The password is composed client-side as a literal: utility statements take no bind parameters.
            cur.execute(sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}").format(
                sql.SQL(verb), sql.Identifier(role), sql.Literal(password)))
            r, d, o = sql.Identifier(role), sql.Identifier(db), sql.Identifier(owner)
            for stmt in (
                "REVOKE ALL ON DATABASE {d} FROM PUBLIC",
                "GRANT CONNECT ON DATABASE {d} TO {r}",
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                "GRANT USAGE ON SCHEMA public TO {r}",
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {r}",
                "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {r}",
                "REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {r}",
                "ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}",
                "ALTER DEFAULT PRIVILEGES FOR ROLE {o} IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {r}",
            ):
                cur.execute(sql.SQL(stmt).format(r=r, d=d, o=o))
    finally:
        raw.close()

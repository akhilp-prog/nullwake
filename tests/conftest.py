"""Test fixtures. Requires PostgreSQL; see README ("Running the tests")."""
from __future__ import annotations

import os

import psycopg
import pytest

TEST_DB = os.environ.setdefault("NULLWAKE_TEST_DATABASE_URL", "postgresql+psycopg://postgres@/nullwake_test?host=/tmp")
WAREHOUSE_ADMIN = os.environ.setdefault("NULLWAKE_TEST_WAREHOUSE_DSN", "host=/tmp dbname=warehouse user=postgres")
os.environ["NULLWAKE_DATABASE_URL"] = TEST_DB
os.environ["NULLWAKE_PUBLIC_URL"] = "http://testserver"
os.environ["TEST_WAREHOUSE_DSN"] = WAREHOUSE_ADMIN

from nullwake.config import get_settings  # noqa: E402

get_settings.cache_clear()

TABLES = ("alert_deliveries", "incident_events", "incidents", "check_runs", "monitors", "contracts", "connections",
          "alert_channels", "audit_log", "sessions", "api_tokens", "users", "worker_heartbeats", "login_attempts")


@pytest.fixture(scope="session", autouse=True)
def migrated():
    from alembic import command
    from nullwake.cli import _alembic_config
    with psycopg.connect(TEST_DB.replace("postgresql+psycopg", "postgresql"), autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    command.upgrade(_alembic_config(), "head")
    yield


@pytest.fixture(autouse=True)
def clean(migrated):
    with psycopg.connect(TEST_DB.replace("postgresql+psycopg", "postgresql"), autocommit=True) as conn:
        conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def db():
    from nullwake.db import session_factory
    session = session_factory()()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def wh():
    """Admin connection to a scratch schema in the test warehouse."""
    with psycopg.connect(WAREHOUSE_ADMIN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS nwtest CASCADE; CREATE SCHEMA nwtest")
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS nwtest CASCADE")


@pytest.fixture
def pg():
    from nullwake.connectors import PostgresConnector
    with PostgresConnector("test", {"dsnEnv": "TEST_WAREHOUSE_DSN"}, 30) as conn:
        yield conn


CONNECTION_YAML = """
apiVersion: nullwake/v1
kind: Connection
metadata: {name: wh}
spec: {type: postgres, dsnEnv: TEST_WAREHOUSE_DSN}
"""


def contract_yaml(body: str, name: str = "orders", dataset: str = "nwtest.orders") -> str:
    indented = "\n".join("  " + line for line in body.strip().splitlines())
    return f"""
apiVersion: nullwake/v1
kind: DataContract
metadata: {{name: {name}, owner: team@example.com}}
spec:
  connection: wh
  dataset: {dataset}
{indented}
"""

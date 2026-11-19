"""Regressions from the v0.5 review: cases that used to report a false pass."""
import pytest

from nullwake.connectors import DIALECTS, ConnectorError, bq_project
from nullwake.monitors import evaluate


class Fake:
    dialect = DIALECTS["postgres"]

    def __init__(self, row):
        self._row = row

    def row(self, sql):
        return self._row


def test_null_assertion_is_an_error_not_a_pass():
    r = evaluate(Fake((None,)), "assertion", "t", {"sql": "select sum(x) from t", "maxValue": 0})
    assert r.status == "error" and "NULL" in r.message
    assert evaluate(Fake((float("nan"),)), "assertion", "t", {"sql": "select 1", "maxValue": 0}).status == "error"
    assert evaluate(Fake((0,)), "assertion", "t", {"sql": "select 1", "maxValue": 0}).status == "pass"


def test_future_timestamp_cannot_hide_a_stopped_load():
    day_ahead = (-86400.0, 10)
    r = evaluate(Fake(day_ahead), "freshness", "t", {"column": "loaded_at", "maxAgeMinutes": 30})
    assert r.status == "fail" and "future" in r.message
    small_skew = (-60.0, 10)  # one minute of clock skew is tolerated
    assert evaluate(Fake(small_skew), "freshness", "t", {"column": "loaded_at", "maxAgeMinutes": 30}).status == "pass"


def test_volume_window_excludes_future_rows():
    sql = DIALECTS["postgres"].volume_sql("s.t", "loaded_at", 1, 2)
    first_bucket = sql.split("SUM(")[1]
    assert "<= (now() - interval '0 seconds')" in first_bucket


def test_bigquery_project_ids_with_hyphens():
    bq = DIALECTS["bigquery"]
    assert bq.table("acme-analytics.sales.orders") == "`acme-analytics.sales.orders`"
    assert bq.table("sales.orders") == "`sales.orders`"
    assert bq_project("nullwake-prod-48213") == "nullwake-prod-48213"
    for bad in ("acme`; drop", "x", "-acme-analytics"):
        with pytest.raises(ConnectorError):
            bq.table(f"{bad}.sales.orders")

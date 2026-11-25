"""OpenTelemetry: off by default; when on, a check run is a span tree with counters, and no values leave."""
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nullwake import telemetry
from nullwake.config import get_settings
from nullwake.worker import execute

from .test_engine import setup_monitor
from .test_scheduler import table


@pytest.fixture
def otel():
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    telemetry._bind(tracer_provider.get_tracer("test"), meter_provider.get_meter("test"))
    yield spans, reader
    telemetry.reset()


def _metrics(reader):
    out = {}
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = [(dict(p.attributes), getattr(p, "value", None) or getattr(p, "count", None))
                               for p in m.data.data_points]
    return out


def test_off_without_an_endpoint(monkeypatch):
    for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)
    assert telemetry.setup("worker") is False and not telemetry.enabled()
    with telemetry.span("x") as sp:  # call sites still work
        sp.set("a", 1)
    telemetry.count("runs", status="pass")


def test_kill_switch_wins_over_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    monkeypatch.setenv("NULLWAKE_OTEL", "false")
    assert telemetry.wanted() is False


def test_failing_check_is_traced_and_counted_without_values(db, tmp_path, wh, otel):
    spans, reader = otel
    table(wh, fresh=False)  # 3 hours old against a 30-minute contract
    mon = setup_monitor(db, tmp_path)
    assert execute(mon.id, get_settings(), trigger="manual") == "fail"

    by_name = {s.name: s for s in spans.get_finished_spans()}
    assert {"nullwake.check", "nullwake.warehouse.query", "nullwake.record_result"} <= set(by_name)
    root = by_name["nullwake.check"]
    assert by_name["nullwake.warehouse.query"].parent.span_id == root.context.span_id
    attrs = dict(root.attributes)
    assert attrs["nullwake.check.status"] == "fail" and attrs["nullwake.trigger"] == "manual"
    assert attrs["nullwake.dataset"] == "nwtest.orders" and attrs["db.system"] == "postgres"
    exported = " ".join(str(v) for s in spans.get_finished_spans() for v in dict(s.attributes).values())
    db.expire_all()
    message = db.get(type(mon), mon.id).last_message
    assert message and message not in exported  # the check's message (with its values) never leaves
    assert "TEST_WAREHOUSE_DSN" not in exported and "host=" not in exported

    metrics = _metrics(reader)
    assert ({"status": "fail", "kind": mon.kind, "trigger": "manual"}, 1) in metrics["nullwake.check.runs"]
    assert ({"event": "opened", "severity": mon.severity}, 1) in metrics["nullwake.incidents"]
    assert metrics["nullwake.check.duration"][0][1] == 1  # one histogram sample

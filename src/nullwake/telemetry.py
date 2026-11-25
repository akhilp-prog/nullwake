"""Optional OpenTelemetry traces and metrics.

Off unless an OTLP endpoint is configured with the standard variables (OTEL_EXPORTER_OTLP_ENDPOINT, or the
per-signal *_TRACES_ENDPOINT / *_METRICS_ENDPOINT), and only if the `otel` extra is installed. With it on, each
check run is a span with child spans for the warehouse query and the result write, alerts are spans, the web app's
requests are traced, and counters and a duration histogram are exported. Any OTLP backend works: SigNoz, Grafana,
Honeycomb, Datadog or a plain OpenTelemetry Collector.

What leaves Nullwake: check, contract, connection and dataset names, check kind, status, trigger, durations and
counts. Never a check's message or value, query text, rows, credentials or connection settings. Set
NULLWAKE_OTEL=false to keep it off even when the OTEL_* variables are set for other software.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator

from . import __version__

log = logging.getLogger("nullwake.telemetry")

_state: dict[str, Any] = {"on": False, "tracer": None, "meter": None, "instruments": {}}


def wanted() -> bool:
    if os.environ.get("NULLWAKE_OTEL", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return any(os.environ.get(k) for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                                           "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT"))


def setup(component: str) -> bool:
    """Configure exporters once per process. component: "web" or "worker". Returns whether telemetry is on."""
    if _state["on"]:
        return True
    if not wanted():
        return False
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but OpenTelemetry is not installed; "
                    "install nullwake[otel] to export traces and metrics")
        return False
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME") or f"nullwake-{component}",
                                "service.version": __version__, "service.namespace": "nullwake"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=30_000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    _bind(trace.get_tracer("nullwake", __version__), metrics.get_meter("nullwake", __version__))
    log.info("OpenTelemetry export on", extra={"service": resource.attributes.get("service.name")})
    return True


def _bind(tracer, meter) -> None:
    """Attach a tracer and meter (setup() does this; tests pass in-memory ones)."""
    _state.update(on=True, tracer=tracer, meter=meter, instruments={
        "runs": meter.create_counter("nullwake.check.runs", unit="{run}", description="Check runs by status"),
        "duration": meter.create_histogram("nullwake.check.duration", unit="ms",
                                           description="Wall time of a check run, warehouse query included"),
        "incidents": meter.create_counter("nullwake.incidents", unit="{incident}",
                                          description="Incidents opened and resolved"),
        "alerts": meter.create_counter("nullwake.alert.deliveries", unit="{delivery}",
                                       description="Alert delivery attempts by outcome"),
    })


def reset() -> None:
    _state.update(on=False, tracer=None, meter=None, instruments={})


def enabled() -> bool:
    return bool(_state["on"])


class _Span:
    """The small part of the span API Nullwake uses, so call sites read the same with telemetry off."""

    def __init__(self, span=None):
        self._span = span

    def set(self, key: str, value) -> None:
        if self._span is not None and value is not None:
            self._span.set_attribute(key, value)

    def fail(self, description: str) -> None:
        if self._span is not None:
            from opentelemetry.trace import Status, StatusCode
            self._span.set_status(Status(StatusCode.ERROR, description))


@contextlib.contextmanager
def span(name: str, **attributes) -> Iterator[_Span]:
    if not _state["on"]:
        yield _Span()
        return
    with _state["tracer"].start_as_current_span(name) as s:
        wrapped = _Span(s)
        for key, value in attributes.items():
            wrapped.set(key, value)
        yield wrapped


def count(instrument: str, amount: float = 1, **attributes) -> None:
    if _state["on"]:
        inst = _state["instruments"][instrument]
        clean = {k: v for k, v in attributes.items() if v is not None}
        (inst.record if instrument == "duration" else inst.add)(amount, clean)


def instrument_app(app) -> None:
    """Trace web requests (health, metrics and static files left out) if the FastAPI instrumentation is installed."""
    if not _state["on"]:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        return
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics,static")

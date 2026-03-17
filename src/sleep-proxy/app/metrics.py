"""Prometheus-Metriken für den Sleep-Proxy."""
from prometheus_client import Counter, Histogram

# Model labels stay intentionally low-cardinality because the set of served
# models is controlled by the Helm values, not by arbitrary user input.
REQUESTS_TOTAL = Counter(
    "sleep_proxy_requests_total",
    "Eingehende Requests, aufgeschlüsselt nach Modell und Status",
    ["model", "status"],
)

WAKE_TOTAL = Counter(
    "sleep_proxy_wake_total",
    "Anzahl ausgelöster wake_up-Aufrufe",
    ["model"],
)

WAKE_DURATION = Histogram(
    "sleep_proxy_wake_duration_seconds",
    "Dauer des Wake-up-Vorgangs in Sekunden",
    ["model"],
    buckets=[1, 5, 10, 20, 30, 45, 60, 90, 120],
)

FORWARDED_TOTAL = Counter(
    "sleep_proxy_forwarded_total",
    "Weitergereichte Requests, aufgeschlüsselt nach Modell und HTTP-Statuscode",
    ["model", "status_code"],
)

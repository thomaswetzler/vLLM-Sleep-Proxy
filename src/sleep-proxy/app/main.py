"""main.py – FastAPI Sleep-Proxy.

Endpunkte:
  GET  /health                   → Liveness-Check
  GET  /v1/models                → Proxy zu vllm-router (optional mit Node-Metadaten)
  GET  /v1/models/extended       → wie /v1/models, aber immer mit Node-Metadaten
  POST /v1/completions           → Wake-on-Demand + Proxy
  POST /v1/chat/completions      → Wake-on-Demand + Proxy
  POST /completions             → Alias für Clients ohne /v1-Präfix
  POST /chat/completions        → Alias für Clients ohne /v1-Präfix
  GET  /metrics                  → Prometheus-Metriken
  ANY  /{path:path}              → Catch-all Proxy (direkt weitergeleitet)
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import settings
from . import kube_client, router_client
from .proxy import forward_request, get_recent_requests, get_runtime_snapshot

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "sleep-proxy gestartet | router=%s | wake_timeout=%ds | auto_sleep=%s | auto_sleep_delay=%.1fs | auto_sleep_level=%d",
        settings.router_url,
        settings.wake_timeout_seconds,
        settings.auto_sleep_enabled,
        settings.auto_sleep_delay_seconds,
        settings.auto_sleep_level,
    )
    yield
    logger.info("sleep-proxy wird beendet")


app = FastAPI(
    title="vLLM Sleep Proxy",
    version="1.0.0",
    description=(
        "Transparenter Proxy mit Wake-on-Demand: "
        "schlafende vLLM-Engines werden automatisch aufgeweckt."
    ),
    lifespan=lifespan,
)


def _include_node_metadata(request: Request) -> bool:
    """Accept both `?include=node` and comma-separated include lists."""
    include_values = request.query_params.getlist("include")
    for include_value in include_values:
        for item in include_value.split(","):
            if item.strip().lower() == "node":
                return True
    return False


async def _models_response(*, include_node: bool) -> JSONResponse:
    """Return router `/v1/models`, optionally enriched with Kubernetes metadata."""
    payload = await router_client.get_models_payload()

    data = payload.get("data")
    if include_node and isinstance(data, list):
        model_locations = await kube_client.get_model_locations()
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str):
                # The proxy keeps the OpenAI-compatible payload intact and only
                # appends fields that callers can opt into.
                location = model_locations.get(model_id, {})
                item["node"] = location.get("node")
                item["nodes"] = location.get("nodes", [])
                item["replicas"] = location.get("replicas", 0)
                item["pods"] = location.get("pods", [])

    return JSONResponse(content=payload)


async def _debug_models_state() -> Dict[str, Any]:
    """Aggregate model, node and runtime data for the standalone ops UI."""
    payload = await router_client.get_models_payload()
    model_locations = await kube_client.get_model_locations()
    engine_groups = await router_client.get_engine_groups()
    runtime = get_runtime_snapshot()

    data = payload.get("data")
    models: List[Dict[str, Any]] = []
    engine_to_model: Dict[str, str] = {}

    for model_id, engine_ids in engine_groups.items():
        for engine_id in engine_ids:
            engine_to_model[engine_id] = model_id

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue

            location = model_locations.get(model_id, {})
            engine_ids = engine_groups.get(model_id, [])
            states: List[str] = []
            for engine_id in engine_ids:
                try:
                    states.append("sleeping" if await router_client.is_sleeping(engine_id) else "awake")
                except Exception:
                    states.append("unknown")

            if not states:
                state = "unknown"
            elif all(value == "sleeping" for value in states):
                state = "sleeping"
            elif all(value == "awake" for value in states):
                state = "awake"
            elif any(value == "awake" for value in states) and any(value == "sleeping" for value in states):
                state = "mixed"
            else:
                state = "unknown"

            models.append(
                {
                    "id": model_id,
                    "state": state,
                    "engine_ids": engine_ids,
                    "engine_states": states,
                    "node": location.get("node"),
                    "nodes": location.get("nodes", []),
                    "replicas": location.get("replicas", 0),
                    "pods": location.get("pods", []),
                    "engine_inflight": sum(
                        int(runtime.get("engine_inflight", {}).get(engine_id, 0))
                        for engine_id in engine_ids
                    ),
                }
            )

    nodes: List[Dict[str, Any]] = []
    grouped_by_node: Dict[str, List[Dict[str, Any]]] = {}
    for model in models:
        node_names = model.get("nodes") or ([model["node"]] if model.get("node") else [])
        for node_name in node_names:
            if isinstance(node_name, str) and node_name:
                grouped_by_node.setdefault(node_name, []).append(
                    {
                        "id": model["id"],
                        "state": model["state"],
                    }
                )

    for node_name in sorted(grouped_by_node):
        active_engine_id = runtime.get("node_active_engines", {}).get(node_name)
        nodes.append(
            {
                "name": node_name,
                "active_engine_id": active_engine_id,
                "active_model": engine_to_model.get(active_engine_id, ""),
                "inflight": int(runtime.get("node_inflight", {}).get(node_name, 0)),
                "models": grouped_by_node[node_name],
            }
        )

    return {
        "generated_at": int(time.time()),
        "models": models,
        "nodes": nodes,
        "runtime": runtime,
        "recent_requests": get_recent_requests(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["ops"])
async def health() -> Dict[str, str]:
    return {"status": "ok"}


# ──────────────────────────────────────────────────────────────────────────────
# Prometheus Metriken
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/metrics", tags=["ops"])
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(
        generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/debug/state", tags=["ops"])
async def debug_state() -> JSONResponse:
    """Expose current model and node state for the independent ops UI."""
    return JSONResponse(content=await _debug_models_state())


@app.get("/debug/requests", tags=["ops"])
async def debug_requests() -> JSONResponse:
    """Return recent proxied inference requests with routing context."""
    return JSONResponse(
        content={
            "generated_at": int(time.time()),
            "recent_requests": get_recent_requests(),
            "runtime": get_runtime_snapshot(),
        }
    )


# ──────────────────────────────────────────────────────────────────────────────
# OpenAI-kompatible Inferenz-Endpunkte (mit Wake-on-Demand)
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/v1/chat/completions", tags=["inference"])
async def chat_completions(request: Request) -> Response:
    """Chat-Completions – weckt schlafende Engine bei Bedarf auf."""
    return await forward_request(request, "v1/chat/completions")


@app.post("/chat/completions", tags=["inference"])
async def chat_completions_alias(request: Request) -> Response:
    """Alias für OpenAI-kompatible Clients, die `/chat/completions` senden."""
    return await forward_request(request, "v1/chat/completions")


@app.post("/v1/completions", tags=["inference"])
async def completions(request: Request) -> Response:
    """Text-Completions – weckt schlafende Engine bei Bedarf auf."""
    return await forward_request(request, "v1/completions")


@app.post("/completions", tags=["inference"])
async def completions_alias(request: Request) -> Response:
    """Alias für OpenAI-kompatible Clients, die `/completions` senden."""
    return await forward_request(request, "v1/completions")


# ──────────────────────────────────────────────────────────────────────────────
# Modell-Liste (kein Wake nötig – direkt proxyen)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/v1/models", tags=["models"])
async def v1_models(request: Request) -> Response:
    return await _models_response(include_node=_include_node_metadata(request))


@app.get("/v1/models/extended", tags=["models"])
async def v1_models_extended() -> Response:
    return await _models_response(include_node=True)


@app.get("/models", tags=["models"])
async def models_alias(request: Request) -> Response:
    return await _models_response(include_node=_include_node_metadata(request))


@app.get("/models/extended", tags=["models"])
async def models_alias_extended() -> Response:
    return await _models_response(include_node=True)


# ──────────────────────────────────────────────────────────────────────────────
# Catch-all – alles andere direkt weiterleiten (kein Wake-on-Demand)
# ──────────────────────────────────────────────────────────────────────────────
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def catch_all(request: Request, path: str) -> Response:
    """Leitet alle anderen Anfragen transparent weiter."""
    return await forward_request(request, path)

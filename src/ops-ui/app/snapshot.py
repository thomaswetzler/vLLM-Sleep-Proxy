"""Aggregate cross-service runtime data for the admin UI."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import settings


def _component_state_from_status(status_code: Optional[int], ok: bool) -> str:
    """Map HTTP results to a UI-friendly component state."""
    if ok:
        return "ok"
    if status_code is None:
        return "down"
    return "error"


def _component_detail(payload: Optional[Dict[str, Any]], error: Optional[str], ok: bool) -> str:
    """Render a short human-readable detail line for one component."""
    if ok:
        return "healthy"
    if error:
        return error
    if isinstance(payload, dict):
        for key in ("detail", "message", "raw"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return "unhealthy"


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str]]:
    """Read JSON and keep transport errors visible to the caller."""
    try:
        response = await client.get(url)
        payload: Dict[str, Any]
        if response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
        else:
            payload = {"raw": response.text}
        return payload, response.status_code, None
    except Exception as exc:  # pragma: no cover - defensive aggregation path
        return None, None, str(exc)


def _path_counts(requests: List[Dict[str, Any]]) -> Dict[str, int]:
    """Summarise direct vs semantic-router traffic from recent request traces."""
    counts = {"direct": 0, "semantic-router": 0}
    for item in requests:
        path_kind = str(item.get("path_kind", "direct") or "direct")
        if path_kind not in counts:
            counts[path_kind] = 0
        counts[path_kind] += 1
    return counts


def _status_counts(requests: List[Dict[str, Any]]) -> Dict[str, int]:
    """Summarise request outcomes for the top-level counters."""
    counts = {"ok": 0, "error": 0, "wake_failed": 0}
    for item in requests:
        status = str(item.get("status", "") or "")
        if status == "wake_failed":
            counts["wake_failed"] += 1
        elif status in {"error", "network_error"}:
            counts["error"] += 1
        elif status in {"ok", "streaming"}:
            counts["ok"] += 1
    return counts


def _request_series(requests: List[Dict[str, Any]], *, window_minutes: int = 12) -> List[Dict[str, Any]]:
    """Build a tiny minute-bucket time series for UI sparklines."""
    now = int(time.time())
    start = now - (window_minutes - 1) * 60
    buckets: List[Dict[str, Any]] = []
    for index in range(window_minutes):
        ts = start + index * 60
        buckets.append(
            {
                "timestamp": ts,
                "direct": 0,
                "semantic_router": 0,
                "wake_failed": 0,
            }
        )

    for item in requests:
        ts = int(item.get("timestamp", 0) or 0)
        if ts < start:
            continue
        bucket_index = min((ts - start) // 60, window_minutes - 1)
        bucket = buckets[bucket_index]
        path_kind = str(item.get("path_kind", "direct") or "direct")
        if path_kind == "semantic-router":
            bucket["semantic_router"] += 1
        else:
            bucket["direct"] += 1
        if str(item.get("status", "")) == "wake_failed":
            bucket["wake_failed"] += 1

    return buckets


def _model_summary(models: List[Dict[str, Any]]) -> Dict[str, int]:
    """Collapse model states into a single card-friendly summary."""
    summary = {"total": len(models), "awake": 0, "sleeping": 0, "mixed": 0, "unknown": 0}
    for model in models:
        state = str(model.get("state", "unknown") or "unknown")
        if state not in summary:
            summary[state] = 0
        summary[state] += 1
    return summary


def _engine_component_from_models(models: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Represent the serving engines as one diagram box."""
    if not models:
        return {
            "id": "vllm-engines",
            "label": "vLLM Engines",
            "status": "down",
            "detail": "no models discovered",
        }

    awake = sum(1 for item in models if item.get("state") == "awake")
    sleeping = sum(1 for item in models if item.get("state") == "sleeping")
    mixed = sum(1 for item in models if item.get("state") == "mixed")

    detail = f"{awake} awake, {sleeping} sleeping"
    if mixed:
        detail += f", {mixed} mixed"

    return {
        "id": "vllm-engines",
        "label": "vLLM Engines",
        "status": "ok" if models else "down",
        "detail": detail,
    }


def _gibibytes(value: int) -> float:
    """Convert bytes to GiB for compact UI display."""
    return round(value / (1024 ** 3), 1)


def _node_memory_summary(node_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse kubelet node summary stats into a cache-focused UI record."""
    node_memory = payload.get("node", {}).get("memory", {})
    pod_working_set_bytes = 0
    for pod in payload.get("pods", []):
        pod_memory = pod.get("memory", {})
        pod_working_set_bytes += int(pod_memory.get("workingSetBytes") or 0)

    usage_bytes = int(node_memory.get("usageBytes") or 0)
    working_set_bytes = int(node_memory.get("workingSetBytes") or 0)
    available_bytes = int(node_memory.get("availableBytes") or 0)
    cache_bytes = max(usage_bytes - working_set_bytes, 0)

    return {
        "name": node_name,
        "pod_ram_bytes": pod_working_set_bytes,
        "pod_ram_gib": _gibibytes(pod_working_set_bytes),
        "node_cache_bytes": cache_bytes,
        "node_cache_gib": _gibibytes(cache_bytes),
        "available_bytes": available_bytes,
        "available_gib": _gibibytes(available_bytes),
    }


def _cluster_node_names(payload: Dict[str, Any]) -> List[str]:
    """Extract node names from the Kubernetes node list response."""
    items = payload.get("items", [])
    names = []
    for item in items:
        metadata = item.get("metadata", {})
        name = str(metadata.get("name", "")).strip()
        if name:
            names.append(name)
    return sorted(names)


async def _fetch_kubernetes_json(
    client: httpx.AsyncClient,
    path: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[str]]:
    """Read JSON from the in-cluster Kubernetes API."""
    token = settings.kubernetes_token
    if not token:
        return None, None, "missing service account token"

    try:
        response = await client.get(
            f"{settings.kubernetes_api_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = response.json()
        return payload, response.status_code, None
    except Exception as exc:  # pragma: no cover - defensive aggregation path
        return None, None, str(exc)


async def build_snapshot() -> Dict[str, Any]:
    """Collect one coherent UI snapshot from the existing cluster services."""
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        sleep_state, sleep_code, sleep_error = await _fetch_json(
            client,
            f"{settings.sleep_proxy_url}/debug/state",
        )

        components: List[Dict[str, Any]] = []
        component_specs = [
            {
                "id": "litellm",
                "label": "LiteLLM",
                "url": f"{settings.litellm_url}/health/readiness" if settings.litellm_url else "",
            },
            {
                "id": "ops-ui",
                "label": "Ops UI",
                "url": f"{settings.ops_ui_url}/health" if settings.ops_ui_url else "",
            },
            {
                "id": "sleep-proxy",
                "label": "Sleep Proxy",
                "url": f"{settings.sleep_proxy_url}/health" if settings.sleep_proxy_url else "",
            },
            {
                "id": "vllm-router",
                "label": "vLLM Router",
                "url": f"{settings.vllm_router_url}/v1/models" if settings.vllm_router_url else "",
            },
            {
                "id": "cpu-embeddings",
                "label": "CPU Embeddings",
                "url": f"{settings.embeddings_url}/health" if settings.embeddings_url else "",
            },
            {
                "id": "whisper",
                "label": "Whisper",
                "url": f"{settings.whisper_url}/health" if settings.whisper_url else "",
            },
            {
                "id": "playground",
                "label": "Playground",
                "url": f"{settings.playground_url}/api/status" if settings.playground_url else "",
            },
        ]

        for spec in component_specs:
            if not spec["url"]:
                continue
            payload, status_code, error = await _fetch_json(client, spec["url"])
            ok = bool(status_code is not None and 200 <= status_code < 400)
            components.append(
                {
                    "id": spec["id"],
                    "label": spec["label"],
                    "status": _component_state_from_status(status_code, ok),
                    "status_code": status_code,
                    "detail": _component_detail(payload, error, ok),
                }
            )

    models = sleep_state.get("models", []) if isinstance(sleep_state, dict) else []
    nodes = sleep_state.get("nodes", []) if isinstance(sleep_state, dict) else []
    requests = sleep_state.get("recent_requests", []) if isinstance(sleep_state, dict) else []
    routing = {} if isinstance(sleep_state, dict) else {}

    node_memory: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        verify=settings.kubernetes_ca_path,
    ) as kube_client:
        node_list_payload, node_list_status_code, node_list_error = await _fetch_kubernetes_json(
            kube_client,
            "/api/v1/nodes",
        )
        node_names = (
            _cluster_node_names(node_list_payload)
            if not node_list_error and node_list_status_code == 200 and isinstance(node_list_payload, dict)
            else []
        )
        if node_list_error or node_list_status_code != 200:
            node_memory.append(
                {
                    "name": "cluster",
                    "error": node_list_error or f"HTTP {node_list_status_code}",
                }
            )
        else:
            for node_name in node_names:
                payload, status_code, error = await _fetch_kubernetes_json(
                    kube_client,
                    f"/api/v1/nodes/{node_name}/proxy/stats/summary",
                )
                if error or status_code != 200 or not isinstance(payload, dict):
                    node_memory.append(
                        {
                            "name": node_name,
                            "error": error or f"HTTP {status_code}",
                        }
                    )
                    continue
                node_memory.append(_node_memory_summary(node_name, payload))

    components.append(_engine_component_from_models(models))

    return {
        "generated_at": int(time.time()),
        "components": components,
        "models": models,
        "nodes": nodes,
        "node_memory": node_memory,
        "requests": requests,
        "request_paths": _path_counts(requests),
        "request_status": _status_counts(requests),
        "request_series": _request_series(requests),
        "model_summary": _model_summary(models),
        "routing": {
            "auto_model": routing.get("auto_model", {}),
            "default_model": routing.get("default_model"),
            "rules": routing.get("rules", []),
            "recent_decisions": routing.get("recent_decisions", []),
            "last_fallback": routing.get("last_fallback"),
        },
        "sources": {
            "sleep_proxy": {
                "status_code": sleep_code,
                "error": sleep_error,
            },
        },
    }

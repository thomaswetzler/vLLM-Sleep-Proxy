"""Aggregate cross-service runtime data for the admin UI."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

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


def _model_display_name(model_id: str) -> str:
    """Shorten local model ids for compact diagram rendering."""
    if model_id.startswith("local/"):
        return model_id.split("/", 1)[1]
    if model_id.startswith("openai/"):
        return model_id.split("/", 1)[1]
    return model_id


def _normalize_runtime_base(url: str) -> str:
    """Normalise API base URLs for stable matching across /v1 variants."""
    normalized = str(url or "").rstrip("/")
    if normalized.endswith("/v1"):
        return normalized[:-3]
    return normalized


def _runtime_component_from_models(
    models: List[Dict[str, Any]],
    *,
    component_id: str,
    label: str,
    empty_detail: str,
) -> Dict[str, Any]:
    """Represent one runtime family as one diagram box."""
    if not models:
        return {
            "id": component_id,
            "label": label,
            "status": "down",
            "detail": empty_detail,
            "models": [],
        }

    return {
        "id": component_id,
        "label": label,
        "status": "ok" if models else "down",
        "detail": "",
        "models": [
            {
                "label": _model_display_name(str(item.get("id", "-") or "-")),
                "node": str((item.get("nodes") or [item.get("node") or "-"])[0] or "-"),
                "status": str(item.get("state", "unknown") or "unknown"),
            }
            for item in models
        ],
    }


def _runtime_bucket(model: Dict[str, Any]) -> str:
    """Collapse runtime strings into the UI buckets we render explicitly."""
    runtime = str(model.get("runtime", "") or "").strip().lower()
    if runtime == "llama_cpp":
        return "llama"
    if runtime == "vllm":
        return "vllm"
    return "unknown"


def _litellm_model_entries_from_configmap(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract LiteLLM model_list entries from the runtime ConfigMap."""
    if not isinstance(payload, dict):
        return []

    proxy_config_raw = (
        payload.get("data", {}).get("proxy_config.yaml")
        if isinstance(payload.get("data"), dict)
        else None
    )
    if not isinstance(proxy_config_raw, str) or not proxy_config_raw.strip():
        return []

    try:
        proxy_config = yaml.safe_load(proxy_config_raw) or {}
    except Exception:
        return []

    model_list = proxy_config.get("model_list")
    if not isinstance(model_list, list):
        return []
    return [entry for entry in model_list if isinstance(entry, dict)]


def _litellm_model_api_base(entry: Dict[str, Any]) -> str:
    """Read the configured upstream API base for one LiteLLM model entry."""
    params = entry.get("litellm_params")
    if not isinstance(params, dict):
        return ""
    return str(params.get("api_base") or params.get("apiBase") or "").strip()


def _classify_runtime_model(
    model_name: str,
    api_base: str,
    managed_model_ids: set[str],
) -> str:
    """Classify a LiteLLM model into managed, cpu or external buckets."""
    if model_name in managed_model_ids:
        return "managed"

    normalized_base = _normalize_runtime_base(api_base)
    sleep_proxy_base = _normalize_runtime_base(settings.sleep_proxy_url)
    router_base = _normalize_runtime_base(settings.vllm_router_url)
    litellm_base = _normalize_runtime_base(settings.litellm_url)

    if normalized_base in {sleep_proxy_base, router_base, litellm_base}:
        return "managed"
    if ".svc.cluster.local" in normalized_base:
        return "cpu"
    if normalized_base:
        return "external"
    return "unknown"


async def _probe_runtime_api_base(
    client: httpx.AsyncClient,
    api_base: str,
) -> Tuple[str, Optional[int], Optional[str]]:
    """Probe one model runtime by trying a small set of common health endpoints."""
    normalized_base = _normalize_runtime_base(api_base)
    if not normalized_base:
        return "unknown", None, "missing api_base"

    probes = [
        f"{normalized_base}/health",
        f"{normalized_base}/v1/models",
        normalized_base,
    ]
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None

    for url in probes:
        payload, status_code, error = await _fetch_json(client, url)
        last_status_code = status_code
        last_error = error
        ok = bool(status_code is not None and 200 <= status_code < 400)
        if ok:
            return "permanent", status_code, None
        if status_code in {401, 403, 405}:
            return "permanent", status_code, None
        if status_code == 404:
            continue
        if error:
            continue
        if isinstance(payload, dict) and payload:
            return _component_state_from_status(status_code, ok), status_code, None

    if last_status_code is not None:
        return _component_state_from_status(last_status_code, False), last_status_code, last_error
    return "down", None, last_error


async def _cpu_models_from_litellm_config(
    client: httpx.AsyncClient,
    model_entries: List[Dict[str, Any]],
    runtime_models: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build runtime CPU model records from LiteLLM config entries."""
    managed_model_ids = {
        str(model.get("id", "") or "")
        for model in runtime_models
        if str(model.get("id", "") or "")
    }

    cpu_entries: List[Tuple[str, str]] = []
    for entry in model_entries:
        model_name = str(entry.get("model_name", "") or "").strip()
        api_base = _litellm_model_api_base(entry)
        runtime_kind = _classify_runtime_model(model_name, api_base, managed_model_ids)
        if runtime_kind != "cpu" or not model_name:
            continue
        cpu_entries.append((model_name, api_base))

    unique_probe_targets = {
        _normalize_runtime_base(api_base): api_base
        for _, api_base in cpu_entries
        if api_base
    }
    probe_states: Dict[str, str] = {}
    for normalized_base, api_base in unique_probe_targets.items():
        state, _, _ = await _probe_runtime_api_base(client, api_base)
        probe_states[normalized_base] = state

    models: List[Dict[str, Any]] = []
    for model_name, api_base in cpu_entries:
        state = probe_states.get(_normalize_runtime_base(api_base), "unknown")
        models.append(
            {
                "id": model_name,
                "label": _model_display_name(model_name),
                "state": state,
                "engine_ids": [],
                "engine_states": [state],
                "node": "cpu",
                "nodes": ["cpu"],
                "replicas": 1 if state not in {"down", "unknown"} else 0,
                "pods": [],
                "engine_inflight": 0,
                "runtime": "cpu",
            }
        )
    return models


def _cpu_component_from_models(models: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Represent CPU-served models as one diagram box."""
    if not models:
        return {
            "id": "cpu-models",
            "label": "CPU Modelle",
            "status": "down",
            "detail": "no cpu models discovered",
            "models": [],
        }

    overall_status = "ok" if all(model.get("state") == "permanent" for model in models) else "error"
    return {
        "id": "cpu-models",
        "label": "CPU Modelle",
        "status": overall_status,
        "detail": "",
        "models": [
            {
                "label": str(model.get("label", model.get("id", "-")) or "-"),
                "node": str((model.get("nodes") or [model.get("node") or "-"])[0] or "-"),
                "status": str(model.get("state", "unknown") or "unknown"),
            }
            for model in models
        ],
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

    runtime_models = sleep_state.get("models", []) if isinstance(sleep_state, dict) else []
    nodes = sleep_state.get("nodes", []) if isinstance(sleep_state, dict) else []
    requests = sleep_state.get("recent_requests", []) if isinstance(sleep_state, dict) else []
    routing = {} if isinstance(sleep_state, dict) else {}

    vllm_models = [
        model for model in runtime_models
        if isinstance(model, dict) and _runtime_bucket(model) == "vllm"
    ]
    llama_models = [
        model for model in runtime_models
        if isinstance(model, dict) and _runtime_bucket(model) == "llama"
    ]

    node_memory: List[Dict[str, Any]] = []
    litellm_configmap_payload: Optional[Dict[str, Any]] = None
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        verify=settings.kubernetes_ca_path,
    ) as kube_client:
        litellm_configmap_payload, _, _ = await _fetch_kubernetes_json(
            kube_client,
            f"/api/v1/namespaces/{settings.kubernetes_namespace}/configmaps/{settings.litellm_configmap_name}",
        )
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

    litellm_model_entries = _litellm_model_entries_from_configmap(litellm_configmap_payload)
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        cpu_models = await _cpu_models_from_litellm_config(client, litellm_model_entries, runtime_models)
    all_models = [*vllm_models, *llama_models, *cpu_models]

    components.append(
        _runtime_component_from_models(
            vllm_models,
            component_id="vllm-engines",
            label="vLLM Engines",
            empty_detail="no vLLM models discovered",
        )
    )
    components.append(
        _runtime_component_from_models(
            llama_models,
            component_id="llama-engines",
            label="llama.cpp Engines",
            empty_detail="no llama.cpp models discovered",
        )
    )
    components.append(_cpu_component_from_models(cpu_models))

    return {
        "generated_at": int(time.time()),
        "components": components,
        "models": all_models,
        "vllm_models": vllm_models,
        "llama_models": llama_models,
        "cpu_models": cpu_models,
        "nodes": nodes,
        "node_memory": node_memory,
        "requests": requests,
        "request_paths": _path_counts(requests),
        "request_status": _status_counts(requests),
        "request_series": _request_series(requests),
        "model_summary": _model_summary(all_models),
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

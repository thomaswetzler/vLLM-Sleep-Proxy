"""Kubernetes-Metadaten für Modell->Node-Anreicherung."""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_SERVICEACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_TOKEN_PATH = _SERVICEACCOUNT_DIR / "token"
_NAMESPACE_PATH = _SERVICEACCOUNT_DIR / "namespace"

_model_locations_cache: Dict[str, Dict[str, Any]] = {}
_model_locations_cache_ts: float = 0.0
_model_locations_cache_lock = asyncio.Lock()


def _kube_api_base_url() -> Optional[str]:
    """Build the in-cluster Kubernetes API URL from service environment vars."""
    host = os.getenv("KUBERNETES_SERVICE_HOST")
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS") or os.getenv("KUBERNETES_SERVICE_PORT")
    if not host or not port:
        return None
    return f"https://{host}:{port}"


def _pod_namespace() -> Optional[str]:
    """Prefer explicit config, otherwise fall back to the serviceaccount file."""
    if settings.pod_namespace:
        return settings.pod_namespace
    try:
        namespace = _NAMESPACE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return namespace or None


def _serviceaccount_token() -> Optional[str]:
    """Read the in-cluster bearer token if the pod runs with RBAC access."""
    try:
        token = _TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _ca_cert_path() -> Union[bool, str]:
    """Use the mounted cluster CA when present, otherwise trust system CAs."""
    ca_path = Path(settings.kubernetes_ca_cert_path)
    if ca_path.exists():
        return str(ca_path)
    return True


def _iter_model_ids(container: Dict[str, Any]) -> Iterable[str]:
    """Yield served aliases first and raw model paths as a compatibility fallback."""
    served_model_names: List[str] = []
    model_paths: List[str] = []

    for field in ("command", "args"):
        values = container.get(field)
        if not isinstance(values, list):
            continue

        index = 0
        while index < len(values):
            value = values[index]
            if not isinstance(value, str):
                index += 1
                continue

            if value == "--served-model-name":
                index += 1
                while index < len(values):
                    candidate = values[index]
                    if not isinstance(candidate, str) or candidate.startswith("--"):
                        break
                    served_model_names.append(candidate)
                    index += 1
                continue

            if value.startswith("/data/models/"):
                model_paths.append(value)

            index += 1

    if served_model_names:
        for model_name in served_model_names:
            yield model_name

    for model_path in model_paths:
        yield model_path


def _pod_sort_key(pod: Dict[str, Any]) -> Tuple[str, str, str]:
    """Sort newer pods first so replacements win over stale replicas."""
    status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
    metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    return (
        str(status.get("startTime") or ""),
        str(metadata.get("creationTimestamp") or ""),
        str(metadata.get("name") or ""),
    )


def _pod_is_ready(pod: Dict[str, Any]) -> bool:
    """Treat only `Running` and ready vLLM pods as valid model locations."""
    status = pod.get("status")
    if not isinstance(status, dict):
        return False
    if status.get("phase") != "Running":
        return False

    container_statuses = status.get("containerStatuses")
    if not isinstance(container_statuses, list):
        return False

    for container_status in container_statuses:
        if not isinstance(container_status, dict):
            continue
        if container_status.get("name") == "vllm":
            return bool(container_status.get("ready"))
    return False


def _extract_model_locations(pods_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Convert raw pod payload into the model metadata shape exposed by `/v1/models`."""
    items = pods_payload.get("items")
    if not isinstance(items, list):
        return {}

    candidates: Dict[str, List[Tuple[Tuple[str, str, str], Dict[str, str]]]] = {}

    for pod in items:
        if not isinstance(pod, dict) or not _pod_is_ready(pod):
            continue

        metadata = pod.get("metadata")
        spec = pod.get("spec")
        status = pod.get("status")
        if not isinstance(metadata, dict) or not isinstance(status, dict):
            continue
        if not isinstance(spec, dict):
            continue

        node_name = spec.get("nodeName")
        if not isinstance(node_name, str) or not node_name:
            continue
        pod_name = metadata.get("name")
        pod_ip = status.get("podIP")
        if not isinstance(pod_name, str) or not pod_name:
            continue

        containers = spec.get("containers")
        if not isinstance(containers, list):
            continue

        sort_key = _pod_sort_key(pod)
        for container in containers:
            if not isinstance(container, dict) or container.get("name") != "vllm":
                continue
            for model_path in _iter_model_ids(container):
                # Model ids come from the actual container command, not from pod
                # names, so node annotations stay correct even if naming changes.
                pod_info = {
                    "name": pod_name,
                    "node": node_name,
                    "ip": pod_ip if isinstance(pod_ip, str) and pod_ip else None,
                }
                candidates.setdefault(model_path, []).append((sort_key, pod_info))

    locations: Dict[str, Dict[str, Any]] = {}
    for model_path, entries in candidates.items():
        entries.sort(key=lambda item: item[0], reverse=True)
        pods: List[Dict[str, Any]] = []
        seen_pods = set()
        for _, pod_info in entries:
            pod_name = pod_info["name"]
            if pod_name in seen_pods:
                continue
            seen_pods.add(pod_name)
            pods.append(dict(pod_info))

        nodes = sorted(
            {
                pod["node"]
                for pod in pods
                if isinstance(pod.get("node"), str) and pod["node"]
            }
        )
        locations[model_path] = {
            "node": nodes[0] if len(nodes) == 1 else None,
            "nodes": nodes,
            "pods": pods,
            "replicas": len(pods),
        }

    return locations


async def get_model_locations() -> Dict[str, Dict[str, Any]]:
    """Liefert Pod-/Node-Metadaten pro Modell für aktive Serving-Engines."""
    global _model_locations_cache, _model_locations_cache_ts

    now = time.monotonic()
    async with _model_locations_cache_lock:
        if now - _model_locations_cache_ts < settings.model_node_cache_ttl_seconds:
            return {
                model_id: dict(location)
                for model_id, location in _model_locations_cache.items()
            }
        # Keep the last successful view as a fallback so temporary API or RBAC
        # issues do not make `/v1/models?include=node` flap unnecessarily.
        fallback = {
            model_id: dict(location)
            for model_id, location in _model_locations_cache.items()
        }

    base_url = _kube_api_base_url()
    namespace = _pod_namespace()
    token = _serviceaccount_token()
    if not base_url or not namespace or not token:
        logger.debug("Kubernetes-Metadaten nicht verfügbar - Node-Anreicherung wird übersprungen")
        return fallback

    try:
        async with httpx.AsyncClient(
            timeout=settings.kubernetes_api_timeout_seconds,
            verify=_ca_cert_path(),
        ) as client:
            response = await client.get(
                f"{base_url}/api/v1/namespaces/{namespace}/pods",
                params={"labelSelector": "app.kubernetes.io/component=serving-engine"},
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        mapping = _extract_model_locations(response.json())
    except Exception as exc:
        logger.warning("Kubernetes-Pod-Metadaten konnten nicht gelesen werden: %s", exc)
        return fallback

    async with _model_locations_cache_lock:
        _model_locations_cache = mapping
        _model_locations_cache_ts = time.monotonic()

    return {
        model_id: dict(location)
        for model_id, location in mapping.items()
    }

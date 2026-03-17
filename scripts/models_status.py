#!/usr/bin/env python3
"""List models with node placement and sleep state.

The script prefers the enriched sleep-proxy `/v1/models?include=node` endpoint,
but can still reconstruct node placement from Kubernetes when the running proxy
image is older or temporarily unavailable.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple


def fetch_json(url: str, timeout: int = 20, method: str = "GET") -> Any:
    """Fetch JSON from a URL with a tiny standard-library-only client."""
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def as_model_list(payload: Any) -> List[Any]:
    """Accept both OpenAI-style `{data: [...]}` and plain list payloads."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    if isinstance(payload, list):
        return payload
    return []


def engine_map(payload: Any) -> Dict[str, List[str]]:
    """Flatten router `/engines` payloads into `{model_id: [engine_ids...]}`."""
    mapping: Dict[str, List[str]] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            engine_id = item.get("engine_id")
            if not isinstance(engine_id, str) or not engine_id:
                continue
            for model_id in item.get("serving_models", []):
                if isinstance(model_id, str) and model_id:
                    mapping.setdefault(model_id, []).append(engine_id)
    elif isinstance(payload, dict):
        for engine_id, item in payload.items():
            if not isinstance(item, dict):
                continue
            model_id = item.get("model_url") or item.get("model")
            if isinstance(model_id, str) and model_id:
                mapping.setdefault(model_id, []).append(engine_id)
    return mapping


def pod_is_ready(pod: Dict[str, Any]) -> bool:
    """Mirror the proxy's readiness check so fallback node data stays consistent."""
    status = pod.get("status")
    if not isinstance(status, dict) or status.get("phase") != "Running":
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


def iter_model_ids(container: Dict[str, Any]) -> Iterable[str]:
    """Extract served aliases first and raw model paths as fallback ids."""
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


def model_locations_from_pods(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Rebuild model->node metadata from Kubernetes pod JSON."""
    if not isinstance(payload, dict):
        return {}

    items = payload.get("items")
    if not isinstance(items, list):
        return {}

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for pod in items:
        if not isinstance(pod, dict) or not pod_is_ready(pod):
            continue

        metadata = pod.get("metadata")
        spec = pod.get("spec")
        status = pod.get("status")
        if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
            continue

        pod_name = metadata.get("name")
        node_name = spec.get("nodeName")
        pod_ip = status.get("podIP")
        if not isinstance(pod_name, str) or not pod_name:
            continue
        if not isinstance(node_name, str) or not node_name:
            continue

        containers = spec.get("containers")
        if not isinstance(containers, list):
            continue

        for container in containers:
            if not isinstance(container, dict) or container.get("name") != "vllm":
                continue
            for model_path in iter_model_ids(container):
                grouped.setdefault(model_path, []).append(
                    {
                        "name": pod_name,
                        "node": node_name,
                        "ip": pod_ip if isinstance(pod_ip, str) and pod_ip else None,
                    }
                )

    locations: Dict[str, Dict[str, Any]] = {}
    for model_path, pods in grouped.items():
        unique_pods: List[Dict[str, Any]] = []
        seen = set()
        for pod in pods:
            pod_name = pod["name"]
            if pod_name in seen:
                continue
            seen.add(pod_name)
            unique_pods.append(pod)

        nodes = sorted({pod["node"] for pod in unique_pods if isinstance(pod.get("node"), str) and pod["node"]})
        locations[model_path] = {
            "node": nodes[0] if len(nodes) == 1 else None,
            "nodes": nodes,
            "pods": unique_pods,
            "replicas": len(unique_pods),
        }

    return locations


def fetch_pod_locations(kubectl: str, namespace: str) -> Dict[str, Dict[str, Any]]:
    """Use kubectl as a fallback when the API response has no node metadata yet."""
    if not namespace:
        return {}

    try:
        payload = subprocess.check_output(
            [
                kubectl,
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/component=serving-engine",
                "-o",
                "json",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            env=os.environ.copy(),
        )
    except Exception:
        return {}

    try:
        return model_locations_from_pods(json.loads(payload))
    except Exception:
        return {}


def node_display_for(item: Dict[str, Any]) -> str:
    """Render single-node and multi-node placements into one table cell."""
    nodes = item.get("nodes")
    if isinstance(nodes, list):
        node_values = [str(node) for node in nodes if isinstance(node, str) and node]
    else:
        node_values = []
    if not node_values:
        node = item.get("node")
        if isinstance(node, str) and node:
            node_values = [node]
    return ",".join(node_values) if node_values else "-"


def replicas_for(item: Dict[str, Any]) -> int:
    """Prefer explicit replica counts, otherwise derive them from pod metadata."""
    replicas = item.get("replicas")
    if isinstance(replicas, int):
        return replicas
    pods = item.get("pods")
    if isinstance(pods, list):
        return len(pods)
    return 0


def sleep_state_for(router_url: str, engine_id: str, sleep_cache: Dict[str, Any]) -> Any:
    """Cache per-engine sleep state to avoid hitting the router repeatedly."""
    if engine_id not in sleep_cache:
        payload = fetch_json(
            router_url + "/is_sleeping?id=" + urllib.parse.quote(engine_id, safe=""),
            timeout=5,
        )
        value = payload.get("is_sleeping") if isinstance(payload, dict) else None
        sleep_cache[engine_id] = value if isinstance(value, bool) else None
    return sleep_cache[engine_id]


def aggregate_state(engine_ids: List[str], states: List[Any]) -> str:
    """Collapse per-engine booleans into one user-facing model state."""
    if not engine_ids:
        return "unknown"
    if any(state is None for state in states):
        return "unknown"
    if all(states):
        return "sleeping"
    if not any(states):
        return "awake"
    return "mixed"


def model_entries(models_payload: Any, engines_payload: Any, router_url: str) -> List[Dict[str, Any]]:
    """Join model metadata, engine ids and sleep state into table rows."""
    sleep_cache: Dict[str, Any] = {}
    engines_by_model = engine_map(engines_payload)
    entries: List[Dict[str, Any]] = []

    for item in as_model_list(models_payload):
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("model") or item.get("name")
        else:
            model_id = item if isinstance(item, str) else None
            item = {}

        if not isinstance(model_id, str) or not model_id:
            continue

        engine_ids = engines_by_model.get(model_id, [])
        states: List[Any] = []
        for engine_id in engine_ids:
            try:
                states.append(sleep_state_for(router_url, engine_id, sleep_cache))
            except Exception:
                states.append(None)

        entries.append(
            {
                "model": model_id,
                "state": aggregate_state(engine_ids, states),
                "node": node_display_for(item),
                "replicas": str(replicas_for(item)),
                "engine_ids": engine_ids,
                "engine_states": states,
            }
        )

    return entries


def table_rows(entries: List[Dict[str, Any]]) -> List[Tuple[str, str, str, str]]:
    """Convert structured entries into printable table tuples."""
    return [
        (
            str(entry["model"]),
            str(entry["state"]),
            str(entry["node"]),
            str(entry["replicas"]),
        )
        for entry in entries
    ]


def print_table(rows: List[Tuple[str, str, str, str]], include_index: bool = False) -> None:
    """Print an aligned plain-text table without external dependencies."""
    headers = ["MODEL", "STATE", "NODE", "REPLICAS"]
    if include_index:
        headers.insert(0, "NR")
    widths = [len(header) for header in headers]
    rendered_rows: List[Tuple[str, ...]] = []
    for idx, row in enumerate(rows, start=1):
        rendered_row: Tuple[str, ...]
        if include_index:
            rendered_row = (str(idx),) + row
        else:
            rendered_row = row
        rendered_rows.append(rendered_row)
        for idx, value in enumerate(rendered_row):
            widths[idx] = max(widths[idx], len(value))

    print("  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print("  ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in rendered_rows:
        print("  ".join(value.ljust(widths[idx]) for idx, value in enumerate(row)))


def choose_entry(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Prompt for a model index and return the selected entry if valid."""
    print_table(table_rows(entries), include_index=True)
    try:
        selection = input("Modellnummer eingeben: ").strip()
    except EOFError:
        return None
    if not selection.isdigit():
        return None
    index = int(selection)
    if index < 1 or index > len(entries):
        return None
    return entries[index - 1]


def toggle_model(entry: Dict[str, Any], router_url: str, sleep_level: int) -> None:
    """Switch all engines of one model between sleep and awake."""
    model_id = str(entry["model"])
    state = str(entry["state"])
    engine_ids = [engine_id for engine_id in entry.get("engine_ids", []) if isinstance(engine_id, str) and engine_id]

    if not engine_ids:
        raise RuntimeError(f"Kein Engine-Mapping fuer Modell '{model_id}' gefunden.")
    if state == "mixed":
        raise RuntimeError(f"Modell '{model_id}' hat gemischte Engine-Zustaende und wird nicht automatisch umgeschaltet.")
    if state == "unknown":
        raise RuntimeError(f"Status von Modell '{model_id}' ist unbekannt.")

    if state == "sleeping":
        action = "wake_up"
        next_state = "awake"
        endpoint = lambda engine_id: router_url + "/wake_up?id=" + urllib.parse.quote(engine_id, safe="")
    else:
        action = "sleep"
        next_state = "sleeping"
        endpoint = lambda engine_id: (
            router_url
            + "/sleep?id="
            + urllib.parse.quote(engine_id, safe="")
            + "&level="
            + str(sleep_level)
        )

    for engine_id in engine_ids:
        try:
            fetch_json(endpoint(engine_id), timeout=120, method="POST")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"{action} fuer Engine '{engine_id}' fehlgeschlagen (HTTP {exc.code}): {details or exc.reason}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"{action} fuer Engine '{engine_id}' fehlgeschlagen: {exc}") from exc

    print(f"Modell '{model_id}' wurde von {state} nach {next_state} umgeschaltet.")


def main() -> int:
    """CLI entrypoint used by `make models` and `make toggle-model`."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-url", required=True)
    parser.add_argument("--router-url", required=True)
    parser.add_argument("--namespace", default="")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--toggle", action="store_true")
    parser.add_argument("--sleep-level", type=int, default=1)
    args = parser.parse_args()

    router_url = args.router_url.rstrip("/")

    try:
        try:
            models_payload = fetch_json(args.models_url, timeout=10)
        except Exception as exc:
            warn(f"Gateway-Models-Endpunkt fehlgeschlagen, nutze Router-Fallback: {exc}")
            models_payload = fetch_json(router_url + "/v1/models", timeout=10)
        engines_payload = fetch_json(router_url + "/engines", timeout=10)
    except Exception as exc:
        print(f"Fehler beim Laden der Modellinfos: {exc}", file=sys.stderr)
        return 1

    fallback_locations = fetch_pod_locations(args.kubectl, args.namespace)
    if fallback_locations and isinstance(models_payload, dict) and isinstance(models_payload.get("data"), list):
        # Merge proxy-native metadata with kubectl-derived fields without
        # clobbering richer values from newer sleep-proxy images.
        for item in models_payload["data"]:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str):
                continue
            location = fallback_locations.get(model_id)
            if not isinstance(location, dict):
                continue
            if not item.get("node"):
                item["node"] = location.get("node")
            if not item.get("nodes"):
                item["nodes"] = location.get("nodes", [])
            if not item.get("pods"):
                item["pods"] = location.get("pods", [])
            replicas = item.get("replicas")
            if not isinstance(replicas, int) or replicas == 0:
                item["replicas"] = location.get("replicas", 0)

    entries = sorted(model_entries(models_payload, engines_payload, router_url), key=lambda entry: str(entry["model"]))
    if not entries:
        print("Keine verfuegbaren Modelle gefunden.", file=sys.stderr)
        return 1

    if args.toggle:
        selected_entry = choose_entry(entries)
        if selected_entry is None:
            return fail("Ungueltige Auswahl.")
        try:
            toggle_model(selected_entry, router_url, args.sleep_level)
        except RuntimeError as exc:
            return fail(str(exc))
        try:
            refreshed_models_payload = fetch_json(args.models_url, timeout=10)
        except Exception:
            refreshed_models_payload = fetch_json(router_url + "/v1/models", timeout=10)
        refreshed_engines_payload = fetch_json(router_url + "/engines", timeout=10)
        refreshed_entries = sorted(
            model_entries(refreshed_models_payload, refreshed_engines_payload, router_url),
            key=lambda entry: str(entry["model"]),
        )
        print()
        print_table(table_rows(refreshed_entries))
        return 0

    print_table(table_rows(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Runtime-aware engine operations for vLLM router and direct engines."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from .config import settings
from .engine_catalog import EngineCatalogEntry, find_entry_for_model, list_entries
from . import router_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendEngine:
    """Resolved engine endpoint used by wake/sleep and forwarding logic."""

    model: str
    engine_id: str
    runtime: str
    endpoint: str
    node_name: Optional[str] = None


def _catalog_entry_to_engine(entry: EngineCatalogEntry) -> BackendEngine:
    """Convert one static catalog entry into a resolved backend engine."""
    return BackendEngine(
        model=entry.model,
        engine_id=entry.engine_id,
        runtime=entry.runtime,
        endpoint=entry.endpoint,
        node_name=entry.node_name,
    )


def build_vllm_engine(model: str, engine_id: str) -> BackendEngine:
    """Represent one router-discovered vLLM engine in the generic shape."""
    return BackendEngine(
        model=model,
        engine_id=engine_id,
        runtime="vllm",
        endpoint=settings.router_url,
    )


async def resolve_engine(model: str) -> Optional[BackendEngine]:
    """Resolve a model either from the static catalog or from the router."""
    entry = find_entry_for_model(model)
    if entry is not None:
        return _catalog_entry_to_engine(entry)

    engine_id = await router_client.resolve_engine_id(model)
    if engine_id is None:
        return None
    return build_vllm_engine(model, engine_id)


def resolve_model_node_hint(model: str) -> Optional[str]:
    """Return the statically configured node for non-router engines."""
    entry = find_entry_for_model(model)
    return entry.node_name if entry is not None else None


async def get_engine_groups() -> Dict[str, List[str]]:
    """Return router engine groups plus static direct-runtime engines."""
    groups: Dict[str, List[str]] = {}

    try:
        groups.update(await router_client.get_engine_groups())
    except Exception as exc:
        logger.warning("Router-Engine-Liste konnte nicht geladen werden: %s", exc)

    for entry in list_entries():
        groups.setdefault(entry.model, []).append(entry.engine_id)

    return {
        model_id: list(engine_ids)
        for model_id, engine_ids in groups.items()
        if engine_ids
    }


def _synthetic_model_payload(entry: EngineCatalogEntry) -> Dict[str, Any]:
    """Build a minimal OpenAI-compatible /v1/models item."""
    return {
        "id": entry.model,
        "object": "model",
        "created": 0,
        "owned_by": entry.runtime,
    }


async def get_models_payload() -> Dict[str, Any]:
    """Return router models plus statically declared direct-runtime models."""
    payload: Dict[str, Any] = {"object": "list", "data": []}

    try:
        router_payload = await router_client.get_models_payload()
        if isinstance(router_payload, dict):
            payload.update(router_payload)
    except Exception as exc:
        logger.warning("Router-/v1/models konnte nicht geladen werden: %s", exc)

    data = payload.get("data")
    if not isinstance(data, list):
        data = []
        payload["data"] = data

    existing_model_ids = {
        str(item.get("id", "") or "")
        for item in data
        if isinstance(item, dict)
    }

    for entry in list_entries():
        if entry.model in existing_model_ids:
            continue
        data.append(_synthetic_model_payload(entry))

    return payload


async def engine_for_model_and_id(model: str, engine_id: str) -> Optional[BackendEngine]:
    """Resolve a concrete engine object for a known model/engine id pair."""
    entry = find_entry_for_model(model)
    if entry is not None and entry.engine_id == engine_id:
        return _catalog_entry_to_engine(entry)

    if engine_id:
        return build_vllm_engine(model, engine_id)
    return None


async def request_url_for_model(model: str, path: str, *, query_string: str = "") -> str:
    """Return the downstream URL for one routed request path."""
    engine = await resolve_engine(model)
    base_url = engine.endpoint if engine is not None else settings.router_url
    suffix = f"?{query_string}" if query_string else ""
    return f"{base_url}/{path.lstrip('/')}{suffix}"


async def _direct_is_sleeping(endpoint: str) -> bool:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{endpoint}/is_sleeping")
    response.raise_for_status()
    payload = response.json()
    value = payload.get("is_sleeping")
    if isinstance(value, bool):
        return value
    raise RuntimeError(f"unerwartetes is_sleeping-Payload fuer direct endpoint {endpoint!r}: {payload!r}")


async def is_sleeping(engine: BackendEngine) -> bool:
    """Check one engine's sleep state regardless of runtime."""
    if engine.runtime == "vllm":
        return await router_client.is_sleeping(engine.engine_id)
    return await _direct_is_sleeping(engine.endpoint)


async def wake_up(engine: BackendEngine) -> None:
    """Wake one engine regardless of runtime."""
    if engine.runtime == "vllm":
        await router_client.wake_up(engine.engine_id)
        return

    async with httpx.AsyncClient(timeout=settings.router_action_timeout_seconds) as client:
        response = await client.post(f"{engine.endpoint}/wake_up")
    response.raise_for_status()


async def sleep(engine: BackendEngine, level: int = 1) -> None:
    """Put one engine to sleep regardless of runtime."""
    if engine.runtime == "vllm":
        await router_client.sleep(engine.engine_id, level=level)
        return

    async with httpx.AsyncClient(timeout=settings.router_action_timeout_seconds) as client:
        response = await client.post(
            f"{engine.endpoint}/sleep",
            params={"level": level},
        )
    response.raise_for_status()


async def poll_until_ready(engine: BackendEngine) -> None:
    """Wait until an engine reports itself as awake."""
    deadline = time.monotonic() + settings.wake_timeout_seconds
    while time.monotonic() < deadline:
        try:
            if not await is_sleeping(engine):
                logger.info("engine %r (%s) ist wach und bereit", engine.engine_id, engine.runtime)
                return
        except Exception as exc:
            logger.warning("poll_until_ready Fehler fuer engine %r: %s", engine.engine_id, exc)
        await asyncio.sleep(settings.poll_interval_seconds)

    raise TimeoutError(
        f"engine {engine.engine_id!r} ist nach {settings.wake_timeout_seconds}s nicht aufgewacht"
    )


async def poll_until_sleeping(engine: BackendEngine) -> None:
    """Wait until an engine reports itself as sleeping."""
    deadline = time.monotonic() + settings.wake_timeout_seconds
    while time.monotonic() < deadline:
        try:
            if await is_sleeping(engine):
                logger.info("engine %r (%s) schlaeft", engine.engine_id, engine.runtime)
                return
        except Exception as exc:
            logger.warning("poll_until_sleeping Fehler fuer engine %r: %s", engine.engine_id, exc)
        await asyncio.sleep(settings.poll_interval_seconds)

    raise TimeoutError(
        f"engine {engine.engine_id!r} ist nach {settings.wake_timeout_seconds}s nicht eingeschlafen"
    )

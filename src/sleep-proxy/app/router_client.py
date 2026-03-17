"""router_client.py – Kapselt alle HTTP-Calls an den vllm-router.

Öffentliche API:
  get_engines()              -> dict[model_url, engine_id]  (gecacht, TTL konfigurierbar)
  resolve_engine_id(model)   -> engine_id | None
  is_sleeping(engine_id)     -> bool
  sleep(engine_id, level)    -> None
  wake_up(engine_id)         -> None
  poll_until_ready(engine_id)-> None  (wartet bis Modell wach ist, wirft TimeoutError)
  poll_until_sleeping(engine_id)-> None  (wartet bis Modell schlaeft, wirft TimeoutError)
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Engine-Cache
# ──────────────────────────────────────────────────────────────────────────────
_engines_cache: Dict[str, List[str]] = {}   # model_url → [engine_id, ...]
_engines_cache_ts: float = 0.0
_engines_cache_lock = asyncio.Lock()


async def get_engine_groups() -> Dict[str, List[str]]:
    """Gibt gecachte {model_url: [engine_id, ...]}-Tabelle zurück (TTL-gesteuert).

    Der vllm-router liefert unter GET /engines eine Liste:
      [
        { "engine_id": "<uuid>", "serving_models": ["/data/models/..."], "created": ... },
        ...
      ]
    Diese wird zu { model_url: engine_id } abgeflacht.
    """
    global _engines_cache, _engines_cache_ts

    now = time.monotonic()
    async with _engines_cache_lock:
        if now - _engines_cache_ts < settings.engine_cache_ttl_seconds and _engines_cache:
            return dict(_engines_cache)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{settings.router_url}/engines")
    resp.raise_for_status()

    data = resp.json()
    mapping: Dict[str, List[str]] = {}

    # Current router versions expose a list of engine objects.
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            engine_id = item.get("engine_id")
            if not isinstance(engine_id, str) or not engine_id:
                continue
            for model_url in item.get("serving_models", []):
                if isinstance(model_url, str) and model_url:
                    mapping.setdefault(model_url, []).append(engine_id)

    # Older router revisions used a dict keyed by engine id; keep supporting it
    # so mixed cluster states during upgrades do not break wake-on-demand.
    elif isinstance(data, dict):
        for engine_id, info in data.items():
            if not isinstance(info, dict):
                continue
            model_url = (
                info.get("model_url")
                or info.get("model")
                or info.get("modelURL")
                or info.get("model_name")
            )
            if model_url and isinstance(model_url, str):
                mapping.setdefault(model_url, []).append(engine_id)

    async with _engines_cache_lock:
        _engines_cache = mapping
        _engines_cache_ts = time.monotonic()

    logger.debug("engine-cache aktualisiert: %s", mapping)
    return {
        model_url: list(engine_ids)
        for model_url, engine_ids in mapping.items()
    }


async def get_engines() -> Dict[str, str]:
    """Flatten the engine groups to one primary engine id per model."""
    return {
        model_url: engine_ids[0]
        for model_url, engine_ids in (await get_engine_groups()).items()
        if engine_ids
    }


async def get_models_payload() -> Dict[str, Any]:
    """Liest das OpenAI-kompatible /v1/models-Payload direkt vom Router."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{settings.router_url}/v1/models")
    resp.raise_for_status()

    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"unerwartetes /v1/models-Payload: {payload!r}")
    return payload


async def resolve_engine_id(model: str) -> Optional[str]:
    """Gibt engine_id für das angegebene model zurück, None wenn unbekannt.

    Unterstützt sowohl vollständige Pfade (/data/models/...) als auch Kurznamen.
    """
    engines = await get_engines()

    # Prefer the exact model id because the playground and gateway now pass the
    # full model path explicitly when they can.
    if model in engines:
        return engines[model]

    # Keep a suffix fallback for older callers that still use short names.
    for model_url, engine_id in engines.items():
        if model_url.endswith(f"/{model}") or model_url.endswith(model):
            return engine_id

    # Accept old `/data/models/...` ids even after the user-facing alias switched
    # to `local/...` by comparing the last path component.
    requested_basename = model.rstrip("/").split("/")[-1]
    if requested_basename:
        for model_url, engine_id in engines.items():
            if model_url.rstrip("/").split("/")[-1] == requested_basename:
                return engine_id

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Sleep / Wake
# ──────────────────────────────────────────────────────────────────────────────

async def is_sleeping(engine_id: str) -> bool:
    """Gibt True zurück wenn der Engine-Pod schläft."""
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(
            f"{settings.router_url}/is_sleeping",
            params={"id": engine_id},
        )
    resp.raise_for_status()
    body = resp.json()
    value = body.get("is_sleeping")
    if isinstance(value, bool):
        return value
    raise RuntimeError(
        f"unerwartetes is_sleeping-Payload für engine {engine_id!r}: {body}"
    )


async def wake_up(engine_id: str) -> None:
    """Sendet POST /wake_up an den Router (der es an die Engine weiterleitet)."""
    async with httpx.AsyncClient(timeout=settings.router_action_timeout_seconds) as client:
        resp = await client.post(
            f"{settings.router_url}/wake_up",
            params={"id": engine_id},
        )
    resp.raise_for_status()
    logger.info("wake_up an engine %r gesendet", engine_id)


async def sleep(engine_id: str, level: int = 1) -> None:
    """Sendet POST /sleep an den Router."""
    async with httpx.AsyncClient(timeout=settings.router_action_timeout_seconds) as client:
        resp = await client.post(
            f"{settings.router_url}/sleep",
            params={"id": engine_id, "level": level},
        )
    resp.raise_for_status()
    logger.info("sleep(level=%s) an engine %r gesendet", level, engine_id)


async def poll_until_ready(engine_id: str) -> None:
    """Wartet bis is_sleeping False zurückgibt. Wirft TimeoutError nach wake_timeout_seconds."""
    deadline = time.monotonic() + settings.wake_timeout_seconds
    while time.monotonic() < deadline:
        try:
            sleeping = await is_sleeping(engine_id)
            if not sleeping:
                logger.info("engine %r ist wach und bereit", engine_id)
                return
        except Exception as exc:
            logger.warning("poll_until_ready Fehler für engine %r: %s", engine_id, exc)
        await asyncio.sleep(settings.poll_interval_seconds)

    raise TimeoutError(
        f"engine {engine_id!r} ist nach {settings.wake_timeout_seconds}s nicht aufgewacht"
    )


async def poll_until_sleeping(engine_id: str) -> None:
    """Wartet bis is_sleeping True zurückgibt. Wirft TimeoutError nach wake_timeout_seconds."""
    deadline = time.monotonic() + settings.wake_timeout_seconds
    while time.monotonic() < deadline:
        try:
            sleeping = await is_sleeping(engine_id)
            if sleeping:
                logger.info("engine %r schlaeft", engine_id)
                return
        except Exception as exc:
            logger.warning("poll_until_sleeping Fehler für engine %r: %s", engine_id, exc)
        await asyncio.sleep(settings.poll_interval_seconds)

    raise TimeoutError(
        f"engine {engine_id!r} ist nach {settings.wake_timeout_seconds}s nicht eingeschlafen"
    )

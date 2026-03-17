"""proxy.py – Wake-on-Demand-Logik + transparentes HTTP-Forwarding.

Kern-Ablauf pro Inferenz-Request:
  1. JSON-Body lesen, model-Feld extrahieren
  2. engine_id via router_client.get_engines() ermitteln (Cache)
  3. Konfliktmodell auf demselben Node ggf. schlafen legen
  4. is_sleeping(engine_id)?  → wake_up() + poll_until_ready()
  5. Request 1:1 an ROUTER_URL weiterleiten (httpx, Streaming-Support)
  6. Nach Request-Ende Engine wieder schlafen legen
  7. Response 1:1 zurück an den Client

Concurrency: Ein asyncio.Lock pro engine_id serialisiert Sleep/Wake-Operationen
             für dasselbe Modell. Zusätzlich reserviert ein Node-Semaphor den
             GPU-Knoten, bis das zuletzt aktive Modell wieder schläft.
"""

import asyncio
from collections import deque
import json
import logging
import time
from contextlib import suppress
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .config import settings
from .metrics import (
    FORWARDED_TOTAL,
    REQUESTS_TOTAL,
    WAKE_DURATION,
    WAKE_TOTAL,
)
from . import kube_client, router_client

logger = logging.getLogger(__name__)

_INTERNAL_ROUTING_HEADERS = {
    "x-selected-model",
    "x-original-model",
    "x-route-reason",
    "x-route-rule",
    "x-routing-path-kind",
}

# ──────────────────────────────────────────────────────────────────────────────
# Lock- und Status-Tabellen je engine_id (lazy-erzeugt)
# ──────────────────────────────────────────────────────────────────────────────
_engine_locks: Dict[str, asyncio.Lock] = {}
_engine_locks_meta = asyncio.Lock()
_engine_state_lock = asyncio.Lock()
_engine_inflight: Dict[str, int] = {}
_pending_sleep_tasks: Dict[str, asyncio.Task[None]] = {}

# Pro Node darf immer nur ein Modell aktiv sein. Der aktive Engine-Slot bleibt
# reserviert, bis das Modell nach dem letzten Request wieder schläft.
_node_conditions: Dict[str, asyncio.Condition] = {}
_node_conditions_meta = asyncio.Lock()
_node_active_engines: Dict[str, str] = {}
_node_inflight: Dict[str, int] = {}
_recent_requests: Deque[Dict[str, Any]] = deque(maxlen=settings.debug_history_size)
_POST_WAKE_RETRYABLE_STATUS_CODES = {502, 503, 504}


def _record_request(entry: Dict[str, Any]) -> None:
    """Keep a small in-memory trace buffer for the ops UI."""
    _recent_requests.appendleft(entry)


def get_recent_requests() -> List[Dict[str, Any]]:
    """Return a JSON-serialisable copy of recent proxied inference requests."""
    return [dict(item) for item in _recent_requests]


def get_runtime_snapshot() -> Dict[str, Any]:
    """Expose lock and reservation state without leaking asyncio internals."""
    return {
        "node_active_engines": dict(_node_active_engines),
        "node_inflight": dict(_node_inflight),
        "engine_inflight": dict(_engine_inflight),
        "pending_sleep_engines": sorted(_pending_sleep_tasks.keys()),
    }


async def _get_engine_lock(engine_id: str) -> asyncio.Lock:
    """Return a per-engine lock created lazily on first access."""
    async with _engine_locks_meta:
        if engine_id not in _engine_locks:
            _engine_locks[engine_id] = asyncio.Lock()
        return _engine_locks[engine_id]


async def _get_node_condition(node_name: str) -> asyncio.Condition:
    """Return the condition used to serialize model switches on one GPU node."""
    async with _node_conditions_meta:
        if node_name not in _node_conditions:
            _node_conditions[node_name] = asyncio.Condition()
        return _node_conditions[node_name]


async def _resolve_model_node(model: str) -> Optional[str]:
    """Resolve the unique node for a model if Kubernetes metadata can prove it."""
    try:
        location = (await kube_client.get_model_locations()).get(model, {})
    except Exception as exc:
        logger.warning("Node-Auflösung für Modell %r fehlgeschlagen: %s", model, exc)
        return None

    node_name = location.get("node")
    if isinstance(node_name, str) and node_name:
        return node_name
    return None


def _location_matches_node(location: Dict[str, Any], node_name: str) -> bool:
    """Match both single-node and replicated multi-node location payloads."""
    node = location.get("node")
    if isinstance(node, str) and node == node_name:
        return True

    nodes = location.get("nodes")
    if isinstance(nodes, list):
        for candidate in nodes:
            if isinstance(candidate, str) and candidate == node_name:
                return True

    return False


async def _sleep_conflicting_engines_on_node(
    node_name: str,
    *,
    target_model: str,
    target_engine_id: str,
) -> None:
    """Legt bereits wache Fremdmodelle auf demselben Node schlafen.

    Das schliesst den Fall ein, dass eine Engine manuell oder direkt am Router
    aufgeweckt wurde. Diese Requests laufen am sleep-proxy vorbei und belegen
    deshalb GPU-Speicher, ohne dass unsere interne Node-Reservation davon weiss.
    """
    try:
        model_locations = await kube_client.get_model_locations()
        engine_map = await router_client.get_engines()
    except Exception as exc:
        logger.warning(
            "Konfliktpruefung fuer Node %r vor Wake-up von %r fehlgeschlagen: %s",
            node_name,
            target_model,
            exc,
        )
        return

    conflicts = []
    for model_path, engine_id in engine_map.items():
        if engine_id == target_engine_id or model_path == target_model:
            continue

        location = model_locations.get(model_path)
        if not isinstance(location, dict) or not _location_matches_node(location, node_name):
            continue

        conflicts.append((model_path, engine_id))

    for model_path, engine_id in conflicts:
        lock = await _get_engine_lock(engine_id)
        async with lock:
            try:
                sleeping = await router_client.is_sleeping(engine_id)
            except Exception as exc:
                logger.warning(
                    "is_sleeping-Check fuer Konfliktmodell %r (engine %r) fehlgeschlagen: %s",
                    model_path,
                    engine_id,
                    exc,
                )
                continue

            if sleeping:
                continue

            logger.info(
                "Node %r blockiert Wake-up fuer %r: aktives Modell %r (engine %r) wird zuerst schlafen gelegt",
                node_name,
                target_model,
                model_path,
                engine_id,
            )
            try:
                await router_client.sleep(engine_id, level=settings.auto_sleep_level)
                await router_client.poll_until_sleeping(engine_id)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Aktives Modell {model_path!r} auf Node {node_name!r} "
                        f"konnte vor dem Wechsel zu {target_model!r} nicht in Sleep "
                        f"versetzt werden: {exc}"
                    ),
                ) from exc

            logger.info(
                "Konfliktmodell %r (engine %r) schlaeft jetzt - Wake-up fuer %r kann fortgesetzt werden",
                model_path,
                engine_id,
                target_model,
            )


async def _begin_node_request(node_name: str, engine_id: str, model: str) -> None:
    """Reserve a node-wide slot so only one model switch is active per GPU node."""
    condition = await _get_node_condition(node_name)
    waiting_logged = False

    async with condition:
        while True:
            active_engine = _node_active_engines.get(node_name)
            inflight = _node_inflight.get(node_name, 0)

            if active_engine is None:
                _node_active_engines[node_name] = engine_id
                _node_inflight[node_name] = 1
                if waiting_logged:
                    logger.info(
                        "Node %r fuer Modell %r wieder frei - Umschaltung laeuft weiter",
                        node_name,
                        model,
                    )
                return

            if active_engine == engine_id:
                _node_inflight[node_name] = inflight + 1
                return

            if not waiting_logged:
                logger.info(
                    "Modell %r wartet auf Node %r - aktive engine %r blockiert den Switch",
                    model,
                    node_name,
                    active_engine,
                )
                waiting_logged = True

            await condition.wait()


async def _release_node_reservation(
    node_name: str,
    engine_id: str,
    model: str,
    *,
    reason: str,
) -> None:
    """Release the node slot once the engine is no longer actively serving."""
    condition = await _get_node_condition(node_name)
    released = False

    async with condition:
        if (
            _node_active_engines.get(node_name) == engine_id
            and _node_inflight.get(node_name, 0) == 0
        ):
            _node_active_engines.pop(node_name, None)
            _node_inflight.pop(node_name, None)
            condition.notify_all()
            released = True

    if released:
        logger.info(
            "Node %r fuer Modell %r freigegeben (%s)",
            node_name,
            model,
            reason,
        )


async def _end_node_request(
    node_name: str,
    engine_id: str,
    model: str,
    *,
    schedule_sleep: bool,
) -> None:
    """Decrease the node inflight counter and optionally free the node immediately."""
    condition = await _get_node_condition(node_name)

    async with condition:
        inflight = max(_node_inflight.get(node_name, 0) - 1, 0)
        if inflight == 0:
            _node_inflight.pop(node_name, None)
        else:
            _node_inflight[node_name] = inflight

    if inflight == 0 and not schedule_sleep:
        await _release_node_reservation(
            node_name,
            engine_id,
            model,
            reason="request beendet ohne auto-sleep",
        )


async def _begin_engine_request(engine_id: str) -> None:
    """Track inflight work and cancel any pending auto-sleep for this engine."""
    async with _engine_state_lock:
        _engine_inflight[engine_id] = _engine_inflight.get(engine_id, 0) + 1
        task = _pending_sleep_tasks.pop(engine_id, None)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _end_engine_request(
    engine_id: str,
    model: str,
    *,
    schedule_sleep: bool,
    node_name: Optional[str] = None,
) -> None:
    """Update inflight state and schedule auto-sleep after the last request."""
    if not settings.auto_sleep_enabled:
        schedule_sleep = False

    async with _engine_state_lock:
        inflight = max(_engine_inflight.get(engine_id, 0) - 1, 0)
        if inflight == 0:
            _engine_inflight.pop(engine_id, None)
        else:
            _engine_inflight[engine_id] = inflight

        should_schedule = schedule_sleep and inflight == 0
        if should_schedule:
            existing = _pending_sleep_tasks.pop(engine_id, None)
        else:
            existing = None

    if existing is not None:
        existing.cancel()
        with suppress(asyncio.CancelledError):
            await existing

    if should_schedule:
        task = asyncio.create_task(
            _sleep_engine_after_idle(
                engine_id,
                model,
                node_name=node_name,
            )
        )
        async with _engine_state_lock:
            _pending_sleep_tasks[engine_id] = task


async def _sleep_engine_after_idle(
    engine_id: str,
    model: str,
    *,
    node_name: Optional[str] = None,
) -> None:
    """Sleep an engine after the configured idle delay if nothing restarted it."""
    try:
        if settings.auto_sleep_delay_seconds > 0:
            await asyncio.sleep(settings.auto_sleep_delay_seconds)

        lock = await _get_engine_lock(engine_id)
        async with lock:
            async with _engine_state_lock:
                if _engine_inflight.get(engine_id, 0) > 0:
                    return

            if node_name is not None:
                condition = await _get_node_condition(node_name)
                async with condition:
                    # Another engine may have taken over the node while this task
                    # was waiting; in that case the old task must silently abort.
                    if _node_active_engines.get(node_name) != engine_id:
                        return
                    if _node_inflight.get(node_name, 0) > 0:
                        return

            released = False
            try:
                if await router_client.is_sleeping(engine_id):
                    released = True
                else:
                    await router_client.sleep(
                        engine_id,
                        level=settings.auto_sleep_level,
                    )
                    released = True
                    logger.info(
                        "engine %r nach Request fuer Modell %r wieder schlafen gelegt",
                        engine_id,
                        model,
                    )
            except Exception as exc:
                with suppress(Exception):
                    released = await router_client.is_sleeping(engine_id)

                logger.warning(
                    "Auto-Sleep fuer engine %r (Modell %r) fehlgeschlagen: %s",
                    engine_id,
                    model,
                    exc,
                )

            if released and node_name is not None:
                await _release_node_reservation(
                    node_name,
                    engine_id,
                    model,
                    reason="engine sleeping",
                )
    except asyncio.CancelledError:
        raise
    finally:
        async with _engine_state_lock:
            task = _pending_sleep_tasks.get(engine_id)
            if task is asyncio.current_task():
                _pending_sleep_tasks.pop(engine_id, None)


# ──────────────────────────────────────────────────────────────────────────────
# Wake-on-Demand
# ──────────────────────────────────────────────────────────────────────────────

async def ensure_awake(
    model: str,
    engine_id: Optional[str] = None,
    *,
    node_name: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    """Weckt das Modell auf, wenn nötig. Gibt ``(engine_id, woke_up)`` zurück.

    Wenn kein engine_id gefunden wird (unbekanntes Modell), wird None zurückgegeben
    – der Request wird dann trotzdem weitergeleitet (Router entscheidet).
    """
    if engine_id is None:
        engine_id = await router_client.resolve_engine_id(model)
    if engine_id is None:
        logger.debug("engine_id für Modell %r nicht gefunden – kein Wake-on-Demand", model)
        return None, False

    lock = await _get_engine_lock(engine_id)
    async with lock:
        if node_name is not None:
            await _sleep_conflicting_engines_on_node(
                node_name,
                target_model=model,
                target_engine_id=engine_id,
            )

        try:
            sleeping = await router_client.is_sleeping(engine_id)
        except Exception as exc:
            logger.warning(
                "is_sleeping-Check für engine %r fehlgeschlagen: %s – weiter ohne Wake",
                engine_id,
                exc,
            )
            return engine_id, False

        if not sleeping:
            return engine_id, False

        logger.info("engine %r schläft – starte wake_up für Modell %r", engine_id, model)
        WAKE_TOTAL.labels(model=model).inc()
        t0 = time.monotonic()

        try:
            await router_client.wake_up(engine_id)
            await router_client.poll_until_ready(engine_id)
        except TimeoutError as exc:
            WAKE_DURATION.labels(model=model).observe(time.monotonic() - t0)
            raise HTTPException(
                status_code=503,
                detail=f"Modell {model!r} konnte nicht rechtzeitig aufgeweckt werden: {exc}",
            )

        elapsed = time.monotonic() - t0
        WAKE_DURATION.labels(model=model).observe(elapsed)
        logger.info(
            "engine %r aufgewacht in %.1fs", engine_id, elapsed
        )

    return engine_id, True


# ──────────────────────────────────────────────────────────────────────────────
# HTTP-Forwarding (mit Streaming-Support)
# ──────────────────────────────────────────────────────────────────────────────

def _hop_by_hop_headers() -> set:
    """Headers that must not be forwarded to the downstream router."""
    return {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        *_INTERNAL_ROUTING_HEADERS,
    }


def _forward_headers(request: Request) -> Dict[str, str]:
    """Kopiert Client-Header, filtert Hop-by-Hop-Header heraus."""
    skip = _hop_by_hop_headers()
    return {k: v for k, v in request.headers.items() if k.lower() not in skip}


def _selected_model_from_headers(request: Request) -> str:
    """Return the semantic-router target model if the caller provided one."""
    return request.headers.get("x-selected-model", "").strip()


async def _stream_response(
    response: httpx.Response,
    client: httpx.AsyncClient,
    finalize: Optional[Callable[[], Awaitable[None]]] = None,
) -> AsyncIterator[bytes]:
    """Generator der Response-Bytes des Routers als Stream liefert."""
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        with suppress(Exception):
            await response.aclose()
        with suppress(Exception):
            await client.aclose()
        if finalize is not None:
            await finalize()


def _should_retry_after_wake(status_code: int) -> bool:
    """Return True for transient post-wake router responses."""
    return status_code in _POST_WAKE_RETRYABLE_STATUS_CODES


async def _sleep_before_post_wake_retry(
    model: str,
    status_code: Optional[int],
    attempt: int,
    total_attempts: int,
    *,
    exc: Optional[Exception] = None,
) -> None:
    """Log and wait between post-wake retries.

    The downstream router sometimes reports a short 503/502/504 window after an
    engine already answered ``is_sleeping=false``. Retrying here is cheaper than
    forcing clients or UIs to repeat the request manually.
    """
    message = (
        f"Transienter Downstream-Status {status_code} direkt nach Wake-up"
        if status_code is not None
        else f"Transienter Netzwerkfehler direkt nach Wake-up: {exc}"
    )
    logger.info(
        "%s für Modell %r – Retry %s/%s in %.1fs",
        message,
        model,
        attempt,
        total_attempts,
        settings.post_wake_retry_delay_seconds,
    )
    await asyncio.sleep(settings.post_wake_retry_delay_seconds)


async def _request_with_post_wake_retry(
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    *,
    model: str,
    woke_up: bool,
) -> Tuple[httpx.Response, int]:
    """Send a normal request and retry short post-wake startup windows."""
    max_retries = settings.post_wake_retry_attempts if woke_up else 0
    retries = 0

    async with httpx.AsyncClient(timeout=settings.forward_timeout_seconds) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    content=content,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                if attempt >= max_retries:
                    raise
                retries += 1
                await _sleep_before_post_wake_retry(
                    model,
                    None,
                    retries,
                    max_retries,
                    exc=exc,
                )
                continue

            if not woke_up or attempt >= max_retries or not _should_retry_after_wake(resp.status_code):
                return resp, retries

            retries += 1
            await _sleep_before_post_wake_retry(
                model,
                resp.status_code,
                retries,
                max_retries,
            )

    raise RuntimeError(f"unerreichbarer Zustand beim Forwarding von Modell {model!r}")


async def _open_stream_with_post_wake_retry(
    method: str,
    url: str,
    headers: Dict[str, str],
    content: bytes,
    *,
    model: str,
    woke_up: bool,
) -> Tuple[httpx.AsyncClient, httpx.Response, int]:
    """Open a downstream stream and retry transient post-wake failures."""
    max_retries = settings.post_wake_retry_attempts if woke_up else 0
    retries = 0
    client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds)

    try:
        for attempt in range(max_retries + 1):
            request = client.build_request(
                method,
                url,
                content=content,
                headers=headers,
            )
            try:
                resp = await client.send(request, stream=True)
            except httpx.RequestError as exc:
                if attempt >= max_retries:
                    raise
                retries += 1
                await _sleep_before_post_wake_retry(
                    model,
                    None,
                    retries,
                    max_retries,
                    exc=exc,
                )
                continue

            if not woke_up or attempt >= max_retries or not _should_retry_after_wake(resp.status_code):
                return client, resp, retries

            retries += 1
            await resp.aclose()
            await _sleep_before_post_wake_retry(
                model,
                resp.status_code,
                retries,
                max_retries,
            )
    except Exception:
        await client.aclose()
        raise

    await client.aclose()
    raise RuntimeError(f"unerreichbarer Zustand beim Stream-Forwarding von Modell {model!r}")


async def forward_request(request: Request, path: str) -> Response:
    """Leitet einen beliebigen Request transparent an den Router weiter.

    Für Inferenz-Endpunkte wird vorher ensure_awake() aufgerufen.
    Streaming (stream=True im Body oder Accept: text/event-stream) wird unterstützt.
    """
    url = f"{settings.router_url}/{path.lstrip('/')}"
    body: bytes = await request.body()
    headers = _forward_headers(request)
    started = time.monotonic()

    # Best-effort extraction keeps the proxy tolerant of non-inference routes
    # and older callers that may not send the exact OpenAI payload shape.
    model: str = ""
    original_model: str = ""
    selected_model: str = ""
    path_kind = request.headers.get("x-routing-path-kind", "").strip() or "direct"
    route_rule = request.headers.get("x-route-rule", "").strip()
    route_reason = request.headers.get("x-route-reason", "").strip()
    is_inference_path = any(
        path.rstrip("/").endswith(ep)
        for ep in ("/v1/completions", "/v1/chat/completions", "/completions", "/chat/completions")
    )
    stream_requested = False
    engine_id: Optional[str] = None
    node_name: Optional[str] = None
    tracked_engine = False
    tracked_node = False
    schedule_sleep = False
    woke_up = False

    if is_inference_path and body:
        try:
            payload: Dict[str, Any] = json.loads(body)
            original_model = str(payload.get("model", "") or "")
            selected_model = _selected_model_from_headers(request)
            stream_requested = bool(payload.get("stream") is True)

            if selected_model:
                if selected_model != original_model:
                    logger.info(
                        "semantic-router waehlt Modell %r fuer urspruengliches Modell %r",
                        selected_model,
                        original_model or "<leer>",
                    )
                payload = dict(payload)
                payload["model"] = selected_model
                body = json.dumps(payload).encode("utf-8")
                model = selected_model
            else:
                model = original_model
        except Exception:
            pass

    if model:
        REQUESTS_TOTAL.labels(model=model, status="started").inc()
        engine_id = await router_client.resolve_engine_id(model)
        if engine_id is not None:
            node_name = await _resolve_model_node(model)
            if node_name is not None:
                await _begin_node_request(node_name, engine_id, model)
                tracked_node = True
            await _begin_engine_request(engine_id)
            tracked_engine = True
        try:
            engine_id, woke_up = await ensure_awake(model, engine_id, node_name=node_name)
            schedule_sleep = engine_id is not None
        except HTTPException:
            _record_request(
                {
                    "timestamp": int(time.time()),
                    "path": path,
                    "requested_model": original_model or model,
                    "effective_model": model,
                    "path_kind": path_kind,
                    "route_rule": route_rule,
                    "route_reason": route_reason,
                        "node": node_name,
                        "engine_id": engine_id,
                        "status": "wake_failed",
                        "wake_retries": 0,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            if tracked_engine and engine_id is not None:
                await _end_engine_request(
                    engine_id,
                    model,
                    schedule_sleep=False,
                    node_name=node_name,
                )
            if tracked_node and node_name is not None and engine_id is not None:
                await _end_node_request(
                    node_name,
                    engine_id,
                    model,
                    schedule_sleep=False,
                )
            REQUESTS_TOTAL.labels(model=model, status="wake_failed").inc()
            raise
        except Exception:
            _record_request(
                {
                    "timestamp": int(time.time()),
                    "path": path,
                    "requested_model": original_model or model,
                    "effective_model": model,
                    "path_kind": path_kind,
                    "route_rule": route_rule,
                    "route_reason": route_reason,
                        "node": node_name,
                        "engine_id": engine_id,
                        "status": "wake_failed",
                        "wake_retries": 0,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            if tracked_engine and engine_id is not None:
                await _end_engine_request(
                    engine_id,
                    model,
                    schedule_sleep=False,
                    node_name=node_name,
                )
            if tracked_node and node_name is not None and engine_id is not None:
                await _end_node_request(
                    node_name,
                    engine_id,
                    model,
                    schedule_sleep=False,
                )
            REQUESTS_TOTAL.labels(model=model, status="wake_failed").inc()
            raise

    # ── Streaming-Response ──────────────────────────────────────────────────
    accept = request.headers.get("accept", "")
    if stream_requested or "text/event-stream" in accept:
        try:
            stream_client, stream_resp, wake_retries = await _open_stream_with_post_wake_retry(
                request.method,
                url,
                headers,
                body,
                model=model,
                woke_up=woke_up,
            )
        except httpx.RequestError as exc:
            if model:
                _record_request(
                    {
                        "timestamp": int(time.time()),
                        "path": path,
                        "requested_model": original_model or model,
                        "effective_model": model,
                        "path_kind": path_kind,
                        "route_rule": route_rule,
                        "route_reason": route_reason,
                        "node": node_name,
                        "engine_id": engine_id,
                        "status": "network_error",
                        "wake_retries": 0,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            if tracked_engine and engine_id is not None:
                await _end_engine_request(
                    engine_id,
                    model,
                    schedule_sleep=schedule_sleep,
                    node_name=node_name,
                )
            if tracked_node and node_name is not None and engine_id is not None:
                await _end_node_request(
                    node_name,
                    engine_id,
                    model,
                    schedule_sleep=schedule_sleep,
                )
            if model:
                REQUESTS_TOTAL.labels(model=model, status="network_error").inc()
            raise HTTPException(status_code=502, detail=f"Router nicht erreichbar: {exc}")

        skip = _hop_by_hop_headers()
        response_headers = {
            k: v for k, v in stream_resp.headers.items() if k.lower() not in skip
        }
        stream_status_code = stream_resp.status_code
        stream_media_type = stream_resp.headers.get("content-type")

        if stream_status_code >= 400:
            error_body = await stream_resp.aread()
            await stream_resp.aclose()
            await stream_client.aclose()
            if model:
                _record_request(
                    {
                        "timestamp": int(time.time()),
                        "path": path,
                        "requested_model": original_model or model,
                        "effective_model": model,
                        "path_kind": path_kind,
                        "route_rule": route_rule,
                        "route_reason": route_reason,
                        "node": node_name,
                        "engine_id": engine_id,
                        "status": "error",
                        "status_code": stream_status_code,
                        "wake_retries": wake_retries,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
                REQUESTS_TOTAL.labels(model=model, status="ok").inc()
                FORWARDED_TOTAL.labels(model=model, status_code=str(stream_status_code)).inc()
            if tracked_engine and engine_id is not None:
                await _end_engine_request(
                    engine_id,
                    model,
                    schedule_sleep=schedule_sleep,
                    node_name=node_name,
                )
            if tracked_node and node_name is not None and engine_id is not None:
                await _end_node_request(
                    node_name,
                    engine_id,
                    model,
                    schedule_sleep=schedule_sleep,
                )
            return Response(
                content=error_body,
                status_code=stream_status_code,
                headers=response_headers,
                media_type=stream_media_type,
            )

        async def finalize_stream() -> None:
            if model:
                _record_request(
                    {
                        "timestamp": int(time.time()),
                        "path": path,
                        "requested_model": original_model or model,
                        "effective_model": model,
                        "path_kind": path_kind,
                        "route_rule": route_rule,
                        "route_reason": route_reason,
                        "node": node_name,
                        "engine_id": engine_id,
                        "status": "streaming",
                        "status_code": stream_status_code,
                        "wake_retries": wake_retries,
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    }
                )
            if tracked_engine and engine_id is not None:
                await _end_engine_request(
                    engine_id,
                    model,
                    schedule_sleep=schedule_sleep,
                    node_name=node_name,
                )
            if tracked_node and node_name is not None and engine_id is not None:
                await _end_node_request(
                    node_name,
                    engine_id,
                    model,
                    schedule_sleep=schedule_sleep,
                )

        return StreamingResponse(
            _stream_response(
                stream_resp,
                stream_client,
                finalize=finalize_stream,
            ),
            status_code=stream_status_code,
            media_type=stream_media_type,
            headers=response_headers,
        )

    # ── Normaler (nicht-streaming) Request ─────────────────────────────────
    try:
        resp, wake_retries = await _request_with_post_wake_retry(
            request.method,
            url,
            headers,
            body,
            model=model,
            woke_up=woke_up,
        )
    except httpx.RequestError as exc:
        if model:
            _record_request(
                {
                    "timestamp": int(time.time()),
                    "path": path,
                    "requested_model": original_model or model,
                    "effective_model": model,
                    "path_kind": path_kind,
                    "route_rule": route_rule,
                    "route_reason": route_reason,
                    "node": node_name,
                    "engine_id": engine_id,
                    "status": "network_error",
                    "wake_retries": 0,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        if tracked_engine and engine_id is not None:
            await _end_engine_request(
                engine_id,
                model,
                schedule_sleep=schedule_sleep,
                node_name=node_name,
            )
        if tracked_node and node_name is not None and engine_id is not None:
            await _end_node_request(
                node_name,
                engine_id,
                model,
                schedule_sleep=schedule_sleep,
            )
        if model:
            REQUESTS_TOTAL.labels(model=model, status="network_error").inc()
        raise HTTPException(status_code=502, detail=f"Router nicht erreichbar: {exc}")

    # Hop-by-Hop-Header aus der Router-Antwort entfernen
    skip = _hop_by_hop_headers()
    response_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in skip
    }

    status_code = resp.status_code
    if model:
        _record_request(
            {
                "timestamp": int(time.time()),
                "path": path,
                "requested_model": original_model or model,
                "effective_model": model,
                "path_kind": path_kind,
                "route_rule": route_rule,
                "route_reason": route_reason,
                "node": node_name,
                "engine_id": engine_id,
                "status": "ok" if status_code < 400 else "error",
                "status_code": status_code,
                "wake_retries": wake_retries,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        REQUESTS_TOTAL.labels(model=model, status="ok").inc()
        FORWARDED_TOTAL.labels(model=model, status_code=str(status_code)).inc()
        if tracked_engine and engine_id is not None:
            await _end_engine_request(
                engine_id,
                model,
                schedule_sleep=schedule_sleep,
                node_name=node_name,
            )
        if tracked_node and node_name is not None and engine_id is not None:
            await _end_node_request(
                node_name,
                engine_id,
                model,
                schedule_sleep=schedule_sleep,
            )

    return Response(
        content=resp.content,
        status_code=status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )

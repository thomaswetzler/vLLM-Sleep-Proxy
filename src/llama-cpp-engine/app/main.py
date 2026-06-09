"""FastAPI wrapper that adds wake/sleep behavior around llama-server."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import settings
from . import process_manager

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "llama-cpp-engine gestartet | alias=%s | file=%s | node=%s",
        settings.model_alias,
        settings.model_file,
        settings.node_name or "-",
    )
    yield
    await process_manager.stop()
    logger.info("llama-cpp-engine beendet")


app = FastAPI(
    title="llama.cpp Engine Wrapper",
    version="0.1.0",
    description="Wake/Sleep wrapper around a local llama-server child process.",
    lifespan=lifespan,
)


def _child_base_url() -> str:
    return f"http://{settings.llama_server_host}:{settings.llama_server_port}"


def _child_url(path: str, request: Request) -> str:
    query_string = str(request.url.query or "").strip()
    suffix = f"?{query_string}" if query_string else ""
    return f"{_child_base_url()}/{path.lstrip('/')}{suffix}"


def _hop_by_hop_headers() -> set[str]:
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
    }


def _forward_headers(request: Request) -> Dict[str, str]:
    skip = _hop_by_hop_headers()
    return {k: v for k, v in request.headers.items() if k.lower() not in skip}


def _synthetic_models_payload() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": settings.model_alias,
                "object": "model",
                "created": 0,
                "owned_by": "llamacpp",
            }
        ],
    }


async def _stream_response(
    response: httpx.Response,
    client: httpx.AsyncClient,
    finalize: Optional[Callable[[], Awaitable[None]]] = None,
) -> AsyncIterator[bytes]:
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


async def _proxy_to_child(request: Request, path: str) -> Response:
    if process_manager.is_sleeping():
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": 503,
                    "message": "Model is sleeping",
                    "type": "unavailable_error",
                }
            },
        )

    url = _child_url(path, request)
    body = await request.body()
    headers = _forward_headers(request)
    accept = request.headers.get("accept", "")

    stream_requested = False
    if body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                stream_requested = bool(payload.get("stream") is True)
        except Exception:
            pass

    if stream_requested or "text/event-stream" in accept:
        client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds)
        try:
            downstream_request = client.build_request(
                request.method,
                url,
                headers=headers,
                content=body,
            )
            response = await client.send(downstream_request, stream=True)
        except Exception:
            await client.aclose()
            raise

        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _hop_by_hop_headers()
        }
        return StreamingResponse(
            _stream_response(response, client),
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
            headers=response_headers,
        )

    async with httpx.AsyncClient(timeout=settings.forward_timeout_seconds) as client:
        response = await client.request(
            request.method,
            url,
            headers=headers,
            content=body,
        )

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _hop_by_hop_headers()
    }
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.headers.get("content-type"),
    )


@app.get("/health", tags=["ops"])
async def health() -> Dict[str, Any]:
    """Wrapper liveness/readiness endpoint.

    The wrapper itself stays ready even while the child model is sleeping so the
    sleep-proxy can still reach `/wake_up`.
    """
    return {
        "status": "ok",
        "is_sleeping": process_manager.is_sleeping(),
        "model": settings.model_alias,
    }


@app.get("/is_sleeping", tags=["ops"])
async def is_sleeping() -> Dict[str, bool]:
    return {"is_sleeping": process_manager.is_sleeping()}


@app.post("/wake_up", tags=["ops"])
async def wake_up() -> Dict[str, str]:
    await process_manager.ensure_started()
    return {"status": "success"}


@app.post("/sleep", tags=["ops"])
async def sleep() -> Dict[str, str]:
    await process_manager.stop()
    return {"status": "success"}


@app.get("/v1/models", tags=["models"])
async def v1_models() -> JSONResponse:
    if process_manager.is_sleeping():
        return JSONResponse(content=_synthetic_models_payload())

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{_child_base_url()}/v1/models")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return JSONResponse(content=payload)
    except Exception as exc:
        logger.warning("Downstream /v1/models fehlgeschlagen, nutze Fallback: %s", exc)

    return JSONResponse(content=_synthetic_models_payload())


@app.get("/models", tags=["models"])
async def models_alias() -> JSONResponse:
    return await v1_models()


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def catch_all(request: Request, path: str) -> Response:
    return await _proxy_to_child(request, path)

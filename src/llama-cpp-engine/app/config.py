"""Environment-backed settings for the llama.cpp engine wrapper."""

from __future__ import annotations

import json
import os
from typing import List, Optional


def _as_bool(value: str, *, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def _as_optional_int(value: str) -> Optional[int]:
    stripped = str(value or "").strip()
    if not stripped:
        return None
    return int(stripped)


def _as_json_list(value: str) -> List[str]:
    stripped = str(value or "").strip()
    if not stripped:
        return []
    payload = json.loads(stripped)
    if not isinstance(payload, list):
        raise ValueError(f"expected JSON list, got {type(payload)!r}")
    return [str(item) for item in payload if str(item or "").strip()]


class _Settings:
    """Small settings container for the wrapper and child llama-server."""

    model_alias: str = os.getenv("ENGINE_MODEL_ALIAS", "").strip()
    model_file: str = os.getenv("ENGINE_MODEL_FILE", "").strip()
    model_path: str = os.getenv("ENGINE_MODEL_PATH", "").strip()
    node_name: str = os.getenv("ENGINE_NODE_NAME", "").strip()

    log_level: str = os.getenv("LOG_LEVEL", "info").upper()
    forward_timeout_seconds: int = int(os.getenv("FORWARD_TIMEOUT_SECONDS", "300"))
    startup_timeout_seconds: int = int(os.getenv("LLAMA_SERVER_STARTUP_TIMEOUT_SECONDS", "240"))
    shutdown_timeout_seconds: int = int(os.getenv("LLAMA_SERVER_SHUTDOWN_TIMEOUT_SECONDS", "30"))

    llama_server_bin: str = os.getenv("LLAMA_SERVER_BIN", "/app/llama-server").strip()
    llama_server_host: str = os.getenv("LLAMA_SERVER_LOCAL_HOST", "127.0.0.1").strip()
    llama_server_port: int = int(os.getenv("LLAMA_SERVER_LOCAL_PORT", "8081"))

    ctx_size: Optional[int] = _as_optional_int(os.getenv("LLAMA_ARG_CTX_SIZE", ""))
    n_gpu_layers: Optional[int] = _as_optional_int(os.getenv("LLAMA_ARG_N_GPU_LAYERS", ""))
    n_parallel: Optional[int] = _as_optional_int(os.getenv("LLAMA_ARG_N_PARALLEL", ""))
    threads: Optional[int] = _as_optional_int(os.getenv("LLAMA_ARG_THREADS", ""))
    mmproj: str = os.getenv("LLAMA_ARG_MM_PROJ", "").strip()
    flash_attention: bool = _as_bool(os.getenv("LLAMA_ARG_FLASH_ATTN", ""), default=False)
    jinja: bool = _as_bool(os.getenv("LLAMA_ARG_JINJA", ""), default=False)
    endpoint_metrics: bool = _as_bool(os.getenv("LLAMA_ARG_ENDPOINT_METRICS", "1"), default=True)
    extra_args: List[str] = _as_json_list(os.getenv("LLAMA_SERVER_EXTRA_ARGS_JSON", "[]"))


settings = _Settings()

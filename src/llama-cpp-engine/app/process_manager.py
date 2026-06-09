"""Lifecycle management for the child llama-server process."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from typing import List, Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_process_lock = asyncio.Lock()
_process: Optional[subprocess.Popen[bytes]] = None


def _child_base_url() -> str:
    return f"http://{settings.llama_server_host}:{settings.llama_server_port}"


def _refresh_process_reference() -> Optional[subprocess.Popen[bytes]]:
    global _process
    if _process is not None and _process.poll() is not None:
        logger.warning("llama-server wurde unerwartet beendet mit Code %s", _process.returncode)
        _process = None
    return _process


def is_running() -> bool:
    """Return True when the child llama-server process is still alive."""
    return _refresh_process_reference() is not None


def is_sleeping() -> bool:
    """Expose a vLLM-like sleep state for the wrapper."""
    return not is_running()


def build_command() -> List[str]:
    """Build the child llama-server command from environment settings."""
    command = [
        settings.llama_server_bin,
        "-m",
        settings.model_file,
        "--host",
        settings.llama_server_host,
        "--port",
        str(settings.llama_server_port),
        "--alias",
        settings.model_alias,
    ]

    if settings.ctx_size is not None:
        command.extend(["-c", str(settings.ctx_size)])
    if settings.n_gpu_layers is not None:
        command.extend(["--n-gpu-layers", str(settings.n_gpu_layers)])
    if settings.n_parallel is not None:
        command.extend(["-np", str(settings.n_parallel)])
    if settings.threads is not None:
        command.extend(["-t", str(settings.threads)])
    if settings.flash_attention:
        command.extend(["-fa", "on"])
    if settings.jinja:
        command.append("--jinja")
    if settings.endpoint_metrics:
        command.append("--endpoint-metrics")
    if settings.mmproj:
        command.extend(["--mmproj", settings.mmproj])
    command.extend(settings.extra_args)
    return command


async def _wait_until_ready() -> None:
    """Poll the child server health endpoint until it is actually ready."""
    deadline = time.monotonic() + settings.startup_timeout_seconds
    last_error: Optional[str] = None

    while time.monotonic() < deadline:
        if not is_running():
            raise RuntimeError("llama-server wurde vorzeitig beendet")

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{_child_base_url()}/health")
            if response.status_code == 200:
                return
            if response.status_code == 503:
                last_error = "Modell wird noch geladen"
            else:
                last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)

        await asyncio.sleep(1)

    raise TimeoutError(last_error or "llama-server wurde nicht rechtzeitig bereit")


async def ensure_started() -> None:
    """Start the child process if needed and wait until it is ready."""
    global _process

    async with _process_lock:
        if is_running():
            await _wait_until_ready()
            return

        command = build_command()
        logger.info("Starte llama-server: %s", " ".join(command))
        _process = subprocess.Popen(command)

    try:
        await _wait_until_ready()
    except Exception:
        await stop()
        raise


async def stop() -> None:
    """Terminate the child process and wait for a clean shutdown."""
    global _process

    async with _process_lock:
        process = _refresh_process_reference()
        if process is None:
            _process = None
            return

        process.terminate()
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, process.wait), timeout=settings.shutdown_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("llama-server reagiert nicht auf SIGTERM, sende SIGKILL")
            process.kill()
            await loop.run_in_executor(None, process.wait)
        finally:
            _process = None

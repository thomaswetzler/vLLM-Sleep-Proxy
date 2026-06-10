#!/usr/bin/env python3
"""Smoke-test LiteLLM models and verify wake/sleep for GPU-backed runtimes."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_TEXT_MODELS = [
    "local/gemma-3-4b-it",
    "local/llama-3.1-8b-instruct",
    "local/qwen2.5-coder-7b-instruct",
    "local/qwen2.5-14b-instruct",
]
DEFAULT_VISION_MODELS = [
    "local/qwen2.5-vl-7b-instruct",
]
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"
DEFAULT_WHISPER_MODEL = "whisper-large-v3"
DEFAULT_ALL_MODELS = [
    *DEFAULT_TEXT_MODELS,
    *DEFAULT_VISION_MODELS,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_WHISPER_MODEL,
]
DEFAULT_VISION_IMAGE_URL = (
    "https://huggingface.co/datasets/huggingface/documentation-images/resolve/main/cats.png"
)


class TestFailure(RuntimeError):
    """Raised when one model fails a smoke test."""


@dataclass(frozen=True)
class ControlTarget:
    """Backend control endpoint used for wake/sleep verification."""

    model: str
    runtime: str
    kind: str
    router_engine_id: Optional[str] = None
    service_name: Optional[str] = None
    service_port: Optional[int] = None


class PortForward:
    """Minimal kubectl port-forward lifecycle helper."""

    def __init__(
        self,
        *,
        kubectl: str,
        namespace: str,
        service_name: str,
        remote_port: int,
    ) -> None:
        self.kubectl = kubectl
        self.namespace = namespace
        self.service_name = service_name
        self.remote_port = remote_port
        self.local_port = self._allocate_local_port()
        self.process: Optional[subprocess.Popen[str]] = None

    @staticmethod
    def _allocate_local_port() -> int:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        return port

    def start(self) -> "PortForward":
        command = [
            self.kubectl,
            "-n",
            self.namespace,
            "port-forward",
            f"svc/{self.service_name}",
            f"{self.local_port}:{self.remote_port}",
        ]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise TestFailure(
                    f"kubectl port-forward für svc/{self.service_name} wurde vorzeitig beendet"
                )
            if self._port_is_open():
                return self
            time.sleep(0.5)
        self.stop()
        raise TestFailure(
            f"kubectl port-forward für svc/{self.service_name} wurde nicht rechtzeitig bereit"
        )

    def _port_is_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.local_port), timeout=1):
                return True
        except OSError:
            return False

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def __enter__(self) -> "PortForward":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def fetch_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 180,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    if headers:
        for key, value in headers.items():
            request.add_header(key, value)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: int = 180,
) -> str:
    request = urllib.request.Request(url, data=body, method=method)
    if headers:
        for key, value in headers.items():
            request.add_header(key, value)
    if content_type:
        request.add_header("Content-Type", content_type)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def post_multipart(
    url: str,
    *,
    fields: Dict[str, str],
    file_field: str,
    file_path: str,
    file_content_type: str,
    headers: Dict[str, str],
    timeout: int = 180,
) -> Any:
    boundary = f"----codex-{uuid.uuid4().hex}"
    chunks: List[bytes] = []

    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    filename = os.path.basename(file_path)
    with open(file_path, "rb") as handle:
        file_bytes = handle.read()

    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {file_content_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )

    body = b"".join(chunks)
    response_text = fetch_text(
        url,
        method="POST",
        headers=headers,
        body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        timeout=timeout,
    )
    return json.loads(response_text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-url", required=True)
    parser.add_argument("--completions-url", required=True)
    parser.add_argument("--embeddings-url", required=True)
    parser.add_argument("--transcriptions-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--core-release", default="vllm")
    parser.add_argument("--router-service", default="vllm-router-service")
    parser.add_argument("--router-port", type=int, default=80)
    parser.add_argument("--test-audio", required=True)
    parser.add_argument("--test-model", default="all")
    parser.add_argument("--text-prompt", default="Reply with exactly OK.")
    parser.add_argument(
        "--vision-prompt",
        default="What animals are shown in the image? Reply with one short sentence.",
    )
    parser.add_argument("--vision-image-url", default=DEFAULT_VISION_IMAGE_URL)
    parser.add_argument("--max-tokens", type=int, default=120)
    parser.add_argument("--sleep-level", type=int, default=1)
    parser.add_argument("--awake-timeout", type=int, default=20)
    parser.add_argument("--sleep-timeout", type=int, default=90)
    parser.add_argument("--sleep-poll-interval", type=float, default=2.0)
    return parser.parse_args()


def bearer_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def normalize_test_models(test_model: str) -> List[str]:
    value = test_model.strip()
    if not value or value.lower() == "all":
        return list(DEFAULT_ALL_MODELS)
    return [item.strip() for item in value.split(",") if item.strip()]


def load_models(models_url: str, api_key: str) -> List[str]:
    payload = fetch_json(models_url, headers=bearer_headers(api_key), timeout=60)
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [
            str(item.get("id"))
            for item in payload["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    if isinstance(payload, list):
        return [str(item) for item in payload if isinstance(item, str)]
    raise TestFailure(f"Unerwartetes /v1/models-Payload: {payload!r}")


def classify_model(model: str) -> str:
    if model == DEFAULT_EMBEDDING_MODEL:
        return "embedding"
    if model == DEFAULT_WHISPER_MODEL:
        return "whisper"
    if model in DEFAULT_VISION_MODELS:
        return "vision"
    return "text"


def load_stack_values(helm: str, release: str, namespace: str) -> Dict[str, Any]:
    try:
        raw = subprocess.check_output(
            [helm, "get", "values", release, "-n", namespace, "-o", "json"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def load_direct_control_targets(stack_values: Dict[str, Any]) -> Dict[str, ControlTarget]:
    result: Dict[str, ControlTarget] = {}
    proxy_values = stack_values.get("sleep-proxy")
    if not isinstance(proxy_values, dict):
        return result
    env_values = proxy_values.get("env")
    if not isinstance(env_values, dict):
        return result
    engine_catalog = env_values.get("engineCatalog")
    if not isinstance(engine_catalog, list):
        return result

    for entry in engine_catalog:
        if not isinstance(entry, dict):
            continue
        model = entry.get("model")
        endpoint = entry.get("endpoint")
        runtime = entry.get("runtime")
        if not isinstance(model, str) or not isinstance(endpoint, str) or not model or not endpoint:
            continue
        parsed = urllib.parse.urlparse(endpoint)
        if not parsed.hostname:
            continue
        service_name = parsed.hostname.split(".", 1)[0]
        service_port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        result[model] = ControlTarget(
            model=model,
            runtime=str(runtime or "direct"),
            kind="direct",
            service_name=service_name,
            service_port=service_port,
        )
    return result


def load_router_targets(router_base_url: str) -> Dict[str, ControlTarget]:
    payload = fetch_json(f"{router_base_url}/engines", timeout=30)
    result: Dict[str, ControlTarget] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            engine_id = item.get("engine_id")
            if not isinstance(engine_id, str) or not engine_id:
                continue
            models = item.get("serving_models")
            if not isinstance(models, list):
                continue
            for model in models:
                if isinstance(model, str) and model:
                    result.setdefault(
                        model,
                        ControlTarget(
                            model=model,
                            runtime="vllm",
                            kind="router",
                            router_engine_id=engine_id,
                        ),
                    )
    elif isinstance(payload, dict):
        for engine_id, item in payload.items():
            if not isinstance(item, dict):
                continue
            model = item.get("model_url") or item.get("model") or item.get("model_name")
            if isinstance(model, str) and model and isinstance(engine_id, str) and engine_id:
                result.setdefault(
                    model,
                    ControlTarget(
                        model=model,
                        runtime="vllm",
                        kind="router",
                        router_engine_id=engine_id,
                    ),
                )
    return result


def resolve_control_target(
    model: str,
    *,
    router_targets: Dict[str, ControlTarget],
    direct_targets: Dict[str, ControlTarget],
) -> Optional[ControlTarget]:
    if model in direct_targets:
        return direct_targets[model]
    if model in router_targets:
        return router_targets[model]

    basename = model.rstrip("/").split("/")[-1]
    if basename:
        for candidate_model, target in router_targets.items():
            candidate_basename = candidate_model.rstrip("/").split("/")[-1]
            if basename == candidate_basename:
                return target
    return None


def wait_for_sleep_state(
    target: ControlTarget,
    *,
    expected_sleeping: bool,
    router_base_url: Optional[str],
    direct_base_url: Optional[str],
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current = control_is_sleeping(
            target,
            router_base_url=router_base_url,
            direct_base_url=direct_base_url,
        )
        if current == expected_sleeping:
            return True
        time.sleep(poll_interval_seconds)
    return False


def control_is_sleeping(
    target: ControlTarget,
    *,
    router_base_url: Optional[str],
    direct_base_url: Optional[str],
) -> bool:
    if target.kind == "router":
        if not router_base_url or not target.router_engine_id:
            raise TestFailure(f"Kein Router-Controlpfad für Modell {target.model!r} verfügbar")
        payload = fetch_json(
            f"{router_base_url}/is_sleeping?id={urllib.parse.quote(target.router_engine_id, safe='')}",
            timeout=30,
        )
    else:
        if not direct_base_url:
            raise TestFailure(f"Kein Direct-Controlpfad für Modell {target.model!r} verfügbar")
        payload = fetch_json(f"{direct_base_url}/is_sleeping", timeout=30)
    if isinstance(payload, dict) and isinstance(payload.get("is_sleeping"), bool):
        return bool(payload["is_sleeping"])
    raise TestFailure(f"Unerwartetes is_sleeping-Payload für {target.model!r}: {payload!r}")


def control_sleep(
    target: ControlTarget,
    *,
    sleep_level: int,
    router_base_url: Optional[str],
    direct_base_url: Optional[str],
) -> None:
    if target.kind == "router":
        if not router_base_url or not target.router_engine_id:
            raise TestFailure(f"Kein Router-Controlpfad für Modell {target.model!r} verfügbar")
        fetch_json(
            (
                f"{router_base_url}/sleep?id="
                f"{urllib.parse.quote(target.router_engine_id, safe='')}"
                f"&level={sleep_level}"
            ),
            method="POST",
            timeout=120,
        )
        return
    if not direct_base_url:
        raise TestFailure(f"Kein Direct-Controlpfad für Modell {target.model!r} verfügbar")
    fetch_json(
        f"{direct_base_url}/sleep?level={sleep_level}",
        method="POST",
        timeout=120,
    )


def extract_chat_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise TestFailure(f"Unerwartete Chat-Antwort: {payload!r}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TestFailure(f"Chat-Antwort ohne choices: {payload!r}")
    first = choices[0]
    if not isinstance(first, dict):
        raise TestFailure(f"Chat-Antwort mit ungueltiger choice: {payload!r}")
    message = first.get("message")
    if not isinstance(message, dict):
        raise TestFailure(f"Chat-Antwort ohne message: {payload!r}")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise TestFailure(f"Chat-Antwort ohne Textinhalt: {payload!r}")
    return content.strip()


def ensure_sleeping_baseline(
    model: str,
    *,
    target: ControlTarget,
    router_base_url: Optional[str],
    direct_base_url: Optional[str],
    sleep_level: int,
    sleep_timeout: int,
    sleep_poll_interval: float,
) -> None:
    control_sleep(
        target,
        sleep_level=sleep_level,
        router_base_url=router_base_url,
        direct_base_url=direct_base_url,
    )
    if not wait_for_sleep_state(
        target,
        expected_sleeping=True,
        router_base_url=router_base_url,
        direct_base_url=direct_base_url,
        timeout_seconds=sleep_timeout,
        poll_interval_seconds=sleep_poll_interval,
    ):
        raise TestFailure(f"Modell {model!r} wurde vor dem Test nicht schlafend")


def assert_awake_then_sleep(
    model: str,
    *,
    target: ControlTarget,
    router_base_url: Optional[str],
    direct_base_url: Optional[str],
    awake_timeout: int,
    sleep_timeout: int,
    sleep_poll_interval: float,
) -> None:
    if not wait_for_sleep_state(
        target,
        expected_sleeping=False,
        router_base_url=router_base_url,
        direct_base_url=direct_base_url,
        timeout_seconds=awake_timeout,
        poll_interval_seconds=0.5,
    ):
        raise TestFailure(f"Modell {model!r} ist nach dem Request nicht als wach beobachtbar")
    if not wait_for_sleep_state(
        target,
        expected_sleeping=True,
        router_base_url=router_base_url,
        direct_base_url=direct_base_url,
        timeout_seconds=sleep_timeout,
        poll_interval_seconds=sleep_poll_interval,
    ):
        raise TestFailure(f"Modell {model!r} ist nach dem Request nicht wieder eingeschlafen")


def test_text_model(
    model: str,
    *,
    completions_url: str,
    api_key: str,
    text_prompt: str,
    max_tokens: int,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": text_prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    response = fetch_json(
        completions_url,
        method="POST",
        headers=bearer_headers(api_key),
        payload=payload,
        timeout=300,
    )
    return extract_chat_content(response)


def test_vision_model(
    model: str,
    *,
    completions_url: str,
    api_key: str,
    vision_prompt: str,
    vision_image_url: str,
    max_tokens: int,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {"type": "image_url", "image_url": {"url": vision_image_url}},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    response = fetch_json(
        completions_url,
        method="POST",
        headers=bearer_headers(api_key),
        payload=payload,
        timeout=300,
    )
    return extract_chat_content(response)


def test_embedding_model(
    model: str,
    *,
    embeddings_url: str,
    api_key: str,
) -> str:
    payload = {"input": "Hello world", "model": model}
    response = fetch_json(
        embeddings_url,
        method="POST",
        headers=bearer_headers(api_key),
        payload=payload,
        timeout=180,
    )
    if not isinstance(response, dict):
        raise TestFailure(f"Unerwartete Embedding-Antwort: {response!r}")
    data = response.get("data")
    if not isinstance(data, list) or not data:
        raise TestFailure(f"Embedding-Antwort ohne data: {response!r}")
    item = data[0]
    if not isinstance(item, dict):
        raise TestFailure(f"Embedding-Antwort mit ungueltigem data-Eintrag: {response!r}")
    embedding = item.get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise TestFailure(f"Embedding-Antwort ohne Vektor: {response!r}")
    return f"embedding_dim={len(embedding)}"


def test_whisper_model(
    model: str,
    *,
    transcriptions_url: str,
    api_key: str,
    audio_path: str,
) -> str:
    if not os.path.isfile(audio_path):
        raise TestFailure(f"Audio-Datei nicht gefunden: {audio_path}")
    response = post_multipart(
        transcriptions_url,
        fields={"model": model},
        file_field="file",
        file_path=audio_path,
        file_content_type="audio/mpeg",
        headers=bearer_headers(api_key),
        timeout=240,
    )
    if not isinstance(response, dict):
        raise TestFailure(f"Unerwartete Whisper-Antwort: {response!r}")
    text = response.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TestFailure(f"Whisper-Antwort ohne Text: {response!r}")
    return text.strip()


def run_one_model(
    model: str,
    *,
    args: argparse.Namespace,
    control_target: Optional[ControlTarget],
    router_base_url: Optional[str],
    direct_base_url: Optional[str],
) -> str:
    model_type = classify_model(model)
    if model_type == "text":
        pass
    elif model_type == "vision":
        pass
    elif model_type == "embedding":
        return test_embedding_model(
            model,
            embeddings_url=args.embeddings_url,
            api_key=args.api_key,
        )
    elif model_type == "whisper":
        return test_whisper_model(
            model,
            transcriptions_url=args.transcriptions_url,
            api_key=args.api_key,
            audio_path=args.test_audio,
        )
    else:
        raise TestFailure(f"Unbekannter Modelltyp für {model!r}")

    if control_target is None:
        raise TestFailure(f"Kein GPU-Controltarget für Modell {model!r} gefunden")

    ensure_sleeping_baseline(
        model,
        target=control_target,
        router_base_url=router_base_url,
        direct_base_url=direct_base_url,
        sleep_level=args.sleep_level,
        sleep_timeout=args.sleep_timeout,
        sleep_poll_interval=args.sleep_poll_interval,
    )
    if model_type == "text":
        result = test_text_model(
            model,
            completions_url=args.completions_url,
            api_key=args.api_key,
            text_prompt=args.text_prompt,
            max_tokens=args.max_tokens,
        )
    else:
        result = test_vision_model(
            model,
            completions_url=args.completions_url,
            api_key=args.api_key,
            vision_prompt=args.vision_prompt,
            vision_image_url=args.vision_image_url,
            max_tokens=args.max_tokens,
        )
    assert_awake_then_sleep(
        model,
        target=control_target,
        router_base_url=router_base_url,
        direct_base_url=direct_base_url,
        awake_timeout=args.awake_timeout,
        sleep_timeout=args.sleep_timeout,
        sleep_poll_interval=args.sleep_poll_interval,
    )
    return result


def main() -> int:
    args = parse_args()
    target_models = normalize_test_models(args.test_model)
    available_models = load_models(args.models_url, args.api_key)
    missing = [model for model in target_models if model not in available_models]
    if missing:
        print(
            "Fehlende Modelle in LiteLLM /v1/models: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    stack_values = load_stack_values(args.helm, args.core_release, args.namespace)
    direct_targets = load_direct_control_targets(stack_values)
    gpu_models = [
        model
        for model in target_models
        if classify_model(model) in {"text", "vision"}
    ]

    results: List[Tuple[str, str, str]] = []
    router_base_url: Optional[str] = None
    router_targets: Dict[str, ControlTarget] = {}

    with ExitStack() as stack:
        if any(model not in direct_targets for model in gpu_models):
            router_pf = stack.enter_context(
                PortForward(
                    kubectl=args.kubectl,
                    namespace=args.namespace,
                    service_name=args.router_service,
                    remote_port=args.router_port,
                )
            )
            router_base_url = f"http://127.0.0.1:{router_pf.local_port}"
            router_targets = load_router_targets(router_base_url)

        direct_pf_cache: Dict[str, Tuple[PortForward, str]] = {}

        for model in target_models:
            direct_base_url: Optional[str] = None
            control_target = resolve_control_target(
                model,
                router_targets=router_targets,
                direct_targets=direct_targets,
            )

            if control_target is not None and control_target.kind == "direct":
                if not control_target.service_name or not control_target.service_port:
                    raise TestFailure(f"Direct-Controltarget für {model!r} ist unvollständig")
                cached = direct_pf_cache.get(control_target.service_name)
                if cached is None:
                    pf = stack.enter_context(
                        PortForward(
                            kubectl=args.kubectl,
                            namespace=args.namespace,
                            service_name=control_target.service_name,
                            remote_port=control_target.service_port,
                        )
                    )
                    direct_base_url = f"http://127.0.0.1:{pf.local_port}"
                    direct_pf_cache[control_target.service_name] = (pf, direct_base_url)
                else:
                    direct_base_url = cached[1]

            try:
                print(f"== Teste {model} ==")
                result = run_one_model(
                    model,
                    args=args,
                    control_target=control_target,
                    router_base_url=router_base_url,
                    direct_base_url=direct_base_url,
                )
                results.append((model, "OK", result))
                print(f"OK  {model} -> {result}")
            except Exception as exc:
                results.append((model, "FAIL", str(exc)))
                print(f"FAIL {model} -> {exc}", file=sys.stderr)

    failed = [item for item in results if item[1] != "OK"]
    print("\nZusammenfassung:")
    for model, status, detail in results:
        print(f"  {status:<4} {model:<34} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

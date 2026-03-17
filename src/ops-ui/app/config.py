"""Configuration for the standalone ops UI."""

import os
from pathlib import Path


class _Settings:
    """Environment-backed settings for the aggregation layer."""

    kubernetes_api_url: str = os.getenv(
        "KUBERNETES_API_URL",
        "https://kubernetes.default.svc",
    ).rstrip("/")
    kubernetes_service_account_token_path: str = os.getenv(
        "KUBERNETES_SERVICE_ACCOUNT_TOKEN_PATH",
        "/var/run/secrets/kubernetes.io/serviceaccount/token",
    )
    kubernetes_service_account_ca_path: str = os.getenv(
        "KUBERNETES_SERVICE_ACCOUNT_CA_PATH",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )

    sleep_proxy_url: str = os.getenv(
        "SLEEP_PROXY_URL",
        "http://sleep-proxy-service.vllm.svc.cluster.local:8080",
    ).rstrip("/")
    vllm_router_url: str = os.getenv(
        "VLLM_ROUTER_URL",
        "http://vllm-router-service.vllm.svc.cluster.local:80",
    ).rstrip("/")
    litellm_url: str = os.getenv("LITELLM_URL", "").rstrip("/")
    ops_ui_url: str = os.getenv(
        "OPS_UI_URL",
        "http://ops-ui-service.vllm.svc.cluster.local:8080",
    ).rstrip("/")
    playground_url: str = os.getenv("PLAYGROUND_URL", "").rstrip("/")
    embeddings_url: str = os.getenv(
        "EMBEDDINGS_URL",
        "http://vllm-baai-bge-large-en-v15-cpu.vllm.svc.cluster.local:3000",
    ).rstrip("/")
    whisper_url: str = os.getenv(
        "WHISPER_URL",
        "http://vllm-whisper-cpu-service.vllm.svc.cluster.local",
    ).rstrip("/")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    refresh_interval_seconds: float = float(os.getenv("REFRESH_INTERVAL_SECONDS", "5"))
    log_level: str = os.getenv("LOG_LEVEL", "info").upper()

    @property
    def kubernetes_token(self) -> str:
        """Read the in-cluster service account token on demand."""
        path = Path(self.kubernetes_service_account_token_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()

    @property
    def kubernetes_ca_path(self) -> str | bool:
        """Return a CA bundle path when the in-cluster file exists."""
        path = Path(self.kubernetes_service_account_ca_path)
        return str(path) if path.exists() else True


settings = _Settings()

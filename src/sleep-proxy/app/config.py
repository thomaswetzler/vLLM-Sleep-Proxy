"""Configuration for the sleep-proxy runtime.

The Helm chart maps these values from `values.yaml` into environment variables.
Keeping the translation here flat makes the runtime easy to reason about from
logs and from `kubectl describe`.
"""
import os


class _Settings:
    """Simple environment-backed settings container.

    The proxy is intentionally lightweight, so a full config framework would add
    more moving parts than value. Group related settings here to keep the wake,
    forward, Kubernetes and auto-sleep behavior easy to trace.
    """

    router_url: str = os.getenv("ROUTER_URL", "http://vllm-router-service:80").rstrip("/")
    wake_timeout_seconds: int = int(os.getenv("WAKE_TIMEOUT_SECONDS", "120"))
    poll_interval_seconds: float = float(os.getenv("POLL_INTERVAL_SECONDS", "2"))
    engine_cache_ttl_seconds: float = float(os.getenv("ENGINE_CACHE_TTL_SECONDS", "30"))
    model_node_cache_ttl_seconds: float = float(os.getenv("MODEL_NODE_CACHE_TTL_SECONDS", "30"))
    log_level: str = os.getenv("LOG_LEVEL", "info").upper()
    # Forwarding covers the full client request lifetime, including wake-up time.
    forward_timeout_seconds: int = int(os.getenv("FORWARD_TIMEOUT_SECONDS", "300"))
    # Router control calls like wake_up()/sleep() can legitimately take longer
    # than a small health/read timeout, especially on the larger Gemma lane.
    router_action_timeout_seconds: int = int(
        os.getenv("ROUTER_ACTION_TIMEOUT_SECONDS", "120")
    )
    # Some engines briefly return 503 after wake_up() before they accept the
    # first real inference request. Retry those transient startup windows here.
    post_wake_retry_attempts: int = int(os.getenv("POST_WAKE_RETRY_ATTEMPTS", "3"))
    post_wake_retry_delay_seconds: float = float(
        os.getenv("POST_WAKE_RETRY_DELAY_SECONDS", "2")
    )
    kubernetes_api_timeout_seconds: int = int(os.getenv("KUBERNETES_API_TIMEOUT_SECONDS", "10"))
    kubernetes_ca_cert_path: str = os.getenv(
        "KUBERNETES_CA_CERT_PATH",
        "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    )
    pod_namespace: str = os.getenv("POD_NAMESPACE", "").strip()
    auto_sleep_enabled: bool = os.getenv("AUTO_SLEEP_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    auto_sleep_delay_seconds: float = float(os.getenv("AUTO_SLEEP_DELAY_SECONDS", "2"))
    auto_sleep_level: int = int(os.getenv("AUTO_SLEEP_LEVEL", "1"))
    debug_history_size: int = int(os.getenv("DEBUG_HISTORY_SIZE", "200"))


settings = _Settings()

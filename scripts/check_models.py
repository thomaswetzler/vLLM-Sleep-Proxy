#!/usr/bin/env python3
"""check_models.py — verify model files on the shared vllm-model-cache PVC.

Spins up a short-lived Alpine pod, mounts the PVC, and checks each model
directory for: existence, config.json, at least one .safetensors weight file,
and absence of incomplete-download artefacts (.part / .tmp).

Usage:
    python3 scripts/check_models.py          # uses env / defaults
    NAMESPACE=vllm make check-models         # via Makefile
"""

import json
import os
import subprocess
import sys
import time

NAMESPACE  = os.environ.get("NAMESPACE",  "vllm")
KUBECTL    = os.environ.get("KUBECTL",    "kubectl")
PVC_NAME   = os.environ.get("PVC_NAME",   "vllm-model-cache")
CHECK_IMG  = os.environ.get("CHECK_IMG",  "alpine:3.19")
TIMEOUT    = int(os.environ.get("CHECK_TIMEOUT", "120"))
POD_NAME   = "vllm-model-check"

# Keep in sync with helm/models/values.yaml loader.models (enabled entries)
_DEFAULT_MODEL_DIRS = [
    "gemma-3-4b-it",
    "llama-3.1-8b-instruct",
    "qwen2.5-coder-7b-instruct",
    "qwen2.5-14b-instruct",
]
MODEL_DIRS = os.environ.get("MODEL_DIRS", "").split() or _DEFAULT_MODEL_DIRS


# ── helpers ────────────────────────────────────────────────────────────────────

def kubectl(*args, capture=False, check=True):
    cmd = [KUBECTL, *args]
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)
    return subprocess.run(cmd, check=check)


def cleanup():
    kubectl(
        "delete", "pod", POD_NAME,
        "-n", NAMESPACE, "--ignore-not-found",
        capture=True, check=False,
    )


# ── pod spec ───────────────────────────────────────────────────────────────────

def _build_script():
    """Return a single sh -c string that checks every model directory."""
    parts = ["errors=0"]
    for d in MODEL_DIRS:
        parts.append(
            f'p=/data/models/{d}; '
            f'if   [ ! -d "$p" ];                          then s=MISSING; '
            f'elif [ ! -f "$p/config.json" ];              then s=NO_CONFIG; '
            f'elif ! ls "$p"/*.safetensors >/dev/null 2>&1; then s=NO_WEIGHTS; '
            f'elif ls "$p"/*.part "$p"/*.tmp >/dev/null 2>&1; then s=INCOMPLETE; '
            f'else s=OK; fi; '
            f'printf "  %-44s %s\\n" "{d}" "$s"; '
            f'[ "$s" = OK ] || errors=$((errors+1))'
        )
    parts.append(
        '[ $errors -eq 0 ] '
        '&& printf "\\nAll models OK\\n" '
        '|| { printf "\\n%s model(s) have issues\\n" $errors; exit 1; }'
    )
    return "; ".join(parts)


def _pod_overrides(script):
    return json.dumps({
        "spec": {
            "volumes": [
                {"name": "m", "persistentVolumeClaim": {"claimName": PVC_NAME}}
            ],
            "containers": [{
                "name": "c",
                "image": CHECK_IMG,
                "command": ["sh", "-c", script],
                # Mount PVC at /data — models live at /data/models/<name>
                "volumeMounts": [{"name": "m", "mountPath": "/data"}],
            }],
            "restartPolicy": "Never",
        }
    })


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"Checking model files on PVC {PVC_NAME} (namespace: {NAMESPACE})...\n",
          flush=True)

    cleanup()

    script   = _build_script()
    overrides = _pod_overrides(script)

    kubectl(
        "run", POD_NAME,
        "-n", NAMESPACE,
        "--image", CHECK_IMG,
        "--restart=Never",
        f"--overrides={overrides}",
    )

    # Wait for pod to reach a terminal phase
    phase = "Unknown"
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        r = kubectl(
            "get", "pod", POD_NAME,
            "-n", NAMESPACE,
            "-o", "jsonpath={.status.phase}",
            capture=True, check=False,
        )
        phase = r.stdout.strip()
        if phase in ("Succeeded", "Failed"):
            break
        time.sleep(2)
    else:
        print(f"\nTimeout: pod did not finish within {TIMEOUT}s.", file=sys.stderr)
        cleanup()
        sys.exit(1)

    # Print pod output
    kubectl("logs", POD_NAME, "-n", NAMESPACE, check=False)

    # Retrieve container exit code
    r = kubectl(
        "get", "pod", POD_NAME,
        "-n", NAMESPACE,
        "-o", "jsonpath={.status.containerStatuses[0].state.terminated.exitCode}",
        capture=True, check=False,
    )
    try:
        exit_code = int(r.stdout.strip())
    except (ValueError, AttributeError):
        exit_code = 1

    cleanup()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

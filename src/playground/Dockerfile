FROM registry.fedoraproject.org/fedora-minimal:latest

USER 0

ARG VLLM_PLAYGROUND_VERSION=0.1.7

ENV PYTHONUNBUFFERED=1 \
    HOME=/home/playground \
    PYTHONPATH=/home/playground/vllm-playground \
    PATH="/usr/local/bin:/home/playground/.local/bin:$PATH"

# Build-time patch assets. These are copied into the image once and then applied
# to the installed upstream wheel so Helm only has to provide configuration.
COPY patch_playground.py remote-targets.js /tmp/playground-build/

RUN microdnf install -y \
    ca-certificates \
    python3.12 \
    curl \
    podman-remote \
    shadow-utils \
    && microdnf clean all

RUN ln -sf /usr/bin/python3.12 /usr/bin/python3 && \
    if [ ! -f /usr/bin/podman ] && [ -f /usr/bin/podman-remote ]; then \
        ln -sf /usr/bin/podman-remote /usr/bin/podman; \
    fi && \
    python3.12 -m ensurepip --upgrade && \
    python3.12 -m pip install --no-cache-dir --upgrade pip

RUN useradd -m -u 1001 -g 0 -s /bin/bash playground && \
    chmod -R g=u /home/playground

RUN python3.12 -m pip install --no-cache-dir \
    "vllm-playground[mcp]==${VLLM_PLAYGROUND_VERSION}" \
    "mcp>=1.0.0" \
    podman && \
    python3.12 - <<'PY'
from pathlib import Path
import shutil
import vllm_playground

src = Path(vllm_playground.__file__).resolve().parent
dst_root = Path("/home/playground/vllm-playground")
dst = dst_root / "vllm_playground"
dst_root.mkdir(parents=True, exist_ok=True)
if dst.exists():
    shutil.rmtree(dst)
shutil.copytree(src, dst)
PY
# Patch the copied app tree in place: backend accepts an explicit remote model
# and the frontend gets the model/target selector helper.
RUN python3.12 /tmp/playground-build/patch_playground.py /home/playground/vllm-playground /tmp/playground-build/remote-targets.js && \
    rm -rf /tmp/playground-build

RUN chmod -R 775 "${HOME}/vllm-playground"

USER 1001

WORKDIR /home/playground/vllm-playground

ENV WEBUI_PORT=7860 \
    VLLM_IMAGE=quay.io/rh_ee_micyang/vllm-mac:v0.11.0 \
    MODELS_DIR=/models \
    # The playground is used in remote mode in-cluster, but upstream still
    # expects a runtime binary to exist when container support is enabled.
    CONTAINER_RUNTIME=podman

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:7860/api/status || exit 1

CMD ["python3.12", "-m", "vllm_playground.app"]

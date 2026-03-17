#!/usr/bin/env python3
"""Patch the packaged vLLM Playground at image-build time.

The upstream wheel does not yet expose a remote model selector, so the image
adds the small backend and frontend hooks we need during the Docker build.
Doing this here keeps the Helm chart focused on configuration instead of source
rewrites at pod startup.
"""

from __future__ import annotations

import shutil
import sys
from hashlib import sha256
from pathlib import Path


REQUEST_MODEL_FIELD = (
    "    model: Optional[str] = None  # Explicit remote model override per chat request\n"
)
REQUEST_MODEL_ANCHOR = "    top_logprobs: Optional[int] = None\n"
PAYLOAD_MODEL_OLD = '            "model": get_model_name_for_api(),\n'
PAYLOAD_MODEL_NEW = '            "model": request.model or get_model_name_for_api(),\n'
def patch_app(app_path: Path) -> None:
    """Allow `/api/chat` callers to override the remote model explicitly."""
    text = app_path.read_text(encoding="utf-8")

    if REQUEST_MODEL_FIELD not in text:
        if REQUEST_MODEL_ANCHOR not in text:
            raise SystemExit("ChatRequestWithStopTokens anchor not found")
        text = text.replace(REQUEST_MODEL_ANCHOR, REQUEST_MODEL_ANCHOR + REQUEST_MODEL_FIELD, 1)

    if PAYLOAD_MODEL_OLD in text:
        text = text.replace(PAYLOAD_MODEL_OLD, PAYLOAD_MODEL_NEW, 1)
    elif PAYLOAD_MODEL_NEW not in text:
        raise SystemExit("chat payload model assignment not found")

    app_path.write_text(text, encoding="utf-8")


def patch_index(index_path: Path, js_source: Path) -> None:
    """Inject the custom remote-target script exactly once."""
    text = index_path.read_text(encoding="utf-8")
    asset_hash = sha256(js_source.read_bytes()).hexdigest()[:12]
    marker = f'    <script type="module" src="/assets/remote-targets.js?v={asset_hash}"></script>'
    if '/assets/remote-targets.js' in text:
        return

    if "</body>" in text:
        text = text.replace("</body>", marker + "\n</body>", 1)
    else:
        text = text.rstrip() + "\n" + marker + "\n"

    index_path.write_text(text, encoding="utf-8")


def install_remote_targets_js(app_root: Path, js_source: Path) -> None:
    """Install the frontend helper next to the packaged playground assets."""
    asset_dir = app_root / "vllm_playground" / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(js_source, asset_dir / "remote-targets.js")


def main() -> None:
    """Patch a copied playground tree in place."""
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch_playground.py <playground-root> <remote-targets-js>")

    app_root = Path(sys.argv[1]).resolve()
    js_source = Path(sys.argv[2]).resolve()

    if not app_root.exists():
        raise SystemExit(f"playground root does not exist: {app_root}")
    if not js_source.exists():
        raise SystemExit(f"remote-targets.js does not exist: {js_source}")

    patch_app(app_root / "vllm_playground" / "app.py")
    patch_index(app_root / "vllm_playground" / "index.html", js_source)
    install_remote_targets_js(app_root, js_source)


if __name__ == "__main__":
    main()

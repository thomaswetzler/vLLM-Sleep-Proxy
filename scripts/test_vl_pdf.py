#!/usr/bin/env python3
"""Convert a local PDF form to Markdown using the configured vision model."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_PROMPT = (
    "Convert this PDF form page into clean Markdown. Preserve headings, section "
    "numbers, tables, checkboxes, and empty input fields using Markdown-friendly "
    "placeholders like [ ] and __________. Output only Markdown."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--completions-url")
    parser.add_argument("--api-key")
    parser.add_argument("--model", required=True)
    parser.add_argument("--pdf-path", required=True)
    parser.add_argument("--output-path")
    parser.add_argument("--text-prompt", default="What is 2 * 5? Reply with just the result.")
    parser.add_argument("--text-max-tokens", type=int, default=32)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--max-tokens", type=int, default=2200)
    return parser.parse_args()


def render_pdf_page_to_png(pdf_path: Path) -> Path:
    fd, png_path = tempfile.mkstemp(prefix="test-vl-", suffix=".png")
    os.close(fd)
    png_file = Path(png_path)
    command = [
        "sips",
        "-s",
        "format",
        "png",
        str(pdf_path),
        "--out",
        str(png_file),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "PDF preview rendering failed via sips:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return png_file


def file_to_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def extract_markdown(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    raise RuntimeError(f"Unexpected completion content format: {content!r}")


def request_completion(
    *,
    base_url: str | None,
    completions_url: str | None,
    api_key: str | None,
    model: str,
    user_content: list[dict[str, Any]],
    max_tokens: int,
) -> str:
    if base_url:
        wake_request = urllib.request.Request(
            f"{base_url.rstrip('/')}/wake_up",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(wake_request, timeout=300):
            pass

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an OCR and document extraction assistant. Preserve structure "
                    "and output only Markdown."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
    }
    target_url = (
        f"{base_url.rstrip('/')}/v1/chat/completions"
        if base_url
        else completions_url
    )
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        target_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        data = json.loads(response.read().decode("utf-8"))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected completion response: {data!r}") from exc
    if base_url:
        try:
            sleep_request = urllib.request.Request(
                f"{base_url.rstrip('/')}/sleep?level=1",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(sleep_request, timeout=120):
                pass
        except Exception:
            pass
    return extract_markdown(content)


def request_text_prompt(
    *,
    base_url: str | None,
    completions_url: str | None,
    api_key: str | None,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    return request_completion(
        base_url=base_url,
        completions_url=completions_url,
        api_key=api_key,
        model=model,
        user_content=[{"type": "text", "text": prompt}],
        max_tokens=max_tokens,
    )


def request_markdown(
    *,
    base_url: str | None,
    completions_url: str | None,
    api_key: str | None,
    model: str,
    prompt: str,
    image_url: str,
    max_tokens: int,
) -> str:
    return request_completion(
        base_url=base_url,
        completions_url=completions_url,
        api_key=api_key,
        model=model,
        user_content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        max_tokens=max_tokens,
    )


def main() -> int:
    args = parse_args()
    if bool(args.base_url) == bool(args.completions_url):
        raise SystemExit("Specify exactly one of --base-url or --completions-url")
    if args.completions_url and not args.api_key:
        raise SystemExit("--api-key is required with --completions-url")
    pdf_path = Path(args.pdf_path).resolve()
    output_path = (
        Path(args.output_path).resolve()
        if args.output_path
        else pdf_path.with_suffix(".md")
    )

    text_result = request_text_prompt(
        base_url=args.base_url,
        completions_url=args.completions_url,
        api_key=args.api_key,
        model=args.model,
        prompt=args.text_prompt,
        max_tokens=args.text_max_tokens,
    )
    print(f"Text prompt result: {text_result}")
    print()

    png_path = render_pdf_page_to_png(pdf_path)
    try:
        markdown = request_markdown(
            base_url=args.base_url,
            completions_url=args.completions_url,
            api_key=args.api_key,
            model=args.model,
            prompt=args.prompt,
            image_url=file_to_data_url(png_path),
            max_tokens=args.max_tokens,
        )
    finally:
        png_path.unlink(missing_ok=True)

    output_path.write_text(markdown + "\n", encoding="utf-8")
    print(f"Markdown written to {output_path}")
    print()
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())

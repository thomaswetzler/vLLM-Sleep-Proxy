"""Static runtime catalog for non-router engines.

The existing stack discovers vLLM engines through the upstream router. For
additional runtimes such as llama.cpp we keep a small declarative catalog in an
environment variable so the sleep-proxy can still make wake/sleep decisions and
know where to forward requests.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, List, Optional

from .config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineCatalogEntry:
    """One statically configured engine endpoint managed outside the router."""

    model: str
    runtime: str
    endpoint: str
    node_name: Optional[str] = None
    engine_key: Optional[str] = None

    @property
    def engine_id(self) -> str:
        return self.engine_key or f"{self.runtime}:{self.model}"

    @property
    def model_basename(self) -> str:
        return self.model.rstrip("/").split("/")[-1]


def _entry_from_raw(raw: Any) -> Optional[EngineCatalogEntry]:
    """Parse one raw JSON object from ENGINE_CATALOG_JSON."""
    if not isinstance(raw, dict):
        return None

    model = str(raw.get("model", "") or "").strip()
    runtime = str(raw.get("runtime", "") or "").strip().lower()
    endpoint = str(raw.get("endpoint", "") or "").strip().rstrip("/")
    node_name = str(raw.get("nodeName", "") or raw.get("node_name", "") or "").strip()
    engine_key = str(raw.get("engineKey", "") or raw.get("engine_key", "") or "").strip()

    if not model or not runtime or not endpoint:
        return None

    return EngineCatalogEntry(
        model=model,
        runtime=runtime,
        endpoint=endpoint,
        node_name=node_name or None,
        engine_key=engine_key or None,
    )


@lru_cache(maxsize=1)
def list_entries() -> List[EngineCatalogEntry]:
    """Return the parsed static runtime catalog."""
    raw_catalog = settings.engine_catalog_json.strip()
    if not raw_catalog:
        return []

    try:
        payload = json.loads(raw_catalog)
    except json.JSONDecodeError as exc:
        logger.warning("ENGINE_CATALOG_JSON konnte nicht geparst werden: %s", exc)
        return []

    if not isinstance(payload, list):
        logger.warning("ENGINE_CATALOG_JSON muss eine JSON-Liste sein, erhalten: %r", type(payload))
        return []

    entries: List[EngineCatalogEntry] = []
    for item in payload:
        entry = _entry_from_raw(item)
        if entry is None:
            logger.warning("Ungueltiger Engine-Catalog-Eintrag wird ignoriert: %r", item)
            continue
        entries.append(entry)
    return entries


def find_entry_for_model(model: str) -> Optional[EngineCatalogEntry]:
    """Match a model id either by full id or by basename fallback."""
    requested = str(model or "").strip()
    if not requested:
        return None

    for entry in list_entries():
        if entry.model == requested:
            return entry

    requested_basename = requested.rstrip("/").split("/")[-1]
    if requested_basename:
        for entry in list_entries():
            if entry.model_basename == requested_basename:
                return entry

    return None

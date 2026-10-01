"""Read the public NetBox Labs NDX catalog and downloadable YAML definitions."""

from __future__ import annotations

import time
from typing import Any

import requests
import yaml
import re

BASE_URL = "https://netboxlabs.com/ndx"
INDEX_URL = f"{BASE_URL}/data/search-index.json"
_cache: tuple[float, list[dict[str, Any]]] | None = None


def _index() -> list[dict[str, Any]]:
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < 900:
        return _cache[1]
    response = requests.get(INDEX_URL, timeout=30)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("NDX search index returned an unexpected payload")
    _cache = (now, rows)
    return rows


def search(query: str, limit: int = 200) -> list[dict[str, Any]]:
    terms = query.casefold().split()
    results = []
    for row in _index():
        if row.get("type") != "device-type":
            continue
        haystack = " ".join(str(row.get(key) or "") for key in (
            "vendor_name", "manufacturer", "model", "part_number", "slug"
        )).casefold()
        if terms and not all(term in haystack for term in terms):
            continue
        results.append({
            **{key: (None if row.get(key) is None else str(row[key])) for key in (
                "vendor_slug", "vendor_name", "manufacturer", "model", "slug", "part_number", "source"
            )},
            "u_height": row.get("u_height"),
        })
        if len(results) >= limit:
            break
    return results


def get_yaml(vendor_slug: str, slug: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", vendor_slug) or not re.fullmatch(r"[A-Za-z0-9_.-]+", slug):
        raise ValueError("Invalid NDX vendor/device identifier")
    response = requests.get(f"{BASE_URL}/{vendor_slug}/{slug}/yaml", timeout=30)
    response.raise_for_status()
    payload = yaml.safe_load(response.text)
    if not isinstance(payload, dict):
        raise ValueError("NDX YAML returned an unexpected payload")
    return payload

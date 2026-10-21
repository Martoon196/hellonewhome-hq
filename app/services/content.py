"""Loads file-based content: marketing templates, area content template, resource seed."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .. import config


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in {path}: {e}") from e


@lru_cache(maxsize=1)
def marketing_templates() -> dict:
    data = _load(config.CONTENT_DIR / "marketing-templates.json", {})
    data.setdefault("flyers", [])
    data.setdefault("fb_posts", [])
    data.setdefault("graphics", [])
    data.setdefault("gbp", {})
    data.setdefault("letters", [])
    data.setdefault("scripts", [])
    return data


@lru_cache(maxsize=1)
def area_content_template() -> dict:
    return _load(config.CONTENT_DIR / "area-content-template.json", {})


def bespoke_area_content(slug: str) -> dict:
    return _load(config.CONTENT_DIR / "areas" / f"{slug}.json", {})


def resources_seed() -> list:
    return _load(config.CONTENT_DIR / "resources-seed.json", [])


def resource_body(name: str) -> str:
    """The HTML body of a seeded in-app resource page, kept as a file under content/resources/ so it can be
    written and read as HTML rather than as a JSON string. Missing file: an empty body, never a failed boot."""
    path = config.CONTENT_DIR / "resources" / f"{name}.html"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def areas_seed() -> list:
    return _load(config.CONTENT_DIR / "areas-seed.json", [])


def reload() -> None:
    marketing_templates.cache_clear()
    area_content_template.cache_clear()

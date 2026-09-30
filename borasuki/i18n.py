"""Shared locale catalog for desktop and Windows integrations."""

import json
import logging
import re
from importlib.resources import files

logger = logging.getLogger(__name__)
SUPPORTED_LANGUAGES = ("en", "tr")
REFERENCE = re.compile(r"\{@([\w.]+)\}")


def resolve_references(catalog: dict[str, str]) -> dict[str, str]:
    resolved, visiting = {}, set()

    def resolve(key):
        if key in resolved:
            return resolved[key]
        if key in visiting:
            raise ValueError(f"Circular locale reference: {key}")
        visiting.add(key)
        resolved[key] = REFERENCE.sub(lambda match: resolve(match[1]), catalog[key])
        visiting.remove(key)
        return resolved[key]

    for key in catalog:
        resolve(key)
    return resolved


def _read_locale(language: str) -> dict[str, str]:
    path = files("borasuki").joinpath("locales", f"{language}.json")
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in catalog.items()
    ):
        raise ValueError(f"Invalid locale catalog: {language}")
    return catalog


def load_catalog(language: str) -> dict[str, str]:
    """Return a fresh catalog, falling back to English for missing entries."""
    catalog = _read_locale("en")
    if language not in SUPPORTED_LANGUAGES:
        logger.warning("Unsupported locale %r; using English", language)
    elif language != "en":
        translated = _read_locale(language)
        missing = catalog.keys() - translated.keys()
        if missing:
            logger.warning("Locale %s is missing keys: %s", language, sorted(missing))
        catalog.update(translated)
    return resolve_references(catalog)


def translate(catalog: dict[str, str], key: str, **values: object) -> str:
    """Missing keys or placeholders raise instead of hiding catalog defects."""
    return catalog[key].format(**values)

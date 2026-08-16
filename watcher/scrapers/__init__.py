"""Scraper registry: one module per source.

Each exposes `fetch(seen: set[str]) -> Iterator[Listing]` and streams its
results, so listings are persisted as they arrive rather than only at the end.
"""
from __future__ import annotations

import importlib
import logging
from typing import Callable, Iterator

from ..models import Listing

log = logging.getLogger(__name__)


def get_scraper(name: str) -> Callable[[set[str]], Iterator[Listing]]:
    module = importlib.import_module(f".{name}", package=__name__)
    return module.fetch

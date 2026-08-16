"""Scraper registry: one module per source, each exposing fetch() -> list[Listing]."""
from __future__ import annotations

import importlib
import logging
from typing import Callable, Iterator

from ..models import Listing

log = logging.getLogger(__name__)


def get_scraper(name: str) -> Callable[[set[str]], Iterator[Listing]]:
    module = importlib.import_module(f".{name}", package=__name__)
    return module.fetch

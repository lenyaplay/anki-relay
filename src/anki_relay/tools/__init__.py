"""MCP tools, grouped by area. Each module exposes ``register(registry)``."""

from __future__ import annotations

from . import cards, decks, media, note_types, notes, search, sync, tags

MODULES = (search, notes, cards, decks, note_types, tags, media, sync)


def register_all(registry) -> None:  # type: ignore[no-untyped-def]
    for module in MODULES:
        module.register(registry)
    registry.finish()

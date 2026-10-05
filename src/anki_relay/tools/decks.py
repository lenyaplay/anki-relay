"""Deck and deck options tools."""

from __future__ import annotations

import copy
from typing import Any, Literal

import anki.collection  # noqa: F401
from anki.collection import Collection
from mcp.server.fastmcp.exceptions import ToolError

from .runtime import Registry, Result, deck_id

READ_ONLY_OPTIONS = {"id", "mod", "usn", "dyn"}


def _merge(target: dict[str, Any], changes: dict[str, Any], path: str = "") -> None:
    for key, value in changes.items():
        if key not in target or key in READ_ONLY_OPTIONS:
            allowed = sorted(k for k in target if k not in READ_ONLY_OPTIONS)
            raise ToolError(f"Unknown option {path + key!r}. Available here: {', '.join(allowed)}")
        if isinstance(target[key], dict):
            if not isinstance(value, dict):
                raise ToolError(f"Option {path + key!r} is a group; pass an object of sub-options.")
            _merge(target[key], value, f"{path}{key}.")
        else:
            target[key] = value


def _options_view(conf: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in conf.items() if k not in READ_ONLY_OPTIONS}


def _deck_config(col: Collection, did: int) -> dict[str, Any]:
    if col.decks.is_filtered(did):
        raise ToolError("Filtered decks have no options group.")
    return col.decks.config_dict_for_deck_id(did)


def register(r: Registry) -> None:
    rt = r.runtime

    @r.tool()
    async def create_deck(name: str) -> str:
        """Create a deck; use '::' for subdecks (e.g. 'Languages::Spanish::Verbs').
        Missing parent decks are created too."""

        def run(col: Collection) -> Result:
            existed = col.decks.id_for_name(name) is not None
            did = col.decks.id(name, create=True)
            return {"deck": col.decks.name(did), "id": did, "created": not existed}

        return await rt.call(run, mutates=lambda res: res["created"])

    @r.tool()
    async def rename_deck(name: str, new_name: str) -> str:
        """Rename a deck or move it under another parent ('A::B' -> 'C::B').
        Subdecks move with it."""

        def run(col: Collection) -> Result:
            did = deck_id(col, name)
            col.decks.rename(did, new_name)
            return {"deck": col.decks.name(did), "id": did}

        return await rt.call(run, mutates=True)

    @r.tool(destructive=True)
    async def delete_deck(name: str, dry_run: bool = True) -> str:
        """Delete a deck with its subdecks and all their cards (notes left without
        cards are deleted too). dry_run=true (default) only reports what would be
        deleted."""

        def run(col: Collection) -> Result:
            did = deck_id(col, name)
            subdecks = [n for n, i in col.decks.deck_and_child_name_ids(did) if i != did]
            cards = col.decks.card_count(did, include_subdecks=True)
            result: Result = {
                "dry_run": dry_run,
                "deck": col.decks.name(did),
                "subdecks": subdecks,
                "cards": cards,
            }
            if not dry_run:
                col.decks.remove([did])
                result["deleted"] = True
            return result

        return await rt.call(run, mutates=lambda res: bool(res.get("deleted")))

    @r.tool(read_only=True)
    async def get_deck_options(deck: str) -> str:
        """The whole options group (preset) of a deck: new cards/day, reviews/day,
        learning and relearning steps, FSRS parameters and desired retention, display
        order, burying of siblings, leech handling, timers, audio, etc. Also lists the
        decks sharing this group. (FSRS on/off is a collection-wide setting.)"""

        def run(col: Collection) -> Result:
            did = deck_id(col, deck)
            conf = _deck_config(col, did)
            used_by = [col.decks.name(d) for d in col.decks.decks_using_config(conf)]
            return {
                "deck": col.decks.name(did),
                "preset": conf["name"],
                "shared_with": [n for n in used_by if n != col.decks.name(did)],
                "fsrs_enabled": bool(col.get_config("fsrs", False)),
                "options": _options_view(conf),
            }

        return await rt.call(run)

    @r.tool()
    async def update_deck_options(
        deck: str,
        changes: dict[str, Any],
        scope: Literal["shared", "new_preset"] | None = None,
    ) -> str:
        """Change options of a deck's options group, partially: `changes` uses the keys
        of get_deck_options, nested groups as objects, e.g. {"new": {"perDay": 30},
        "rev": {"perDay": 300}, "desiredRetention": 0.9}. If the group is shared by
        several decks, the call fails listing them unless scope="shared" (change the
        group for all of them) or scope="new_preset" (give this deck its own copy first)."""
        if not changes:
            raise ToolError("No changes given.")

        def run(col: Collection) -> Result:
            did = deck_id(col, deck)
            conf = _deck_config(col, did)
            users = col.decks.decks_using_config(conf)
            name = col.decks.name(did)
            others = [col.decks.name(d) for d in users if d != did]
            if others and scope is None:
                raise ToolError(
                    f"Options group {conf['name']!r} is shared with: {', '.join(others)}. "
                    "Pass scope='shared' to change it for all of them, or "
                    "scope='new_preset' to give this deck its own copy."
                )
            trial = copy.deepcopy(conf)
            _merge(trial, changes)
            if scope == "new_preset" and others:
                new_conf = col.decks.add_config(name, clone_from=conf)
                deck_dict = col.decks.get(did)
                assert deck_dict is not None
                deck_dict["conf"] = new_conf["id"]
                col.decks.save(deck_dict)
                conf = new_conf
            _merge(conf, changes)
            col.decks.update_config(conf)
            fresh = col.decks.config_dict_for_deck_id(did)
            return {
                "deck": name,
                "preset": fresh["name"],
                "affected_decks": [name] + (others if scope == "shared" else []),
                "applied": changes,
            }

        return await rt.call(run, mutates=True)

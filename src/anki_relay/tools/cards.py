"""Card tools."""

from __future__ import annotations

import anki.collection  # noqa: F401
from anki.collection import Collection

from .runtime import Registry, Result, check_batch, ensure_deck, existing_ids


def register(r: Registry) -> None:
    rt = r.runtime
    settings = r.settings

    def with_cards(card_ids: list[int], action, **extra) -> Result:  # type: ignore[no-untyped-def]
        def run(col: Collection) -> Result:
            existing, missing = existing_ids(col, card_ids, "card")
            result: Result = {"cards": len(existing), **extra}
            if existing:
                action(col, existing, result)
            if missing:
                result["not_found"] = missing
            return result

        return run

    @r.tool()
    async def move_cards(card_ids: list[int], deck: str) -> str:
        """Move cards to a deck (created if missing; '::' for subdecks)."""
        check_batch(card_ids, settings, "card ids")

        def action(col: Collection, ids: list[int], result: Result) -> None:
            col.set_deck(ids, ensure_deck(col, deck))
            result["moved_to"] = deck

        return await rt.call(with_cards(card_ids, action), mutates=True)

    @r.tool()
    async def set_suspended(card_ids: list[int], suspended: bool) -> str:
        """Suspend (suspended=true) or unsuspend (false) cards."""
        check_batch(card_ids, settings, "card ids")

        def action(col: Collection, ids: list[int], result: Result) -> None:
            if suspended:
                col.sched.suspend_cards(ids)
            else:
                col.sched.unsuspend_cards(ids)

        return await rt.call(with_cards(card_ids, action, suspended=suspended), mutates=True)

    @r.tool()
    async def set_buried(card_ids: list[int], buried: bool) -> str:
        """Bury cards until tomorrow (buried=true) or unbury them (false)."""
        check_batch(card_ids, settings, "card ids")

        def action(col: Collection, ids: list[int], result: Result) -> None:
            if buried:
                col.sched.bury_cards(ids, manual=True)
            else:
                col.sched.unbury_cards(ids)

        return await rt.call(with_cards(card_ids, action, buried=buried), mutates=True)

    @r.tool(destructive=True)
    async def forget_cards(
        card_ids: list[int],
        dry_run: bool = True,
        restore_position: bool = True,
        reset_counts: bool = False,
    ) -> str:
        """Reset cards to new, discarding their scheduling (review history is kept).
        restore_position puts them back at their original new position; reset_counts
        zeroes the review and lapse counters. dry_run=true (default) changes nothing and
        reports how many cards would be reset."""
        check_batch(card_ids, settings, "card ids")

        def action(col: Collection, ids: list[int], result: Result) -> None:
            reviewed = len(col.find_cards("cid:" + ",".join(map(str, ids)) + " -is:new"))
            result["already_studied"] = reviewed
            if not dry_run:
                col.sched.schedule_cards_as_new(
                    ids, restore_position=restore_position, reset_counts=reset_counts
                )
                result["reset"] = len(ids)

        return await rt.call(
            with_cards(card_ids, action, dry_run=dry_run),
            mutates=lambda res: bool(res.get("reset")),
        )

    @r.tool()
    async def set_due_date(card_ids: list[int], days: str) -> str:
        """Set the due date in Anki's syntax: "0" = today, "1" = tomorrow, "1-7" = random
        day in the range, a trailing "!" (e.g. "3!") also sets the interval to that value."""
        check_batch(card_ids, settings, "card ids")

        def action(col: Collection, ids: list[int], result: Result) -> None:
            col.sched.set_due_date(ids, days)

        return await rt.call(with_cards(card_ids, action, days=days), mutates=True)

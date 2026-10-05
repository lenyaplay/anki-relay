"""Overview and search tools."""

from __future__ import annotations

import datetime as dt
from typing import Any

import anki.collection  # noqa: F401
from anki.cards import Card
from anki.collection import Collection

from .runtime import (
    DEFAULT_LIMIT,
    Registry,
    Result,
    deck_id,
    existing_ids,
    page,
    text_preview,
)

QUEUES = {
    -3: "buried",
    -2: "buried",
    -1: "suspended",
    0: "new",
    1: "learning",
    2: "review",
    3: "learning",
    4: "preview",
}
TYPES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}
MAX_REVLOG = 50
MAX_OVERVIEW_DECKS = 300


def _date_for_day(col: Collection, day: int) -> str:
    return (dt.date.today() + dt.timedelta(days=day - col.sched.today)).isoformat()


def due_info(col: Collection, card: Card) -> Any:
    # In a filtered deck report the home deck schedule.
    due = card.odue if card.odid else card.due
    if card.type == 0:
        return {"new_position": due}
    if card.queue in (1, 4) or (card.type in (1, 3) and due > 1_000_000_000):
        return dt.datetime.fromtimestamp(due, dt.UTC).isoformat(timespec="minutes")
    return _date_for_day(col, due)


def card_summary(col: Collection, card: Card, deck_names: dict[int, str]) -> Result:
    nt = card.note_type()
    tmpl = nt["tmpls"][card.ord]["name"] if nt["type"] == 0 else f"Cloze {card.ord + 1}"
    out: Result = {
        "id": card.id,
        "note_id": card.nid,
        "deck": deck_names.get(card.did, str(card.did)),
        "template": tmpl,
        "queue": QUEUES.get(card.queue, str(card.queue)),
        "due": due_info(col, card),
        "interval_days": card.ivl,
    }
    if card.memory_state:
        out["difficulty"] = round(card.memory_state.difficulty, 2)
        out["stability_days"] = round(card.memory_state.stability, 1)
    elif card.factor:
        out["ease"] = card.factor / 1000
    if card.odid:
        out["home_deck"] = deck_names.get(card.odid, str(card.odid))
    if card.flags:
        out["flag"] = card.user_flag()
    return out


def deck_name_map(col: Collection) -> dict[int, str]:
    return {d.id: d.name for d in col.decks.all_names_and_ids()}


def register(r: Registry) -> None:
    rt = r.runtime

    @r.tool(read_only=True)
    async def collection_overview() -> str:
        """Structure of the collection: decks (card count, options group), note types
        (fields, number of card templates, number of notes), tag count and totals.
        Start here when asked to understand or reorganise the user's collection."""

        def run(col: Collection) -> Result:
            decks = []
            all_decks = sorted(col.decks.all_names_and_ids(), key=lambda d: d.name)
            for d in all_decks[:MAX_OVERVIEW_DECKS]:
                entry: Result = {
                    "name": d.name,
                    "cards": col.decks.card_count(d.id, include_subdecks=False),
                }
                if col.decks.is_filtered(d.id):
                    entry["filtered"] = True
                else:
                    entry["options"] = col.decks.config_dict_for_deck_id(d.id)["name"]
                decks.append(entry)
            note_types = []
            for item in col.models.all_use_counts():
                nt = col.models.get(item.id)
                if nt is None:
                    continue
                note_types.append(
                    {
                        "name": item.name,
                        "kind": "cloze" if nt["type"] == 1 else "standard",
                        "fields": [f["name"] for f in nt["flds"]],
                        "templates": len(nt["tmpls"]),
                        "notes": item.use_count,
                    }
                )
            result: Result = {
                "notes": col.note_count(),
                "cards": col.card_count(),
                "tags": len(col.tags.all()),
                "decks": decks,
                "note_types": note_types,
            }
            if len(all_decks) > MAX_OVERVIEW_DECKS:
                result["decks_truncated"] = len(all_decks)
            return result

        return await rt.call(run)

    @r.tool(read_only=True)
    async def find_notes(
        query: str,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        fields: list[str] | None = None,
    ) -> str:
        """Search notes with Anki search syntax (e.g. 'deck:Spanish tag:verb', 'added:7',
        'note:Basic "front:*cat*"'; empty string = all). Returns id, note type, tags,
        decks of its cards and plain-text field previews (all fields or only `fields`).
        Use get_notes for exact field HTML. Paginate with limit/offset."""

        def run(col: Collection) -> Result:
            ids = sorted(col.find_notes(query))
            chunk, meta = page(ids, limit, offset)
            names = deck_name_map(col)
            items = []
            for nid in chunk:
                note = col.get_note(nid)
                nt = note.note_type()
                assert nt is not None
                if fields:
                    selected = {f: note[f] for f in fields if f in note}
                else:
                    selected = dict(note.items())
                items.append(
                    {
                        "id": nid,
                        "note_type": nt["name"],
                        "fields": {k: text_preview(v) for k, v in selected.items()},
                        "tags": note.tags,
                        "decks": sorted({names.get(c.did, str(c.did)) for c in note.cards()}),
                    }
                )
            return {**meta, "notes": items}

        return await rt.call(run)

    @r.tool(read_only=True)
    async def find_cards(query: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> str:
        """Search cards with Anki search syntax (e.g. 'deck:French is:due', 'is:suspended',
        'prop:ivl>30'). Returns id, note id, deck, template, queue, due, interval and
        ease (SM-2) or difficulty/stability (FSRS). Paginate with limit/offset."""

        def run(col: Collection) -> Result:
            ids = sorted(col.find_cards(query))
            chunk, meta = page(ids, limit, offset)
            names = deck_name_map(col)
            return {**meta, "cards": [card_summary(col, col.get_card(c), names) for c in chunk]}

        return await rt.call(run)

    @r.tool(read_only=True)
    async def get_notes(note_ids: list[int]) -> str:
        """Full data of notes: note type, every field with its exact HTML, tags and
        cards (id, deck, template, queue)."""

        def run(col: Collection) -> Result:
            existing, missing = existing_ids(col, note_ids, "note")
            names = deck_name_map(col)
            notes = []
            for nid in existing:
                note = col.get_note(nid)
                nt = note.note_type()
                assert nt is not None
                notes.append(
                    {
                        "id": nid,
                        "note_type": nt["name"],
                        "fields": dict(note.items()),
                        "tags": note.tags,
                        "modified": dt.datetime.fromtimestamp(note.mod, dt.UTC).isoformat(
                            timespec="seconds"
                        ),
                        "cards": [
                            {
                                "id": c.id,
                                "deck": names.get(c.did, str(c.did)),
                                "template": card_summary(col, c, names)["template"],
                                "queue": QUEUES.get(c.queue, str(c.queue)),
                            }
                            for c in note.cards()
                        ],
                    }
                )
            result: Result = {"notes": notes}
            if missing:
                result["not_found"] = missing
            return result

        return await rt.call(run)

    @r.tool(read_only=True)
    async def card_info(card_ids: list[int]) -> str:
        """Details of cards including review history (latest 50 revlog entries each):
        added, first/latest review, due, interval, ease or FSRS memory state, lapses,
        reviews, time spent."""

        def run(col: Collection) -> Result:
            existing, missing = existing_ids(col, card_ids, "card")
            names = deck_name_map(col)
            cards = []
            for cid in existing:
                card = col.get_card(cid)
                s = col.card_stats_data(cid)
                info = card_summary(col, card, names)
                info.update(
                    {
                        "added": _ts(s.added),
                        "first_review": _ts(s.first_review) if s.HasField("first_review") else None,
                        "latest_review": (
                            _ts(s.latest_review) if s.HasField("latest_review") else None
                        ),
                        "reviews": s.reviews,
                        "lapses": s.lapses,
                        "total_seconds": round(s.total_secs, 1),
                        "options": s.preset,
                    }
                )
                if s.HasField("fsrs_retrievability"):
                    info["retrievability"] = round(s.fsrs_retrievability, 3)
                info["revlog"] = [
                    {
                        "time": _ts(e.time),
                        "kind": _REVIEW_KINDS.get(e.review_kind, str(e.review_kind)),
                        "button": e.button_chosen,
                        "interval_days": round(e.interval / 86400, 2),
                        "seconds": round(e.taken_secs, 1),
                    }
                    for e in list(s.revlog)[:MAX_REVLOG]
                ]
                cards.append(info)
            result: Result = {"cards": cards}
            if missing:
                result["not_found"] = missing
            return result

        return await rt.call(run)

    @r.tool(read_only=True)
    async def stats(deck: str | None = None) -> str:
        """Today's numbers for the whole collection or one deck (with subdecks): cards
        due today within the deck limits (new / learning / review), studied today, and
        totals of new, learning, review-due and suspended cards."""

        def run(col: Collection) -> Result:
            scope = ""
            if deck:
                did = deck_id(col, deck)
                node = col.sched.deck_due_tree(did)
                nodes = [node] if node else []
                scope = f'deck:"{col.decks.name(did)}" '
            else:
                root = col.sched.deck_due_tree()
                nodes = list(root.children) if root else []

            def count(q: str) -> int:
                return len(col.find_cards(scope + q))

            return {
                "scope": deck or "collection",
                "due_today": {
                    "new": sum(n.new_count for n in nodes),
                    "learning": sum(n.learn_count for n in nodes),
                    "review": sum(n.review_count for n in nodes),
                },
                "studied_today": count("rated:1"),
                "totals": {
                    "new": count("is:new"),
                    "learning": count("is:learn"),
                    "due": count("is:due"),
                    "suspended": count("is:suspended"),
                },
            }

        return await rt.call(run)


_REVIEW_KINDS = {0: "learning", 1: "review", 2: "relearning", 3: "filtered", 4: "manual"}


def _ts(value: int) -> str:
    return dt.datetime.fromtimestamp(value, dt.UTC).isoformat(timespec="seconds")

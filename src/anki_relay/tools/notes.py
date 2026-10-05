"""Note tools."""

from __future__ import annotations

from typing import Any, Required, TypedDict

import anki.collection  # noqa: F401
from anki.collection import Collection
from anki.errors import AnkiException
from anki.notes import NoteFieldsCheckResult
from mcp.server.fastmcp.exceptions import ToolError

from .runtime import (
    SCHEMA_DANGER,
    Registry,
    Result,
    check_batch,
    ensure_deck,
    existing_ids,
    field_index,
    field_names,
    notetype,
    tags_list,
    template_index,
)

CHECK_PROBLEMS = {
    NoteFieldsCheckResult.EMPTY: "skipped: the first field is empty",
    NoteFieldsCheckResult.DUPLICATE: (
        "skipped: duplicate (a note of this type with the same first field exists; "
        "pass allow_duplicates=true to add anyway)"
    ),
    NoteFieldsCheckResult.MISSING_CLOZE: (
        "skipped: cloze note type but the text has no cloze deletion like {{c1::...}}"
    ),
    NoteFieldsCheckResult.NOTETYPE_NOT_CLOZE: (
        "skipped: the text has cloze deletions but the note type is not a cloze type"
    ),
    NoteFieldsCheckResult.FIELD_NOT_CLOZE: (
        "skipped: cloze deletion in a field that the template does not use as cloze"
    ),
}


class NewNote(TypedDict, total=False):
    deck: Required[str]
    note_type: Required[str]
    fields: Required[dict[str, str]]
    tags: list[str]


class NoteUpdate(TypedDict, total=False):
    note_id: Required[int]
    fields: dict[str, str]
    tags: list[str]


def _set_fields(nt: dict[str, Any], note: Any, fields: dict[str, Any]) -> None:
    for name, value in fields.items():
        field_index(nt, name)
        note[name] = "" if value is None else str(value)


def change_note_type_plan(
    col: Collection,
    note_ids: list[int],
    new_note_type: str,
    field_map: dict[str, str | None],
    template_map: dict[str, str | None] | None,
) -> tuple[Any, Result]:
    existing, missing = existing_ids(col, note_ids, "note")
    if not existing:
        raise ToolError("None of the given notes exist.")
    try:
        old_id = col.models.get_single_notetype_of_notes(existing)
    except AnkiException:
        raise ToolError(
            "All notes must have the same note type; call once per note type."
        ) from None
    old = col.models.get(old_id)
    assert old is not None
    new = notetype(col, new_note_type)
    info = col.models.change_notetype_info(old_notetype_id=old_id, new_notetype_id=new["id"])
    req = info.input

    new_from = [-1] * len(new["flds"])
    for old_name, new_name in field_map.items():
        oi = field_index(old, old_name)
        if new_name is None:
            continue
        ni = field_index(new, new_name)
        if new_from[ni] != -1:
            raise ToolError(f"Two old fields are mapped to the new field {new_name!r}.")
        new_from[ni] = oi
    del req.new_fields[:]
    req.new_fields.extend(new_from)
    kept_fields = {i for i in new_from if i >= 0}
    lost_fields = [f for i, f in enumerate(field_names(old)) if i not in kept_fields]

    both_standard = old["type"] == 0 and new["type"] == 0
    lost_templates: list[str] = []
    template_plan: dict[str, str | None] = {}
    if both_standard:
        if template_map is not None:
            new_tmpl_from = [-1] * len(new["tmpls"])
            for old_name, new_name in template_map.items():
                oi = template_index(old, old_name)
                if new_name is None:
                    continue
                ni = template_index(new, new_name)
                if new_tmpl_from[ni] != -1:
                    raise ToolError(f"Two old templates are mapped to {new_name!r}.")
                new_tmpl_from[ni] = oi
            del req.new_templates[:]
            req.new_templates.extend(new_tmpl_from)
        mapped = {i for i in req.new_templates if i >= 0}
        for i, t in enumerate(old["tmpls"]):
            target = next((j for j, src in enumerate(req.new_templates) if src == i), None)
            template_plan[t["name"]] = new["tmpls"][target]["name"] if target is not None else None
            if i not in mapped:
                lost_templates.append(t["name"])
    elif template_map:
        raise ToolError("template_map only applies when both note types are standard (not cloze).")

    del req.note_ids[:]
    req.note_ids.extend(existing)

    nid_query = "nid:" + ",".join(map(str, existing))
    cards_lost = 0
    for name in lost_templates:
        cards_lost += len(col.find_cards(f'{nid_query} "card:{name}"'))
    plan: Result = {
        "notes": len(existing),
        "from_note_type": old["name"],
        "to_note_type": new["name"],
        "fields": {
            new["flds"][i]["name"]: (old["flds"][src]["name"] if src >= 0 else None)
            for i, src in enumerate(new_from)
        },
        "old_fields_dropped": lost_fields,
        "requires_full_sync": True,
    }
    if template_plan:
        plan["templates"] = template_plan
        plan["cards_deleted"] = cards_lost
    if missing:
        plan["not_found"] = missing
    return req, plan


def register(r: Registry) -> None:
    rt = r.runtime
    settings = r.settings

    @r.tool()
    async def add_notes(notes: list[NewNote], allow_duplicates: bool = False) -> str:
        """Add notes in one batch (up to MAX_BATCH). Each item: {"deck", "note_type",
        "fields": {field name: HTML}, "tags": [..]}; items may use different decks and
        note types. Missing decks are created (use '::' for subdecks). Empty, duplicate
        and invalid cloze notes are skipped. Returns a result per item: note_id or error.
        Upload media with add_media first and put the returned snippet into a field."""
        check_batch(notes, settings, "notes")

        def run(col: Collection) -> Result:
            results = []
            added = 0
            for i, item in enumerate(notes):
                try:
                    nt = notetype(col, item["note_type"])
                    note = col.new_note(nt)
                    fields = item.get("fields") or {}
                    if not fields:
                        raise ToolError("no fields given")
                    _set_fields(nt, note, fields)
                    note.tags = tags_list(item.get("tags"))
                    problem = note.fields_check()
                    if problem != NoteFieldsCheckResult.NORMAL and not (
                        problem == NoteFieldsCheckResult.DUPLICATE and allow_duplicates
                    ):
                        results.append({"index": i, "error": CHECK_PROBLEMS[problem]})
                        continue
                    did = ensure_deck(col, item["deck"])
                    col.add_note(note, did)
                    added += 1
                    results.append({"index": i, "note_id": note.id})
                except (ToolError, AnkiException, KeyError) as exc:
                    msg = f"missing key {exc}" if isinstance(exc, KeyError) else str(exc)
                    results.append({"index": i, "error": msg})
            return {"added": added, "failed": len(notes) - added, "results": results}

        return await rt.call(run, mutates=lambda res: res["added"] > 0)

    @r.tool()
    async def update_notes(updates: list[NoteUpdate]) -> str:
        """Change notes in one batch: [{"note_id", "fields"?: {name: HTML}, "tags"?: [..]}].
        Only the given fields change; "tags", when given, replaces the note's tags.
        Returns a result per item."""
        check_batch(updates, settings, "updates")

        def run(col: Collection) -> Result:
            ids = [int(u["note_id"]) for u in updates if "note_id" in u]
            existing, _ = existing_ids(col, ids, "note")
            exists = set(existing)
            results = []
            changed = 0
            for i, item in enumerate(updates):
                try:
                    nid = int(item["note_id"])
                    if nid not in exists:
                        raise ToolError(f"note {nid} not found")
                    if "fields" not in item and "tags" not in item:
                        raise ToolError("nothing to change: pass fields and/or tags")
                    note = col.get_note(nid)
                    nt = note.note_type()
                    assert nt is not None
                    if item.get("fields"):
                        _set_fields(nt, note, item["fields"])
                    if "tags" in item:
                        note.tags = tags_list(item.get("tags"))
                    col.update_note(note)
                    changed += 1
                    results.append({"index": i, "note_id": nid, "ok": True})
                except (ToolError, AnkiException, KeyError, ValueError) as exc:
                    msg = f"missing key {exc}" if isinstance(exc, KeyError) else str(exc)
                    results.append({"index": i, "error": msg})
            return {"updated": changed, "failed": len(updates) - changed, "results": results}

        return await rt.call(run, mutates=lambda res: res["updated"] > 0)

    @r.tool(destructive=True)
    async def delete_notes(note_ids: list[int], dry_run: bool = True) -> str:
        """Delete notes and all their cards. With dry_run=true (default) nothing is
        changed and the number of affected notes and cards is returned."""
        check_batch(note_ids, settings, "note ids")

        def run(col: Collection) -> Result:
            existing, missing = existing_ids(col, note_ids, "note")
            cards = len(col.find_cards("nid:" + ",".join(map(str, existing)))) if existing else 0
            result: Result = {"dry_run": dry_run, "notes": len(existing), "cards": cards}
            if missing:
                result["not_found"] = missing
            if not dry_run and existing:
                col.remove_notes(existing)
                result["deleted"] = len(existing)
            return result

        return await rt.call(run, mutates=lambda res: bool(res.get("deleted")))

    @r.tool(destructive=True, schema=True)
    async def change_note_type(
        note_ids: list[int],
        new_note_type: str,
        field_map: dict[str, str | None],
        template_map: dict[str, str | None] | None = None,
        dry_run: bool = True,
    ) -> str:
        """Convert notes (all of one note type) to another note type. field_map maps
        old field name -> new field name (or null to drop); unmapped old fields are
        lost. template_map (standard types only) maps old template -> new template;
        cards of unmapped templates are deleted. Default template mapping is by
        position. dry_run=true (default) only reports what would happen."""
        check_batch(note_ids, settings, "note ids")
        if dry_run:

            def preview(col: Collection) -> Result:
                _, plan = change_note_type_plan(
                    col, note_ids, new_note_type, field_map, template_map
                )
                return {"dry_run": True, **plan, "warning": SCHEMA_DANGER}

            return await rt.call(preview)

        def apply(col: Collection) -> Result:
            req, plan = change_note_type_plan(col, note_ids, new_note_type, field_map, template_map)
            col.models.change_notetype_of_notes(req)
            return {"dry_run": False, "changed": plan["notes"], **plan}

        return await rt.call_schema(apply)

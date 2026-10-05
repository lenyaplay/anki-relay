"""Tag tools."""

from __future__ import annotations

import re

import anki.collection  # noqa: F401
from anki.collection import Collection
from mcp.server.fastmcp.exceptions import ToolError

from .runtime import Registry, Result, check_batch, existing_ids, page, unknown

_SEARCH_SPECIAL = re.compile(r'([\\"*_:()\-])')


def _escape(tag: str) -> str:
    return _SEARCH_SPECIAL.sub(r"\\\1", tag)


def register(r: Registry) -> None:
    rt = r.runtime
    settings = r.settings

    @r.tool(read_only=True)
    async def list_tags(limit: int = 200, offset: int = 0) -> str:
        """All tags (hierarchical tags use '::') with the number of notes carrying
        exactly that tag. Paginate with limit/offset."""

        def run(col: Collection) -> Result:
            tags = sorted(col.tags.all(), key=str.lower)
            chunk, meta = page(list(range(len(tags))), limit, offset)
            items = []
            for i in chunk:
                tag = tags[i]
                esc = _escape(tag)
                notes = len(col.find_notes(f'"tag:{esc}" -"tag:{esc}::*"'))
                items.append({"tag": tag, "notes": notes})
            return {**meta, "tags": items}

        return await rt.call(run)

    def bulk(note_ids: list[int], tags: list[str], add: bool):  # type: ignore[no-untyped-def]
        check_batch(note_ids, settings, "note ids")
        joined = " ".join(t.strip() for t in tags if t.strip())
        if not joined:
            raise ToolError("No tags given.")

        def run(col: Collection) -> Result:
            existing, missing = existing_ids(col, note_ids, "note")
            changed = 0
            if existing:
                op = col.tags.bulk_add if add else col.tags.bulk_remove
                changed = op(existing, joined).count
            result: Result = {"notes_changed": changed}
            if missing:
                result["not_found"] = missing
            return result

        return run

    @r.tool()
    async def add_tags(note_ids: list[int], tags: list[str]) -> str:
        """Add tags to notes (batch). Use '::' for hierarchy, e.g. 'lang::es'."""
        return await rt.call(bulk(note_ids, tags, add=True), mutates=True)

    @r.tool()
    async def remove_tags(note_ids: list[int], tags: list[str]) -> str:
        """Remove tags from notes (batch)."""
        return await rt.call(bulk(note_ids, tags, add=False), mutates=True)

    @r.tool()
    async def rename_tag(old: str, new: str) -> str:
        """Rename a tag on all notes; child tags ('old::x') are renamed too
        ('new::x'), so this also moves a tag within the hierarchy."""

        def run(col: Collection) -> Result:
            all_tags = col.tags.all()
            lowered = old.lower()
            if not any(
                t.lower() == lowered or t.lower().startswith(lowered + "::") for t in all_tags
            ):
                raise unknown("tag", old, all_tags)
            count = col.tags.rename(old, new).count
            return {"renamed": old, "to": new, "notes_changed": count}

        return await rt.call(run, mutates=True)

    @r.tool()
    async def clear_unused_tags() -> str:
        """Remove tags that no note uses any more."""

        def run(col: Collection) -> Result:
            return {"removed": col.tags.clear_unused_tags().count}

        return await rt.call(run, mutates=lambda res: res["removed"] > 0)

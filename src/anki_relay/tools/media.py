"""Media tools."""

from __future__ import annotations

import fnmatch
import os

import anki.collection  # noqa: F401
from anki.collection import Collection

from ..media import field_snippet, prepare_media
from .runtime import DEFAULT_LIMIT, Registry, Result, check_batch, page

MAX_REPORTED = 200


def register(r: Registry) -> None:
    rt = r.runtime
    settings = r.settings

    @r.tool(needs_media_sync=True)
    async def add_media(
        filename: str, url: str | None = None, data_base64: str | None = None
    ) -> str:
        """Save an image, audio or video file into the collection's media and return
        the final file name and a ready snippet for a note field (<img src="..."> for
        images, [sound:...] for audio/video). Give exactly one source: url (http/https)
        or data_base64 (plain base64 or a data: URI). If the name has no extension it is
        derived from the content type. Call this before add_notes/update_notes."""
        media = await rt.guard(prepare_media(settings, rt.fetcher, filename, url, data_base64))

        def run(col: Collection) -> Result:
            final = col.media.write_data(media.filename, media.data)
            return {"filename": final, "field": field_snippet(final), "bytes": len(media.data)}

        return await rt.call(run, mutates=True)

    @r.tool(read_only=True)
    async def list_media(
        pattern: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> str:
        """List files in the collection's media folder, optionally filtered by a
        shell-style pattern like '*.mp3' or 'dog*'."""

        def run(col: Collection) -> Result:
            folder = col.media.dir()
            names = sorted(
                n
                for n in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, n))
                and (pattern is None or fnmatch.fnmatch(n.lower(), pattern.lower()))
            )
            chunk, meta = page(list(range(len(names))), limit, offset)
            files = [
                {"filename": names[i], "bytes": os.path.getsize(os.path.join(folder, names[i]))}
                for i in chunk
            ]
            return {**meta, "files": files}

        return await rt.call(run)

    @r.tool(destructive=True)
    async def delete_media(filenames: list[str], dry_run: bool = True) -> str:
        """Delete media files (moved to Anki's media trash; the deletion syncs to other
        devices). Notes referring to them will show missing files. dry_run=true
        (default) only reports which files exist."""
        check_batch(filenames, settings, "file names")

        def run(col: Collection) -> Result:
            existing = [n for n in dict.fromkeys(filenames) if col.media.have(n)]
            missing = [n for n in filenames if n not in existing]
            result: Result = {"dry_run": dry_run, "files": existing}
            if missing:
                result["not_found"] = missing
            if not dry_run and existing:
                col.media.trash_files(existing)
                result["deleted"] = len(existing)
            return result

        return await rt.call(run, mutates=lambda res: bool(res.get("deleted")))

    @r.tool(read_only=True)
    async def check_media() -> str:
        """Anki's media check: files in the media folder that no note uses, and files
        that notes refer to but that are missing."""

        def run(col: Collection) -> Result:
            report = col.media.check()
            return {
                "unused_count": len(report.unused),
                "unused": list(report.unused)[:MAX_REPORTED],
                "missing_count": len(report.missing),
                "missing": list(report.missing)[:MAX_REPORTED],
                "notes_with_missing_media": len(report.missing_media_notes),
            }

        return await rt.call(run)

"""Sync tools."""

from __future__ import annotations

from ..users import User
from .runtime import Registry, Result


def register(r: Registry) -> None:
    rt = r.runtime

    @r.tool()
    async def sync(force_download: bool = False) -> str:
        """Sync with AnkiWeb now, ignoring the usual intervals, then sync media
        (waits up to 30 s). force_download=true replaces the server copy with the
        collection from AnkiWeb (use it when told a full sync is required, after
        syncing Anki on the user's computer); a backup of the server copy is kept.
        If a one-way upload after a schema change is pending, sync() retries it."""

        def run(user: User) -> Result:
            with user.lock:
                user.collection()
                messages = rt.sync.sync_now_locked(user, force_download=force_download)
                rt.sync.start_media_sync_locked(user)
            media = rt.sync.wait_media(user)
            with user.lock:
                status = rt.sync.status_locked(user)
            status.pop("media", None)
            result: Result = {"synced": True, "media": media, "status": status}
            if messages:
                result["messages"] = messages
            return result

        return await rt.call_plain(run)

    @r.tool(read_only=True)
    async def sync_status() -> str:
        """When the last successful sync was, whether there are unsynced changes,
        whether a one-way upload after a schema change is pending, the last error and
        when the next attempt happens, and media sync progress. Does not contact
        AnkiWeb."""

        def run(user: User) -> Result:
            with user.lock:
                return rt.sync.status_locked(user)

        return await rt.call_plain(run)

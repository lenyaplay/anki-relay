"""Collection and media sync with AnkiWeb.

The server behaves like one more Anki device:

* **pull** — before a tool touches the collection, a normal sync runs if the last
  successful one is older than ``SYNC_PULL_INTERVAL``;
* **push** — after a change, one sync runs after ``SYNC_PUSH_DELAY`` seconds of
  quiet, but no later than ``SYNC_PUSH_MAX_DELAY`` after the first unsynced change;
* a full **download** happens only when AnkiWeb asks for it and the local copy has
  nothing unsynced (or was never synced), or when the user forces it;
* a full **upload** happens only inside the schema change protocol
  (:meth:`SyncManager.schema_change_locked`). There is deliberately no other
  code path that uploads; a test enforces this.

Methods ending in ``_locked`` must be called from a worker thread holding
``user.lock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import anki.collection  # noqa: F401 - import order matters for anki
from anki.collection import Collection
from anki.errors import NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth, SyncOutput

from .config import Settings
from .users import User, UserManager

log = logging.getLogger(__name__)

T = TypeVar("T")

BACKOFF_BASE = 5.0
BACKOFF_MAX = 300.0
MEDIA_WAIT_SECONDS = 30.0
MEDIA_POLL_SECONDS = 0.5

FORCE_DOWNLOAD_HINT = (
    "Sync Anki on your computer first, then call sync(force_download=true) to replace "
    "the server copy with the one from AnkiWeb."
)
PENDING_SCHEMA_MSG = (
    "A note type structure change was applied on the server copy but its one-way upload "
    "to AnkiWeb has not completed yet. Normal sync is paused until it does: call sync() "
    "to retry the upload, or sync(force_download=true) to discard the change."
)


class SyncProblem(Exception):
    """A sync failure with a message meant for the user.

    ``details`` carries structured state (e.g. the ``full_sync`` block) so that
    Claude can decide with the user instead of parsing prose.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


class SyncUnavailable(SyncProblem):
    """Transient: network trouble, AnkiWeb errors or rate limiting; retried with backoff."""


class SyncConflict(SyncProblem):
    """AnkiWeb wants a full sync that the server will not do on its own."""


class SyncAuthError(SyncProblem):
    """AnkiWeb rejected the stored login."""


class SchemaChangesDisabled(SyncProblem):
    pass


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds")


class AnkiSyncBackend:
    """The only place that talks to AnkiWeb. Tests replace it with a fake."""

    def login(
        self, col: Collection, username: str, password: str, endpoint: str | None
    ) -> SyncAuth:
        return col.sync_login(username, password, endpoint)

    def sync_collection(self, col: Collection, auth: SyncAuth) -> SyncOutput:
        return col.sync_collection(auth, sync_media=False)

    def full_sync(self, col: Collection, auth: SyncAuth, *, upload: bool) -> None:
        col.full_upload_or_download(auth=auth, server_usn=None, upload=upload)

    def start_media_sync(self, col: Collection, auth: SyncAuth) -> None:
        col.sync_media(auth)

    def media_sync_status(self, col: Collection) -> Any:
        return col.media_sync_status()

    def abort_media_sync(self, col: Collection) -> None:
        col.abort_media_sync()


class SyncManager:
    def __init__(self, settings: Settings, users: UserManager, backend: AnkiSyncBackend) -> None:
        self.settings = settings
        self.users = users
        self.backend = backend
        self.loop: asyncio.AbstractEventLoop | None = None
        self._push_tasks: dict[str, asyncio.Task] = {}
        self._idle_task: asyncio.Task | None = None
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopping = False
        for user_id in self.users.known_ids():
            user = self.users.get(user_id)
            if user.state.dirty and not user.state.pending_schema_upload:
                log.info(
                    "user %s has unsynced changes from a previous run",
                    user.id,
                    extra={"event": "sync.resume", "user": user.id},
                )
                self._schedule_push(user.id)
        self._idle_task = self.loop.create_task(self._idle_loop())

    async def stop(self) -> None:
        """Graceful shutdown (SIGTERM): push what is left, then close collections."""
        self._stopping = True
        tasks = [*self._push_tasks.values()]
        if self._idle_task:
            tasks.append(self._idle_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._push_tasks.clear()
        for user in self.users.loaded():
            st = user.state
            if st.dirty and not st.pending_schema_upload and not st.auth_invalid:
                try:
                    await asyncio.to_thread(self.push_now, user)
                except Exception as exc:
                    log.warning(
                        "final sync of user %s failed: %s",
                        user.id,
                        exc,
                        extra={"event": "sync.shutdown_failed", "user": user.id},
                    )
        await asyncio.to_thread(self.users.close_all)

    # ------------------------------------------------------------------ helpers

    def _endpoint(self) -> str | None:
        return self.settings.sync_endpoint or None

    def auth_for(self, user: User) -> SyncAuth:
        if user.state.auth_invalid:
            raise self._auth_error()
        auth = user.load_auth()
        if auth is None:
            raise SyncAuthError("No AnkiWeb login stored. Reconnect the connector in Claude.")
        return auth

    def _record_success(self, user: User, gen: int) -> None:
        st = user.state
        st.last_sync = time.time()
        st.ever_synced = True
        st.failures = 0
        st.backoff_until = 0.0
        st.last_sync_error = None
        st.conflict = False
        st.full_sync = None
        if user.change_gen == gen:
            st.dirty = False
            st.dirty_since = None
        user.save_state()

    def _record_failure(self, user: User, message: str) -> None:
        st = user.state
        st.failures += 1
        st.backoff_until = time.time() + min(BACKOFF_BASE * 2 ** (st.failures - 1), BACKOFF_MAX)
        st.last_sync_error = message
        user.save_state()
        log.warning(
            "sync of user %s failed (%d in a row): %s",
            user.id,
            st.failures,
            message,
            extra={
                "event": "sync.failed",
                "user": user.id,
                "failures": st.failures,
                "retry_in_s": round(st.backoff_until - time.time()),
            },
        )

    def _translate(self, user: User, exc: Exception) -> SyncProblem:
        if isinstance(exc, SyncError) and exc.kind == SyncErrorKind.AUTH:
            user.state.auth_invalid = True
            user.save_state()
            return self._auth_error()
        if isinstance(exc, NetworkError):
            return SyncUnavailable(f"No connection to AnkiWeb: {exc}")
        if isinstance(exc, SyncError):
            return SyncUnavailable(f"AnkiWeb error: {exc}")
        return SyncUnavailable(f"Sync failed: {exc}")

    @staticmethod
    def _auth_error() -> SyncAuthError:
        return SyncAuthError(
            "AnkiWeb rejected the saved login. Reconnect the connector in Claude "
            "(Settings → Connectors) and sign in again."
        )

    def mark_dirty_locked(self, user: User) -> None:
        st = user.state
        now = time.time()
        user.change_gen += 1
        user.last_change = now
        if not st.dirty:
            st.dirty = True
            st.dirty_since = now
        user.save_state()
        if self.loop is not None and not self._stopping:
            # A fresh context: the push belongs to no single tool call.
            self.loop.call_soon_threadsafe(
                self._schedule_push, user.id, context=contextvars.Context()
            )

    # ------------------------------------------------------------------ pull

    def prepare_locked(self, user: User) -> tuple[Collection, list[str]]:
        """Open the collection and pull from AnkiWeb if due. Returns warnings."""
        col = user.collection()
        st = user.state
        warnings: list[str] = []
        if st.pending_schema_upload:
            warnings.append(PENDING_SCHEMA_MSG)
            return col, warnings
        self.auth_for(user)
        now = time.time()
        due = (
            not st.ever_synced
            or st.last_sync is None
            or now - st.last_sync >= self.settings.sync_pull_interval
        )
        if not due:
            return col, warnings
        if now < st.backoff_until:
            msg = (
                f"AnkiWeb is unreachable ({st.last_sync_error}); next attempt after "
                f"{_iso(st.backoff_until)}."
            )
            if not st.ever_synced:
                raise SyncUnavailable(msg)
            if st.conflict:
                raise SyncConflict(
                    st.last_sync_error or "AnkiWeb requires a full sync.", st.full_sync
                )
            warnings.append(msg + " Working on the server copy; changes will be sent later.")
            return col, warnings
        try:
            warnings.extend(self.normal_sync_locked(user, allow_full_download=True, reason="pull"))
        except SyncUnavailable as exc:
            if not st.ever_synced:
                raise
            warnings.append(f"{exc} Working on the server copy; changes will be sent later.")
        return user.collection(), warnings

    def normal_sync_locked(
        self, user: User, *, allow_full_download: bool, reason: str = "manual"
    ) -> list[str]:
        """One normal sync. Handles a full download only when it is safe."""
        col = user.collection()
        auth = self.auth_for(user)
        st = user.state
        gen = user.change_gen
        start = time.monotonic()
        try:
            out = self.backend.sync_collection(col, auth)
        except Exception as exc:
            problem = self._translate(user, exc)
            log.info(
                "sync (%s) of user %s raised %s",
                reason,
                user.id,
                type(exc).__name__,
                extra={
                    "event": "sync.normal",
                    "user": user.id,
                    "reason": reason,
                    "outcome": type(problem).__name__,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                },
            )
            if not isinstance(problem, SyncAuthError):
                self._record_failure(user, str(problem))
            raise problem from exc
        required_name = SyncOutput.ChangesRequired.Name(out.required)
        log.info(
            "sync (%s) of user %s: %s",
            reason,
            user.id,
            required_name,
            extra={
                "event": "sync.normal",
                "user": user.id,
                "reason": reason,
                "required": required_name,
                "unsynced_before": user.state.dirty,
                "duration_ms": round((time.monotonic() - start) * 1000),
            },
        )
        if out.new_endpoint:
            user.save_auth(auth.hkey, out.new_endpoint)
        required = out.required
        if required in (SyncOutput.NO_CHANGES, SyncOutput.NORMAL_SYNC):
            self._record_success(user, gen)
            return []
        details = self._full_sync_details(user, col, required)
        if allow_full_download and details["safe_to_download"]:
            # Downloading only replaces the server copy, which here holds nothing that
            # AnkiWeb lacks: never synced, or no unsynced changes and no local schema
            # change. Real AnkiWeb answers FULL_SYNC (not FULL_DOWNLOAD) after another
            # device uploaded a schema change, so this is the common case.
            first = not st.ever_synced
            backup = self._full_download_locked(user, auth)
            if first:
                return []
            return [
                "AnkiWeb required a full sync (usually a note type was restructured on "
                "another device). The server copy had no unsynced changes, so it was "
                "replaced with the collection from AnkiWeb; nothing was lost"
                + (f" (backup: {backup})." if backup else ".")
            ]
        if details["ankiweb_empty"]:
            msg = (
                "The collection on AnkiWeb is empty, while the server copy is not. Ask the "
                "user whether AnkiWeb was reset on purpose. The server never uploads on its "
                "own: to keep the collection, upload it to AnkiWeb from Anki on the user's "
                "computer, then call sync(force_download=true)."
            )
        elif details["unsynced_changes_on_server"]:
            msg = (
                "AnkiWeb requires a full sync (probably a note type was restructured on "
                "another device), but the server copy has unsynced changes that downloading "
                "would discard. Tell the user what would be lost and call "
                "sync(force_download=true) only after they confirm (a backup of the server "
                "copy is kept)."
            )
        else:
            msg = (
                "AnkiWeb requires a full sync that would overwrite AnkiWeb with the server "
                "copy; the server never does that on its own. " + FORCE_DOWNLOAD_HINT
            )
        st.conflict = True
        st.full_sync = details
        self._record_failure(user, msg)
        raise SyncConflict(msg, details)

    @staticmethod
    def _full_sync_details(user: User, col: Collection, required: int) -> dict[str, Any]:
        st = user.state
        wants_full = required in (SyncOutput.FULL_SYNC, SyncOutput.FULL_DOWNLOAD)
        local_schema = bool(col.schema_changed()) if st.ever_synced else False
        unsynced = bool(st.dirty) if st.ever_synced else False
        return {
            "answer": SyncOutput.ChangesRequired.Name(required),
            "unsynced_changes_on_server": unsynced,
            "local_schema_changed": local_schema,
            "pending_schema_upload": st.pending_schema_upload,
            "ankiweb_empty": required == SyncOutput.FULL_UPLOAD,
            "safe_to_download": wants_full
            and not unsynced
            and not local_schema
            and not st.pending_schema_upload,
            "backup_on_download": True,
        }

    # ------------------------------------------------------------------ push

    def _schedule_push(self, user_id: str) -> None:
        if self._stopping or self.loop is None:
            return
        task = self._push_tasks.get(user_id)
        if task is not None and not task.done():
            return  # the running loop re-reads the deadline after each sleep
        self._push_tasks[user_id] = self.loop.create_task(self._push_loop(user_id))

    async def _push_loop(self, user_id: str) -> None:
        user = self.users.get(user_id)
        s = self.settings
        while True:
            st = user.state
            if not st.dirty or st.pending_schema_upload or st.auth_invalid:
                return
            now = time.time()
            first = st.dirty_since if st.dirty_since is not None else now
            deadline = min(user.last_change + s.sync_push_delay, first + s.sync_push_max_delay)
            deadline = max(deadline, st.backoff_until)
            if deadline > now:
                await asyncio.sleep(deadline - now)
                continue
            try:
                await asyncio.to_thread(self.push_now, user)
            except SyncUnavailable:
                continue  # backoff recorded; retry after it
            except SyncProblem:
                return  # needs the user; state shows it
            except Exception:
                log.exception(
                    "unexpected error while pushing user %s",
                    user.id,
                    extra={"event": "sync.push_crashed", "user": user.id},
                )
                return

    def push_now(self, user: User) -> None:
        with user.lock:
            st = user.state
            if not st.dirty or st.pending_schema_upload:
                return
            self.normal_sync_locked(user, allow_full_download=False, reason="push")
            self.start_media_sync_locked(user)

    # ------------------------------------------------------------------ full sync

    def _wait_media_idle_locked(self, user: User, timeout: float = 60.0) -> None:
        col = user.collection()
        end = time.monotonic() + timeout
        aborted = False
        while True:
            try:
                active = self.backend.media_sync_status(col).active
            except Exception:
                return
            if not active:
                return
            if time.monotonic() > end and not aborted:
                self.backend.abort_media_sync(col)
                aborted = True
                end = time.monotonic() + 10
            elif time.monotonic() > end:
                return
            time.sleep(MEDIA_POLL_SECONDS)

    def _full_download_locked(self, user: User, auth: SyncAuth) -> str | None:
        """Replace the server copy with AnkiWeb's. Returns the backup file name, if any."""
        st = user.state
        backup = None
        if st.ever_synced and user.col_path.exists():
            backup = self.backup_locked(user, "before-download").name
        self._wait_media_idle_locked(user)
        col = user.collection()
        log.info(
            "full download for user %s",
            user.id,
            extra={"event": "sync.full_download", "user": user.id, "backup": backup},
        )
        start = time.monotonic()
        col.close_for_full_sync()
        try:
            self.backend.full_sync(col, auth, upload=False)
        except Exception as exc:
            problem = self._translate(user, exc)
            self._record_failure(user, str(problem))
            raise problem from exc
        finally:
            col.reopen(after_full_sync=True)
        log.info(
            "full download for user %s done",
            user.id,
            extra={
                "event": "sync.full_download_done",
                "user": user.id,
                "duration_ms": round((time.monotonic() - start) * 1000),
            },
        )
        st.pending_schema_upload = False
        self._record_success(user, user.change_gen)
        user.state.dirty = False
        user.state.dirty_since = None
        user.save_state()
        self.start_media_sync_locked(user)
        return backup

    def _upload_after_schema_change_locked(self, user: User, auth: SyncAuth) -> None:
        """The one and only one-way upload. Called from the schema change protocol."""
        st = user.state
        # Set before uploading so that a crash in the middle is not forgotten.
        st.pending_schema_upload = True
        user.save_state()
        self._wait_media_idle_locked(user)
        col = user.collection()
        log.info(
            "one-way upload after schema change for user %s",
            user.id,
            extra={"event": "sync.full_upload", "user": user.id},
        )
        start = time.monotonic()
        col.close_for_full_sync()
        try:
            self.backend.full_sync(col, auth, upload=True)
        except Exception as exc:
            problem = self._translate(user, exc)
            self._record_failure(user, str(problem))
            raise problem from exc
        finally:
            col.reopen(after_full_sync=True)
        log.info(
            "one-way upload for user %s done",
            user.id,
            extra={
                "event": "sync.full_upload_done",
                "user": user.id,
                "duration_ms": round((time.monotonic() - start) * 1000),
            },
        )
        st.pending_schema_upload = False
        self._record_success(user, user.change_gen)
        st.dirty = False
        st.dirty_since = None
        user.save_state()

    def schema_change_locked(
        self, user: User, apply: Callable[[Collection], T]
    ) -> tuple[T, dict[str, Any]]:
        """Run a schema-changing operation under the full upload protocol.

        1. a normal sync must finish with NO_CHANGES / NORMAL_SYNC, else cancel;
        2. back up the collection file;
        3. apply the change;
        4. if Anki marked the schema modified, upload the collection one-way.
           A failed upload leaves ``pending_schema_upload`` set, which blocks
           normal sync until ``sync()`` retries the upload.
        """
        if not self.settings.allow_schema_changes:
            raise SchemaChangesDisabled("Schema changes are disabled on this server.")
        st = user.state
        if st.pending_schema_upload:
            raise SyncConflict(PENDING_SCHEMA_MSG)
        auth = self.auth_for(user)
        try:
            self.normal_sync_locked(user, allow_full_download=False, reason="schema_presync")
        except SyncProblem as exc:
            raise SyncProblem(f"Cancelled, nothing was changed: {exc}") from exc
        backup = self.backup_locked(user, "schema")
        col = user.collection()
        result = apply(col)
        info: dict[str, Any] = {"backup": backup.name}
        if col.schema_changed():
            try:
                self._upload_after_schema_change_locked(user, auth)
                info["full_upload"] = "done"
                info["next_step"] = (
                    "On every other device choose 'Download from AnkiWeb' at the next sync."
                )
            except SyncProblem as exc:
                info["full_upload"] = f"failed: {exc}"
                info["next_step"] = PENDING_SCHEMA_MSG
        else:
            info["full_upload"] = "not needed"
            self.mark_dirty_locked(user)
        return result, info

    # ------------------------------------------------------------------ explicit sync()

    def sync_now_locked(self, user: User, *, force_download: bool) -> list[str]:
        st = user.state
        auth = self.auth_for(user)
        if force_download:
            self._full_download_locked_forced(user, auth)
            return ["The server copy was replaced with the collection from AnkiWeb."]
        if st.pending_schema_upload:
            self._upload_after_schema_change_locked(user, auth)
            return ["The pending one-way upload completed."]
        warnings = self.normal_sync_locked(user, allow_full_download=True)
        self.start_media_sync_locked(user)
        return warnings

    def _full_download_locked_forced(self, user: User, auth: SyncAuth) -> None:
        st = user.state
        st.dirty = False
        st.dirty_since = None
        st.pending_schema_upload = False
        st.ever_synced = st.ever_synced and user.col_path.exists()
        self._full_download_locked(user, auth)

    # ------------------------------------------------------------------ media

    def start_media_sync_locked(self, user: User) -> str:
        if not self.settings.media_sync:
            return "disabled"
        col = user.collection()
        auth = self.auth_for(user)
        try:
            if self.backend.media_sync_status(col).active:
                return "already running"
        except Exception as exc:
            user.state.last_media_error = str(exc)
            user.save_state()
        self.backend.start_media_sync(col, auth)
        log.info(
            "media sync started for user %s",
            user.id,
            extra={"event": "sync.media", "user": user.id},
        )
        return "started"

    def media_status_locked(self, user: User) -> dict[str, Any]:
        if not self.settings.media_sync:
            return {"enabled": False}
        st = user.state
        result: dict[str, Any] = {"enabled": True}
        if user.col is not None:
            try:
                status = self.backend.media_sync_status(user.col)
                result["active"] = bool(status.active)
                if status.active and status.HasField("progress"):
                    p = status.progress
                    result["progress"] = {
                        "checked": p.checked,
                        "added": p.added,
                        "removed": p.removed,
                    }
            except Exception as exc:
                st.last_media_error = str(exc)
                user.save_state()
                result["active"] = False
        if st.last_media_error:
            result["last_error"] = st.last_media_error
        return result

    def wait_media(self, user: User, timeout: float = MEDIA_WAIT_SECONDS) -> str:
        """Wait for a running media sync (no lock held between polls)."""
        if not self.settings.media_sync:
            return "disabled"
        end = time.monotonic() + timeout
        while True:
            with user.lock:
                if user.col is None:
                    return "idle"
                try:
                    active = self.backend.media_sync_status(user.col).active
                except Exception as exc:
                    user.state.last_media_error = str(exc)
                    user.save_state()
                    return f"failed: {exc}"
                if not active:
                    user.state.last_media_sync = time.time()
                    user.state.last_media_error = None
                    user.save_state()
                    return "done"
            if time.monotonic() >= end:
                return "media is still syncing in the background"
            time.sleep(MEDIA_POLL_SECONDS)

    # ------------------------------------------------------------------ status

    def status_locked(self, user: User) -> dict[str, Any]:
        st = user.state
        now = time.time()
        result: dict[str, Any] = {
            "last_sync": _iso(st.last_sync),
            "seconds_since_last_sync": round(now - st.last_sync) if st.last_sync else None,
            "unsynced_changes": st.dirty,
            "pending_schema_upload": st.pending_schema_upload,
            "ever_synced": st.ever_synced,
        }
        if st.pending_schema_upload:
            result["note"] = PENDING_SCHEMA_MSG
        if st.full_sync:
            result["full_sync"] = st.full_sync
        if st.auth_invalid:
            result["login_invalid"] = True
        if st.last_sync_error:
            result["last_error"] = st.last_sync_error
        if st.backoff_until > now:
            result["next_attempt_after"] = _iso(st.backoff_until)
        result["media"] = self.media_status_locked(user)
        return result

    # ------------------------------------------------------------------ backups

    def backup_locked(self, user: User, reason: str) -> Path:
        """Copy the collection file to backups/ (closing it so the copy is consistent)."""
        was_open = user.col is not None
        user.close()
        user.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S-%f")
        target = user.backups_dir / f"collection-{stamp}-{reason}.anki2"
        try:
            if user.col_path.exists():
                shutil.copy2(user.col_path, target)
        finally:
            if was_open:
                user.collection()
        backups = sorted(user.backups_dir.glob("collection-*.anki2"))
        for old in backups[: -self.settings.backup_keep]:
            with contextlib.suppress(OSError):
                old.unlink()
        log.info(
            "backup of user %s: %s",
            user.id,
            target.name,
            extra={"event": "sync.backup", "user": user.id, "reason": reason},
        )
        return target

    # ------------------------------------------------------------------ idle

    async def _idle_loop(self) -> None:
        max_idle = self.settings.collection_idle_minutes * 60
        while True:
            await asyncio.sleep(min(60.0, max_idle / 2))
            await asyncio.to_thread(self.close_idle, max_idle)

    def close_idle(self, max_idle: float) -> None:
        busy = set()
        for user in self.users.loaded():
            if user.col is None or not user.lock.acquire(blocking=False):
                continue
            try:
                with contextlib.suppress(Exception):
                    if self.backend.media_sync_status(user.col).active:
                        busy.add(user.id)
            finally:
                user.lock.release()
        self.users.close_idle(max_idle, busy)

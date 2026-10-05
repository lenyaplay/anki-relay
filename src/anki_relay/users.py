"""Users, their on-disk data, collections and locks.

Layout: ``data/users/<user_id>/`` holds ``collection.anki2``, ``collection.media/``,
``sync_auth.json`` (hkey + endpoint only), ``state.json`` and ``backups/``.

An Anki collection is not thread-safe: every access to a user's collection
happens while holding ``user.lock``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import os
import threading
import time
from pathlib import Path

import anki.collection  # noqa: F401 - must be imported before anki.notes & co.
from anki.collection import Collection
from anki.sync import SyncAuth

from .config import Settings
from .fileutil import read_json, write_private_json

log = logging.getLogger(__name__)


def user_id_for(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


@dataclasses.dataclass
class UserState:
    """Persisted per-user sync state (``state.json``)."""

    ever_synced: bool = False
    dirty: bool = False
    dirty_since: float | None = None
    last_sync: float | None = None
    last_sync_error: str | None = None
    failures: int = 0
    backoff_until: float = 0.0
    pending_schema_upload: bool = False
    conflict: bool = False
    full_sync: dict | None = None
    auth_invalid: bool = False
    last_media_sync: float | None = None
    last_media_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> UserState:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class User:
    def __init__(self, user_id: str, root: Path) -> None:
        self.id = user_id
        self.dir = root / user_id
        self.lock = threading.Lock()
        self.col: Collection | None = None
        self.last_used = time.monotonic()
        # Bumped on every local change; lets a push know whether new changes
        # arrived while it was running.
        self.change_gen = 0
        self.last_change = 0.0
        self.state = UserState.from_dict(read_json(self.state_path, {}))

    # Paths ---------------------------------------------------------------

    @property
    def col_path(self) -> Path:
        return self.dir / "collection.anki2"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def auth_path(self) -> Path:
        return self.dir / "sync_auth.json"

    @property
    def backups_dir(self) -> Path:
        return self.dir / "backups"

    # Persistence ---------------------------------------------------------

    def save_state(self) -> None:
        write_private_json(self.state_path, dataclasses.asdict(self.state))

    def load_auth(self) -> SyncAuth | None:
        data = read_json(self.auth_path, None)
        if not data or not data.get("hkey"):
            return None
        return SyncAuth(hkey=data["hkey"], endpoint=data.get("endpoint") or None)

    def save_auth(self, hkey: str, endpoint: str | None) -> None:
        # Only the AnkiWeb session key and endpoint are stored, never the password.
        write_private_json(self.auth_path, {"hkey": hkey, "endpoint": endpoint or ""})

    # Collection (call with self.lock held) ---------------------------------

    def collection(self) -> Collection:
        if self.col is None:
            self.dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(self.dir, 0o700)
            self.col = Collection(str(self.col_path))
            log.info("opened collection of user %s", self.id)
        self.last_used = time.monotonic()
        return self.col

    def close(self) -> None:
        if self.col is not None:
            try:
                self.col.close()
            finally:
                self.col = None
            log.info("closed collection of user %s", self.id)


class UserManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = settings.users_dir
        self._users: dict[str, User] = {}
        self._guard = threading.Lock()

    def get(self, user_id: str) -> User:
        if not user_id or not all(c in "0123456789abcdef" for c in user_id):
            raise ValueError("invalid user id")
        with self._guard:
            user = self._users.get(user_id)
            if user is None:
                user = self._users[user_id] = User(user_id, self.root)
            return user

    def for_email(self, email: str) -> User:
        return self.get(user_id_for(email))

    def known_ids(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "state.json").exists())

    def loaded(self) -> list[User]:
        with self._guard:
            return list(self._users.values())

    def close_idle(self, max_idle_seconds: float, busy: set[str] | None = None) -> None:
        now = time.monotonic()
        for user in self.loaded():
            if user.col is None or now - user.last_used < max_idle_seconds:
                continue
            if busy and user.id in busy:
                continue
            if not user.lock.acquire(blocking=False):
                continue
            try:
                if now - user.last_used >= max_idle_seconds:
                    user.close()
            finally:
                user.lock.release()

    def close_all(self) -> None:
        for user in self.loaded():
            with user.lock:
                user.close()

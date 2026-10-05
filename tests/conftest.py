from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import threading
import time
import weakref
from collections import Counter
from pathlib import Path
from typing import Any

import anki.collection  # noqa: F401
import pytest
import uvicorn
from anki import sync_pb2
from anki.collection import Collection
from anki.dbproxy import DBProxy
from anki.errors import NetworkError, SyncError, SyncErrorKind
from anki.sync import SyncAuth, SyncOutput
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp.exceptions import ToolError

from anki_relay import auth as auth_module
from anki_relay.config import load_settings
from anki_relay.server import App, build_app
from anki_relay.sync import AnkiSyncBackend
from anki_relay.users import user_id_for

EMAIL = "alice@example.com"
EMAIL_B = "bob@example.com"
PASSWORD = "correct horse"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"

# Keep the sign-in failure delay short in tests.
auth_module.FAIL_DELAY = 0.01


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeAnkiWeb(AnkiSyncBackend):
    """Stands in for AnkiWeb: counts calls and returns scripted answers.

    Like the real server, it answers FULL_SYNC when the local schema was modified
    since the last sync, and FULL_DOWNLOAD for a device that never synced.
    """

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.uploads: list[bool] = []
        self.script: list[Any] = []  # next answers: int (ChangesRequired) or Exception
        self.full_sync_error: Exception | None = None
        self.media_active_polls = 0
        self.media_started = 0
        self._seen: set[str] = set()
        self.passwords = {EMAIL: PASSWORD, EMAIL_B: PASSWORD}
        self.login_error: Exception | None = None

    # login
    def login(
        self, col: Collection, username: str, password: str, endpoint: str | None
    ) -> SyncAuth:
        self.calls["login"] += 1
        if self.login_error is not None:
            raise self.login_error
        if self.passwords.get(username) != password:
            raise SyncError("Email or password was incorrect", None, None, None, SyncErrorKind.AUTH)
        return SyncAuth(hkey="hkey-" + username, endpoint="https://sync.example/")

    # collection
    def sync_collection(self, col: Collection, auth: SyncAuth) -> SyncOutput:
        self.calls["sync"] += 1
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return SyncOutput(required=answer)
        if col.path not in self._seen:
            self._seen.add(col.path)
            return SyncOutput(required=SyncOutput.FULL_DOWNLOAD)
        if col.schema_changed():
            return SyncOutput(required=SyncOutput.FULL_SYNC)
        col.db.execute("update col set ls = ?", int(time.time() * 1000) + 1)
        return SyncOutput(required=SyncOutput.NO_CHANGES)

    def full_sync(self, col: Collection, auth: SyncAuth, *, upload: bool) -> None:
        self.calls["full_upload" if upload else "full_download"] += 1
        self.uploads.append(upload)
        if self.full_sync_error is not None:
            raise self.full_sync_error
        self._seen.add(col.path)
        # Like a real full sync: afterwards the collection counts as synced.
        DBProxy(weakref.proxy(col._backend)).execute("update col set ls = scm")

    # media
    def start_media_sync(self, col: Collection, auth: SyncAuth) -> None:
        self.calls["media"] += 1
        self.media_started += 1
        self._polls = self.media_active_polls

    def media_sync_status(self, col: Collection) -> Any:
        polls = getattr(self, "_polls", 0)
        if polls > 0:
            self._polls = polls - 1
            return sync_pb2.MediaSyncStatusResponse(
                active=True, progress=sync_pb2.MediaSyncProgress(checked="10", added="1")
            )
        return sync_pb2.MediaSyncStatusResponse(active=False)

    def abort_media_sync(self, col: Collection) -> None:
        self._polls = 0


def network_error() -> NetworkError:
    return NetworkError("connection refused", None, None, None)


def make_settings(tmp_path: Path, **overrides: Any):
    values: dict[str, Any] = {
        "public_url": "http://localhost:8000",
        "allowed_emails": f"{EMAIL},{EMAIL_B}",
        "data_dir": tmp_path / "data",
        "sync_push_delay": 0.3,
        "sync_push_max_delay": 1.5,
        "sync_pull_interval": 60,
    }
    values.update(overrides)
    return load_settings(**values)


class Harness:
    """An in-process app with a fake AnkiWeb and direct tool calls."""

    def __init__(self, app: App, fake: FakeAnkiWeb) -> None:
        self.app = app
        self.fake = fake

    @property
    def settings(self):
        return self.app.settings

    def sign_in(self, email: str = EMAIL) -> str:
        user = self.app.users.for_email(email)
        user.save_auth("hkey-" + email, "https://sync.example/")
        self.app.provider.store.users[user.id] = email
        self.app.provider.store.save()
        return user.id

    async def call(self, tool: str, email: str = EMAIL, **args: Any) -> Any:
        user_id = user_id_for(email)
        token = AccessToken(token="t", client_id="c", scopes=["anki"], subject=user_id)
        reset = auth_context_var.set(AuthenticatedUser(token))
        try:
            content = await self.app.mcp.call_tool(tool, args)
        finally:
            auth_context_var.reset(reset)
        return json.loads(content[0].text)  # type: ignore[index,union-attr]

    async def fails(self, tool: str, email: str = EMAIL, **args: Any) -> str:
        with pytest.raises(ToolError) as info:
            await self.call(tool, email, **args)
        return str(info.value)

    async def tool_names(self) -> set[str]:
        return {t.name for t in await self.app.mcp.list_tools()}

    def user(self, email: str = EMAIL):
        return self.app.users.for_email(email)


@pytest.fixture
def fake() -> FakeAnkiWeb:
    return FakeAnkiWeb()


@pytest.fixture
async def harness_factory(tmp_path: Path, fake: FakeAnkiWeb):
    created: list[Harness] = []

    async def make(**overrides: Any) -> Harness:
        fetcher = overrides.pop("fetcher", None)
        settings = make_settings(tmp_path, **overrides)
        app = build_app(settings, backend=fake, fetcher=fetcher)
        await app.sync.start()
        h = Harness(app, fake)
        created.append(h)
        return h

    yield make
    for h in created:
        h.app.sync._stopping = True
        for task in list(h.app.sync._push_tasks.values()):
            task.cancel()
        if h.app.sync._idle_task:
            h.app.sync._idle_task.cancel()
        await asyncio.to_thread(h.app.users.close_all)


@pytest.fixture
async def h(harness_factory) -> Harness:
    harness = await harness_factory()
    harness.sign_in(EMAIL)
    harness.sign_in(EMAIL_B)
    return harness


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.05) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not reached in time")


@contextlib.asynccontextmanager
async def mcp_session(base_url: str, token: str):
    """A real MCP client session over Streamable HTTP; yields (session, initialize result)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        create_mcp_http_client(headers=headers) as http,
        streamable_http_client(base_url + "/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        yield session, init


class LiveServer:
    """Runs the real ASGI app with uvicorn in a background thread."""

    def __init__(self, app: App, port: int) -> None:
        self.app = app
        self.port = port
        config = uvicorn.Config(app.asgi, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> LiveServer:
        self.thread.start()
        end = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > end:
                raise RuntimeError("server did not start")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


@pytest.fixture
def live_factory(tmp_path: Path, fake: FakeAnkiWeb):
    servers: list[LiveServer] = []
    port = free_port()

    def make(**overrides: Any) -> LiveServer:
        overrides.setdefault("public_url", f"http://127.0.0.1:{port}")
        settings = make_settings(tmp_path, **overrides)
        server = LiveServer(build_app(settings, backend=fake), port).start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        with contextlib.suppress(Exception):
            server.stop()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper() in {
            "PUBLIC_URL",
            "ALLOWED_EMAILS",
            "DISABLED_TOOLS",
            "MEDIA_SYNC",
            "ALLOW_SCHEMA_CHANGES",
            "DATA_DIR",
            "SYNC_ENDPOINT",
        }:
            monkeypatch.delenv(key, raising=False)

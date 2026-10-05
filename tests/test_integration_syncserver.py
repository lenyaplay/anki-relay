"""End-to-end against a real Anki sync server (``python -m anki.syncserver``, local only).

A second Collection plays the user's desktop. This exercises the real anki sync
code paths that the fake cannot: sign-in, full download, normal sync both ways,
media sync and the schema change protocol with its one-way upload.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anki.collection  # noqa: F401
import pytest
from anki.collection import Collection
from anki.sync import SyncAuth, SyncOutput

from anki_relay.config import load_settings
from anki_relay.server import build_app

from .conftest import EMAIL, PASSWORD, LiveServer, free_port, mcp_session
from .test_oauth import Flow
from .test_tools import PNG

pytestmark = pytest.mark.syncserver


@pytest.fixture
def syncserver(tmp_path: Path):
    port = free_port()
    env = dict(
        os.environ,
        SYNC_USER1=f"{EMAIL}:{PASSWORD}",
        SYNC_BASE=str(tmp_path / "server"),
        SYNC_HOST="127.0.0.1",
        SYNC_PORT=str(port),
    )
    # The server logs a lot; never leave its output in an unread pipe.
    log = open(tmp_path / "syncserver.log", "wb")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "anki.syncserver"], env=env, stdout=log, stderr=subprocess.STDOUT
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if time.monotonic() > deadline or proc.poll() is not None:
                raise RuntimeError("anki.syncserver did not start") from None
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/"
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


class Desktop:
    """The user's other device."""

    def __init__(self, path: Path, endpoint: str) -> None:
        path.mkdir(parents=True)
        self.col = Collection(str(path / "collection.anki2"))
        self.auth: SyncAuth = self.col.sync_login(EMAIL, PASSWORD, endpoint)

    def sync(self, on_full: str | None = None) -> int:
        out = self.col.sync_collection(self.auth, sync_media=False)
        if out.required in (SyncOutput.NO_CHANGES, SyncOutput.NORMAL_SYNC):
            return out.required
        assert on_full in ("upload", "download"), SyncOutput.ChangesRequired.Name(out.required)
        self.col.close_for_full_sync()
        try:
            self.col.full_upload_or_download(
                auth=self.auth, server_usn=None, upload=on_full == "upload"
            )
        finally:
            self.col.reopen(after_full_sync=True)
        return out.required

    def sync_media(self) -> None:
        self.col.sync_media(self.auth)
        deadline = time.monotonic() + 30
        while self.col.media_sync_status().active:
            assert time.monotonic() < deadline
            time.sleep(0.1)

    def add(self, front: str) -> None:
        note = self.col.new_note(self.col.models.by_name("Basic"))
        note["Front"] = front
        self.col.add_note(note, self.col.decks.id("Desk"))

    def schema_change_and_upload(self, field: str) -> None:
        nt = self.col.models.by_name("Basic")
        self.col.models.add_field(nt, self.col.models.new_field(field))
        self.col.models.update_dict(nt)
        assert self.sync(on_full="upload") == SyncOutput.FULL_SYNC


def wait(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.1)


async def call(server: LiveServer, token: str, tool: str, **args):
    async with mcp_session(server.url, token) as (session, _):
        res = await session.call_tool(tool, args)
    text = res.content[0].text  # type: ignore[union-attr]
    if res.isError:
        raise AssertionError(text)
    return json.loads(text)


async def call_error(server: LiveServer, token: str, tool: str, **args) -> str:
    async with mcp_session(server.url, token) as (session, _):
        res = await session.call_tool(tool, args)
    assert res.isError
    return res.content[0].text  # type: ignore[union-attr]


async def test_real_sync_round_trip(tmp_path: Path, syncserver: str) -> None:
    # The user's desktop already has a collection on the sync server, with media.
    desktop = Desktop(tmp_path / "desktop", syncserver)
    for i in range(3):
        desktop.add(f"desktop {i}")
    desktop.col.media.write_data("from-desktop.png", base64.b64decode(PNG))
    assert desktop.sync(on_full="upload") == SyncOutput.FULL_UPLOAD  # empty server
    desktop.sync_media()

    settings = load_settings(
        public_url=f"http://127.0.0.1:{free_port()}",
        allowed_emails=EMAIL,
        data_dir=tmp_path / "relay",
        sync_endpoint=syncserver,
        sync_pull_interval=0,
        sync_push_delay=0.3,
        sync_push_max_delay=1,
    )
    port = int(settings.public_url.rsplit(":", 1)[1])
    server = LiveServer(build_app(settings), port).start()
    try:
        # Sign-in through the real login endpoint and the real sync server.
        flow = Flow(server)
        flow.register()
        assert flow.login(flow.authorize(), password="wrong").status_code == 401
        token = flow.tokens()["access_token"]
        user = server.app.users.for_email(EMAIL)
        assert json.loads(user.auth_path.read_text())["hkey"]

        # First call: full download of the existing collection, then media.
        overview = await call(server, token, "collection_overview")
        assert overview["notes"] == 3
        media_dir = Path(str(user.col_path).replace(".anki2", ".media"))
        wait(lambda: (media_dir / "from-desktop.png").exists())

        # Relay → desktop through a normal (delayed) push.
        added = await call(
            server,
            token,
            "add_notes",
            notes=[
                {"deck": "Relay", "note_type": "Basic", "fields": {"Front": f"relay {i}"}}
                for i in range(2)
            ],
        )
        assert added["added"] == 2
        media = await call(server, token, "add_media", filename="relay.png", data_base64=PNG)
        wait(lambda: not user.state.dirty)
        assert desktop.sync() in (SyncOutput.NO_CHANGES, SyncOutput.NORMAL_SYNC)
        assert desktop.col.note_count() == 5
        wait(lambda: not server.app.sync.media_status_locked(user).get("active", False))
        res = await call(server, token, "sync")
        assert res["media"] == "done"
        desktop.sync_media()
        assert desktop.col.media.have(media["filename"])

        # Desktop → relay.
        desktop.add("desktop later")
        desktop.sync()
        found = await call(server, token, "find_notes", query="")
        assert found["total"] == 6

        # Schema change through the protocol: one-way upload, desktop must download.
        dry = await call(
            server,
            token,
            "edit_note_type_schema",
            name="Basic",
            operations=[{"op": "add_field", "name": "Extra"}],
        )
        assert dry["requires_full_sync"] is True
        res = await call(
            server,
            token,
            "edit_note_type_schema",
            name="Basic",
            operations=[{"op": "add_field", "name": "Extra"}],
            dry_run=False,
        )
        assert res["sync"]["full_upload"] == "done"
        assert list(user.backups_dir.glob("*-schema.anki2"))
        required = desktop.sync(on_full="download")
        assert required in (SyncOutput.FULL_DOWNLOAD, SyncOutput.FULL_SYNC)
        fields = [f["name"] for f in desktop.col.models.by_name("Basic")["flds"]]
        assert fields == ["Front", "Back", "Extra"] and desktop.col.note_count() == 6
        # After the upload both sides sync normally again.
        desktop.add("after schema")
        assert desktop.sync() in (SyncOutput.NO_CHANGES, SyncOutput.NORMAL_SYNC)
        assert (await call(server, token, "find_notes", query=""))["total"] == 7

        # The desktop restructures a note type itself. Real AnkiWeb then answers
        # FULL_SYNC (not FULL_DOWNLOAD). The relay copy has nothing unsynced, so the
        # relay downloads on its own (REQ-003) and says so.
        desktop.schema_change_and_upload("FromDesktop")
        nt = await call(server, token, "get_note_type", name="Basic")
        assert [f["name"] for f in nt["fields"]][-1] == "FromDesktop"
        assert "was replaced with the collection from the sync server" in nt["warnings"][0]

        # Same, but the relay has unsynced changes: it refuses to drop them silently.
        settings_push = server.app.settings
        settings_push.sync_push_delay = 30
        settings_push.sync_push_max_delay = 60
        await call(
            server,
            token,
            "add_notes",
            notes=[{"deck": "Relay", "note_type": "Basic", "fields": {"Front": "unsynced"}}],
        )
        desktop.sync(on_full="download")
        desktop.schema_change_and_upload("Second")
        msg = await call_error(server, token, "find_notes", query="")
        assert "only after they confirm" in msg
        block = json.loads(msg.split("full_sync: ", 1)[1])
        assert block["answer"] == "FULL_SYNC"
        assert block["unsynced_changes_on_server"] is True and block["safe_to_download"] is False
        status = await call(server, token, "sync_status")
        assert status["full_sync"]["safe_to_download"] is False
        res = await call(server, token, "sync", force_download=True)
        assert res["synced"]
        nt = await call(server, token, "get_note_type", name="Basic")
        assert [f["name"] for f in nt["fields"]][-1] == "Second"
        assert list(user.backups_dir.glob("*before-download*"))
        status = await call(server, token, "sync_status")
        assert status["unsynced_changes"] is False and status["pending_schema_upload"] is False
    finally:
        server.stop()
        desktop.col.close()


def test_rust_log_has_sync_details_and_no_secrets(tmp_path: Path, syncserver: str) -> None:
    """ANKI_RUST_LOG=debug against a real sync server (REQ-005), in a separate process."""
    desktop = Desktop(tmp_path / "desktop", syncserver)
    desktop.add("on the server")
    desktop.sync(on_full="upload")
    desktop.col.close()

    log_dir = tmp_path / "logs"
    probe = Path(__file__).with_name("rust_log_probe.py")
    proc = subprocess.run(
        [
            sys.executable,
            str(probe),
            syncserver,
            str(tmp_path / "data"),
            str(log_dir),
            EMAIL,
            PASSWORD,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    hkey = proc.stdout.strip().splitlines()[-1]
    text = (log_dir / "anki-rust.log").read_text(encoding="utf-8", errors="replace")
    assert "fetched state" in text and "SyncMeta" in text
    assert "\x1b[" not in text  # no ANSI colour codes in the file
    for name, secret in {"password": PASSWORD, "hkey": hkey, "email": EMAIL}.items():
        assert secret not in text, name

"""Sync behaviour with a fake AnkiWeb that counts calls."""

from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest
from anki.errors import SyncError, SyncErrorKind
from anki.sync import SyncOutput

import anki_relay
from anki_relay import sync as sync_module
from anki_relay.sync import BACKOFF_BASE, BACKOFF_MAX, SyncUnavailable

from .conftest import Harness, network_error, wait_for

SRC = Path(anki_relay.__file__).parent


def basic(front: str, deck: str = "S") -> dict:
    return {"deck": deck, "note_type": "Basic", "fields": {"Front": front, "Back": "b"}}


async def first_call(h: Harness) -> None:
    await h.call("collection_overview")  # initial full download


# ---------------------------------------------------------------- batching


async def test_series_of_calls_gives_one_pull_and_one_push(harness_factory) -> None:
    h = await harness_factory(sync_push_delay=0.5, sync_push_max_delay=10)
    h.sign_in()
    for i in range(20):
        await h.call("add_notes", notes=[basic(f"note {i}")])
    assert h.fake.calls["sync"] == 1  # the first pull (a full download for a new device)
    assert h.fake.calls["full_download"] == 1
    await wait_for(lambda: h.fake.calls["sync"] == 2, timeout=5)
    await asyncio.sleep(1.0)
    assert h.fake.calls["sync"] == 2  # exactly one push for the whole series
    assert not h.user().state.dirty
    assert h.fake.media_started >= 1


async def test_pull_respects_interval(harness_factory) -> None:
    h = await harness_factory(sync_pull_interval=0.5)
    h.sign_in()
    await first_call(h)
    await h.call("find_notes", query="")
    assert h.fake.calls["sync"] == 1
    await asyncio.sleep(0.6)
    await h.call("find_notes", query="")
    assert h.fake.calls["sync"] == 2


async def test_push_max_delay_with_continuous_changes(harness_factory) -> None:
    h = await harness_factory(sync_push_delay=0.5, sync_push_max_delay=1.0)
    h.sign_in()
    await first_call(h)
    start = time.monotonic()
    first_push = None
    while time.monotonic() - start < 2.6:
        await h.call("add_notes", notes=[basic(f"n{time.monotonic()}")])
        if first_push is None and h.fake.calls["sync"] >= 2:
            first_push = time.monotonic() - start
        await asyncio.sleep(0.1)
    assert first_push is not None and 0.8 <= first_push <= 1.6, first_push
    assert h.fake.calls["sync"] >= 3  # changes kept coming, pushes kept happening


async def test_unsynced_changes_pushed_on_shutdown(harness_factory) -> None:
    h = await harness_factory(sync_push_delay=60, sync_push_max_delay=600)
    h.sign_in()
    await h.call("add_notes", notes=[basic("before SIGTERM")])
    assert h.fake.calls["sync"] == 1
    await h.app.sync.stop()  # what the lifespan does on SIGTERM
    assert h.fake.calls["sync"] == 2
    assert not h.user().state.dirty


async def test_leftover_changes_pushed_on_startup(harness_factory) -> None:
    h = await harness_factory(sync_push_delay=60, sync_push_max_delay=600)
    h.sign_in()
    await h.call("add_notes", notes=[basic("crash")])
    # simulate a crash: no flush, just drop everything
    h.app.sync._stopping = True
    for task in h.app.sync._push_tasks.values():
        task.cancel()
    await asyncio.to_thread(h.app.users.close_all)
    assert h.user().state.dirty
    calls = h.fake.calls["sync"]

    h2 = await harness_factory(sync_push_delay=60, sync_push_max_delay=600)
    await wait_for(lambda: h.fake.calls["sync"] == calls + 1, timeout=5)
    assert not h2.user().state.dirty


# ---------------------------------------------------------------- full upload only via the protocol


def test_upload_true_appears_only_in_the_schema_protocol() -> None:
    allowed = {"_upload_after_schema_change_locked"}
    found = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg != "upload":
                        continue
                    if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                        continue
                    found.append((path.name, func.name, ast.unparse(kw.value)))
    # AnkiSyncBackend.full_sync forwards its argument; the only literal True is the protocol.
    literal_true = [f for f in found if f[2] == "True"]
    assert [f[1] for f in literal_true] == ["_upload_after_schema_change_locked"], found
    forwarded = [f for f in found if f[2] != "True"]
    assert [(f[0], f[1]) for f in forwarded] == [("sync.py", "full_sync")], found
    callers = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef):
                for node in ast.walk(func):
                    if (
                        isinstance(node, ast.Attribute)
                        and node.attr in allowed
                        and func.name not in allowed
                    ):
                        callers.add(func.name)
    assert callers == {"schema_change_locked", "sync_now_locked"}


@pytest.mark.parametrize("answer", [SyncOutput.FULL_UPLOAD, SyncOutput.FULL_SYNC])
async def test_normal_path_never_uploads(h: Harness, answer: int) -> None:
    await first_call(h)
    await h.call("add_notes", notes=[basic("x")])
    h.fake.script = [answer] * 5
    with pytest.raises(sync_module.SyncConflict) as info:
        await asyncio.to_thread(h.app.sync.push_now, h.user())
    assert "force_download=true" in str(info.value)
    with pytest.raises(Exception):
        await h.call("sync")
    assert h.fake.uploads.count(True) == 0
    assert h.user().state.dirty  # nothing lost locally


async def test_full_download_with_unsynced_changes_needs_confirmation(h: Harness) -> None:
    await first_call(h)
    await h.call("add_notes", notes=[basic("local only")])
    h.fake.script = [SyncOutput.FULL_DOWNLOAD]
    msg = await h.fails("sync")
    assert "force_download=true" in msg
    assert h.fake.calls["full_download"] == 1  # only the initial one
    res = await h.call("sync", force_download=True)
    assert res["synced"]
    assert h.fake.calls["full_download"] == 2
    assert list((h.user().backups_dir).glob("*before-download*"))


def full_sync_block(message: str) -> dict:
    """The structured block appended to a tool error (REQ-003)."""
    marker = "full_sync: "
    assert marker in message, message
    return json.loads(message.split(marker, 1)[1])


@pytest.mark.parametrize("answer", [SyncOutput.FULL_DOWNLOAD, SyncOutput.FULL_SYNC])
async def test_clean_copy_downloads_automatically(harness_factory, answer: int) -> None:
    h = await harness_factory(sync_pull_interval=0)
    h.sign_in()
    await first_call(h)
    h.fake.script = [answer]
    res = await h.call("find_notes", query="")
    assert "nothing was lost" in res["warnings"][0]
    assert "-before-download.anki2" in res["warnings"][0]
    assert h.fake.calls["full_download"] == 2
    assert h.fake.uploads.count(True) == 0
    assert list(h.user().backups_dir.glob("*before-download*"))
    status = await h.call("sync_status")
    assert "full_sync" not in status and "last_error" not in status


async def test_unsynced_changes_block_automatic_download(harness_factory) -> None:
    h = await harness_factory(sync_pull_interval=0, sync_push_delay=60, sync_push_max_delay=600)
    h.sign_in()
    await first_call(h)
    await h.call("add_notes", notes=[basic("not yet on AnkiWeb")])
    h.fake.script = [SyncOutput.FULL_SYNC]
    msg = await h.fails("find_notes", query="")
    block = full_sync_block(msg)
    assert block["answer"] == "FULL_SYNC"
    assert block["unsynced_changes_on_server"] is True
    assert block["safe_to_download"] is False and block["ankiweb_empty"] is False
    assert "only after they confirm" in msg
    assert h.fake.calls["full_download"] == 1  # only the initial one
    status = await h.call("sync_status")
    assert status["full_sync"]["safe_to_download"] is False
    # the user confirms: forced download, then the block is gone
    await h.call("sync", force_download=True)
    assert "full_sync" not in await h.call("sync_status")


async def test_local_schema_change_blocks_automatic_download(harness_factory) -> None:
    h = await harness_factory(sync_pull_interval=0)
    h.sign_in()
    await first_call(h)
    user = h.user()
    with user.lock:
        user.collection().set_schema_modified()
    h.fake.script = [SyncOutput.FULL_SYNC]
    block = full_sync_block(await h.fails("find_notes", query=""))
    assert block["local_schema_changed"] is True and block["safe_to_download"] is False
    assert h.fake.calls["full_download"] == 1


async def test_empty_ankiweb_is_never_handled_automatically(harness_factory) -> None:
    h = await harness_factory(sync_pull_interval=0)
    h.sign_in()
    await first_call(h)
    h.fake.script = [SyncOutput.FULL_UPLOAD]
    msg = await h.fails("find_notes", query="")
    block = full_sync_block(msg)
    assert block["ankiweb_empty"] is True and block["safe_to_download"] is False
    assert "reset on purpose" in msg
    assert h.fake.calls["full_download"] == 1 and h.fake.uploads.count(True) == 0


# ---------------------------------------------------------------- schema protocol


async def test_schema_change_cancelled_when_presync_needs_full(h: Harness) -> None:
    await first_call(h)
    for answer in (SyncOutput.FULL_SYNC, network_error()):
        h.fake.script = [answer]
        msg = await h.fails(
            "edit_note_type_schema",
            name="Basic",
            operations=[{"op": "add_field", "name": "Extra"}],
            dry_run=False,
        )
        assert "Cancelled, nothing was changed" in msg
        fields = [f["name"] for f in (await h.call("get_note_type", name="Basic"))["fields"]]
        assert fields == ["Front", "Back"]
    assert h.fake.uploads.count(True) == 0
    assert not h.user().backups_dir.exists() or not list(h.user().backups_dir.iterdir())


async def test_backup_before_upload_and_rotation(harness_factory) -> None:
    h = await harness_factory(backup_keep=2)
    h.sign_in()
    await first_call(h)
    seen_at_upload: list[int] = []
    original = h.fake.full_sync

    def full_sync(col, auth, *, upload):
        if upload:
            seen_at_upload.append(len(list(h.user().backups_dir.glob("*.anki2"))))
        return original(col, auth, upload=upload)

    h.fake.full_sync = full_sync  # type: ignore[method-assign]
    for i in range(3):
        res = await h.call(
            "edit_note_type_schema",
            name="Basic",
            operations=[{"op": "add_field", "name": f"F{i}"}],
            dry_run=False,
        )
        assert res["sync"]["backup"].endswith("-schema.anki2")
    assert seen_at_upload == [1, 2, 2]
    backups = sorted(h.user().backups_dir.glob("*.anki2"))
    assert len(backups) == 2
    assert backups[-1].stat().st_size > 0


async def test_failed_upload_sets_pending_flag_that_blocks_sync(h: Harness) -> None:
    await first_call(h)
    h.fake.full_sync_error = network_error()
    res = await h.call(
        "edit_note_type_schema",
        name="Basic",
        operations=[{"op": "add_field", "name": "Extra"}],
        dry_run=False,
    )
    assert res["sync"]["full_upload"].startswith("failed")
    user = h.user()
    assert user.state.pending_schema_upload
    calls = h.fake.calls["sync"]

    added = await h.call("add_notes", notes=[basic("while pending")])
    assert any("one-way upload" in w for w in added["warnings"])
    await asyncio.sleep(0.6)  # push delay passes
    assert h.fake.calls["sync"] == calls  # normal sync stays blocked
    status = await h.call("sync_status")
    assert status["pending_schema_upload"] is True
    msg = await h.fails(
        "edit_note_type_schema",
        name="Basic",
        operations=[{"op": "add_field", "name": "Another"}],
        dry_run=False,
    )
    assert "one-way upload" in msg

    h.fake.full_sync_error = None
    res = await h.call("sync")
    assert "pending one-way upload completed" in res["messages"][0]
    assert not user.state.pending_schema_upload
    assert h.fake.uploads.count(True) == 2  # failed attempt + retry


async def test_schema_switch_off_removes_tools(harness_factory) -> None:
    h = await harness_factory(allow_schema_changes=False)
    h.sign_in()
    names = await h.tool_names()
    for tool in ("edit_note_type_schema", "delete_note_type", "change_note_type"):
        assert tool not in names
    assert {"update_note_type", "create_note_type"} <= names
    user = h.user()
    with pytest.raises(sync_module.SchemaChangesDisabled):
        await asyncio.to_thread(lambda: h.app.sync.schema_change_locked(user, lambda col: {}))
    assert h.fake.uploads.count(True) == 0


# ---------------------------------------------------------------- errors and backoff


async def test_exponential_backoff(h: Harness) -> None:
    await first_call(h)
    await h.call("add_notes", notes=[basic("x")])
    user = h.user()
    delays = []
    for _ in range(9):
        h.fake.script = [network_error()]
        with pytest.raises(SyncUnavailable):
            await asyncio.to_thread(h.app.sync.push_now, user)
        delays.append(round(user.state.backoff_until - time.time()))
    expected = [min(BACKOFF_BASE * 2**i, BACKOFF_MAX) for i in range(9)]
    assert delays == [round(e) for e in expected]
    status = await h.call("sync_status")
    assert "next_attempt_after" in status and "connection refused" in status["last_error"]

    # during backoff, tools keep working on the local copy without contacting AnkiWeb
    user.state.last_sync = 0
    calls = h.fake.calls["sync"]
    res = await h.call("find_notes", query="")
    assert h.fake.calls["sync"] == calls
    assert "unreachable" in res["warnings"][0]

    # the push loop retries after the backoff and recovers
    user.state.backoff_until = time.time() + 0.2
    user.state.failures = 1
    h.app.sync._schedule_push(user.id)
    await wait_for(lambda: not user.state.dirty, timeout=5)
    assert user.state.failures == 0


async def test_rejected_login_asks_to_reconnect(h: Harness) -> None:
    await first_call(h)
    h.user().state.last_sync = 0
    h.fake.script = [SyncError("bad hkey", None, None, None, SyncErrorKind.AUTH)]
    msg = await h.fails("find_notes", query="")
    assert "Reconnect the connector" in msg
    assert h.user().state.auth_invalid
    assert "Reconnect" in await h.fails("find_notes", query="")


async def test_new_endpoint_is_saved(harness_factory) -> None:
    h = await harness_factory(sync_pull_interval=0)
    h.sign_in()
    await first_call(h)
    h.fake.script = []
    original = h.fake.sync_collection

    def with_endpoint(col, auth):
        out = original(col, auth)
        out.new_endpoint = "https://sync9.ankiweb.net/"
        return out

    h.fake.sync_collection = with_endpoint  # type: ignore[method-assign]
    await h.call("find_notes", query="")
    assert h.user().load_auth().endpoint == "https://sync9.ankiweb.net/"


# ---------------------------------------------------------------- media


async def test_media_sync_waits_and_runs_once(h: Harness) -> None:
    await first_call(h)
    h.fake.media_active_polls = 3
    res = await h.call("sync")
    assert res["media"] == "done"
    h.fake.media_active_polls = 1000
    h.fake.start_media_sync(None, None)  # type: ignore[arg-type]
    started = h.fake.media_started
    user = h.user()
    with user.lock:
        assert h.app.sync.start_media_sync_locked(user) == "already running"
    assert h.fake.media_started == started


async def test_media_wait_gives_up_after_timeout(h: Harness, monkeypatch) -> None:
    await first_call(h)
    monkeypatch.setattr(sync_module, "MEDIA_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(sync_module, "MEDIA_POLL_SECONDS", 0.05)
    h.fake.media_active_polls = 10_000
    h.fake.start_media_sync(None, None)  # type: ignore[arg-type]
    user = h.user()
    assert "background" in await asyncio.to_thread(h.app.sync.wait_media, user, 0.3)

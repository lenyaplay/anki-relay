"""Logging (REQ-004): JSON file, call context, redaction, rotation, debug bundle."""

from __future__ import annotations

import json
import logging
import tarfile
from pathlib import Path

import pytest

from anki_relay.debug_bundle import build_bundle
from anki_relay.logging_setup import (
    LOG_FILE,
    AccessLogFilter,
    redact,
    redact_args,
    remove_handlers,
    setup_logging,
    summarize_result,
)
from anki_relay.server import main
from anki_relay.users import user_id_for

from .conftest import EMAIL, EMAIL_B, PASSWORD, Harness, make_settings, mcp_session
from .test_oauth import Flow
from .test_tools import PNG

FIELD_SECRET = "FIELD-CONTENT-7f3a"
TEMPLATE_SECRET = "TEMPLATE-TEXT-91bd"


@pytest.fixture
def logs(tmp_path: Path):
    settings = make_settings(tmp_path, log_dir=tmp_path / "logs", log_retention_days=3)
    handlers = setup_logging(settings)
    yield settings.log_dir / LOG_FILE
    remove_handlers(handlers)


def records(path: Path) -> list[dict]:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---------------------------------------------------------------- the key guarantee


async def test_no_secrets_or_card_content_in_log_file(logs: Path, live_factory) -> None:
    server = live_factory(log_dir=logs.parent)
    flow = Flow(server)
    tokens = flow.tokens(EMAIL)
    refreshed = flow.refresh(tokens["refresh_token"]).json()
    token = refreshed["access_token"]
    async with mcp_session(server.url, token) as (session, _):
        await session.call_tool(
            "add_notes",
            {
                "notes": [
                    {
                        "deck": "LogDeck",
                        "note_type": "Basic",
                        "fields": {"Front": FIELD_SECRET, "Back": FIELD_SECRET + "-back"},
                        "tags": ["logtag"],
                    }
                ]
            },
        )
        await session.call_tool("find_notes", {"query": "deck:LogDeck"})
        await session.call_tool(
            "preview_card",
            {"note_type": "Basic", "fields": {"Front": FIELD_SECRET}, "front": TEMPLATE_SECRET},
        )
        await session.call_tool(
            "create_note_type",
            {
                "name": "LogType",
                "fields": ["Word"],
                "templates": [{"name": "C", "front": "{{Word}}" + TEMPLATE_SECRET}],
                "css": TEMPLATE_SECRET,
            },
        )
        await session.call_tool("add_media", {"filename": "dot.png", "data_base64": PNG})
        await session.call_tool("get_note_type", {"name": "Missing"})
    server.stop()

    text = logs.read_text(encoding="utf-8")
    forbidden = {
        "password": PASSWORD,
        "email": EMAIL,
        "access token": tokens["access_token"],
        "refreshed access token": token,
        "refresh token": tokens["refresh_token"],
        "new refresh token": refreshed["refresh_token"],
        "hkey": "hkey-" + EMAIL,
        "field content": FIELD_SECRET,
        "template text": TEMPLATE_SECRET,
        "base64 data": PNG[:40],
    }
    leaks = [name for name, value in forbidden.items() if value in text]
    assert not leaks, leaks

    entries = records(logs)
    calls = [e for e in entries if e.get("event") == "tool.call"]
    add = next(e for e in calls if e["tool"] == "add_notes")
    assert add["user"] == user_id_for(EMAIL) and add["outcome"] == "ok"
    note = add["arguments"]["notes"][0]
    assert note["deck"] == "LogDeck" and note["tags"] == ["logtag"]
    assert note["fields"] == {"Front": f"<{len(FIELD_SECRET)} chars>", "Back": "<23 chars>"}
    assert add["result"]["added"] == 1
    find = next(e for e in calls if e["tool"] == "find_notes")
    assert find["arguments"]["query"] == "deck:LogDeck"
    failed = next(e for e in calls if e["tool"] == "get_note_type")
    assert failed["outcome"] == "error" and "Unknown note type" in failed["error"]
    events = {e.get("event") for e in entries}
    assert {"auth.login", "auth.token", "auth.refresh", "sync.normal"} <= events


async def test_sync_events_carry_the_call_id(logs: Path, harness_factory) -> None:
    h: Harness = await harness_factory(sync_pull_interval=0)
    h.sign_in()
    await h.call("find_notes", query="")
    entries = records(logs)
    call = next(e for e in entries if e.get("event") == "tool.call")
    sync = [e for e in entries if e.get("event") == "sync.normal"]
    assert sync and all(e["call_id"] == call["call_id"] for e in sync)
    assert sync[0]["user"] == user_id_for(EMAIL) and sync[0]["reason"] == "pull"
    assert sync[0]["required"] == "FULL_DOWNLOAD"


async def test_push_is_not_attributed_to_a_tool_call(logs: Path, harness_factory) -> None:
    h: Harness = await harness_factory(sync_push_delay=0.1, sync_push_max_delay=0.5)
    h.sign_in()
    await h.call("add_notes", notes=[{"deck": "P", "note_type": "Basic", "fields": {"Front": "x"}}])
    from .conftest import wait_for

    await wait_for(lambda: not h.user().state.dirty)
    push = [e for e in records(logs) if e.get("reason") == "push"]
    assert push and "call_id" not in push[0] and push[0]["user"] == user_id_for(EMAIL)


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"query": "deck:A tag:x"}, {"query": "deck:A tag:x"}),
        (
            {"fields": {"Front": "secret", "Back": ""}},
            {"fields": {"Front": "<6 chars>", "Back": "<0 chars>"}},
        ),
        ({"fields": ["Front", "Back"]}, {"fields": ["Front", "Back"]}),
        ({"data_base64": "QUJD"}, {"data_base64": "<4 chars>"}),
        ({"css": ".card{}", "front": "{{A}}"}, {"css": "<7 chars>", "front": "<5 chars>"}),
        (
            {"url": "https://u:p@cdn.example.com/a/b.png?token=abc#x"},
            {"url": "https://cdn.example.com/a/b.png"},
        ),
        ({"note_ids": list(range(10))}, {"note_ids": {"count": 10, "first": [0, 1, 2, 3, 4]}}),
        ({"tags": ["a", "b"], "dry_run": False}, {"tags": ["a", "b"], "dry_run": False}),
        (
            {"operations": [{"op": "add_template", "name": "T", "front": "abc", "back": "de"}]},
            {
                "operations": [
                    {"op": "add_template", "name": "T", "front": "<3 chars>", "back": "<2 chars>"}
                ]
            },
        ),
    ],
)
def test_redact_args(args: dict, expected: dict) -> None:
    assert redact_args(args) == expected


def test_redact_args_cuts_long_lists() -> None:
    notes = [{"deck": "D", "fields": {"Front": "x"}}] * 30
    out = redact_args({"notes": notes})["notes"]
    assert len(out) == 21 and out[-1] == "<10 more>"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("POST password=hunter22&email=x", "hunter22"),
        ('{"refresh_token": "rt-123456"}', "rt-123456"),
        ("hkey=deadbeefcafe", "deadbeefcafe"),
        ("/callback?code=XyZ123&state=abc", "XyZ123"),
    ],
)
def test_redact_scrubs_secret_shapes(text: str, secret: str) -> None:
    assert secret not in redact(text)
    assert "[redacted]" in redact(text)


def test_access_log_loses_query_string() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/login?req=SECRETREQ", "1.1", 200),
        None,
    )
    AccessLogFilter().filter(record)
    assert "SECRETREQ" not in record.getMessage() and "/login" in record.getMessage()


@pytest.mark.parametrize(("status", "kept"), [(200, False), (503, True)])
def test_healthcheck_probes_are_not_logged(status: int, kept: bool) -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "GET", "/health", "1.1", status),
        None,
    )
    assert AccessLogFilter().filter(record) is kept


def test_json_lines_skip_uvicorn_color_duplicate() -> None:
    from anki_relay.logging_setup import JsonFormatter

    record = logging.LogRecord("uvicorn.error", logging.INFO, "", 0, "started", (), None)
    record.color_message = "\x1b[36mstarted\x1b[0m"
    assert "color_message" not in json.loads(JsonFormatter().format(record))


def test_summarize_result_keeps_only_numbers() -> None:
    summary = summarize_result(
        json.dumps(
            {
                "added": 2,
                "failed": 1,
                "deck": "Name",
                "results": [{"error": "x"}, {}],
                "warnings": ["w"],
                "note_id": 5,
                "dry_run": True,
            }
        )
    )
    assert summary == {"added": 2, "failed": 1, "dry_run": True, "item_errors": 1, "warnings": 1}


# ---------------------------------------------------------------- files


def test_rotation_keeps_retention_days(logs: Path) -> None:
    handler = next(
        h for h in logging.getLogger().handlers if getattr(h, "baseFilename", None) == str(logs)
    )
    assert handler.when == "MIDNIGHT" and handler.backupCount == 3 and handler.utc
    for day in range(1, 7):
        (logs.parent / f"{LOG_FILE}.2026-09-0{day}").write_text("{}\n")
    handler.doRollover()
    rotated = sorted(p.name for p in logs.parent.glob(LOG_FILE + ".*"))
    assert len(rotated) == 3, rotated


def test_log_dir_empty_means_stdout_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, log_dir="")
    assert settings.log_dir is None
    handlers = setup_logging(settings)
    try:
        assert not any(hasattr(h, "baseFilename") for h in handlers)
    finally:
        remove_handlers(handlers)


async def test_debug_bundle(logs: Path, harness_factory, monkeypatch, capsys) -> None:
    h: Harness = await harness_factory(log_dir=logs.parent)
    h.sign_in(EMAIL)
    h.sign_in(EMAIL_B)
    await h.call(
        "add_notes", notes=[{"deck": "B", "note_type": "Basic", "fields": {"Front": FIELD_SECRET}}]
    )
    tokens = h.app.provider._issue("client", ["anki"], user_id_for(EMAIL), None)
    await h.call(
        "edit_note_type_schema",
        name="Basic",
        operations=[{"op": "add_field", "name": "X"}],
        dry_run=False,
    )

    path = build_bundle(h.settings)
    with tarfile.open(path) as tar:
        names = set(tar.getnames())
        blob = b"".join(tar.extractfile(m).read() for m in tar.getmembers() if m.isfile())  # type: ignore[union-attr]
    uid = user_id_for(EMAIL)
    assert {"system.json", f"users/{uid}.json", f"logs/{LOG_FILE}"} <= names
    text = blob.decode("utf-8", errors="replace")
    for secret in (
        EMAIL,
        EMAIL_B,
        "hkey-",
        tokens.access_token,
        tokens.refresh_token,
        FIELD_SECRET,
    ):
        assert secret not in text, secret
    with tarfile.open(path) as tar:
        system = json.load(tar.extractfile("system.json"))  # type: ignore[arg-type]
        user = json.load(tar.extractfile(f"users/{uid}.json"))  # type: ignore[arg-type]
    assert (
        system["config"]["allowed_users"]["count"] == 2 and "allowed_emails" not in system["config"]
    )
    assert system["versions"]["anki"] == "26.9.3"
    assert user["has_ankiweb_login"] is True and user["allowed"] is True
    assert user["tokens"]["access"] == 1 and user["backups"]
    assert user["state"]["ever_synced"] is True

    # the CLI entry point
    monkeypatch.setenv("PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("ALLOWED_EMAILS", EMAIL)
    monkeypatch.setenv("DATA_DIR", str(h.settings.data_dir))
    monkeypatch.setenv("LOG_DIR", str(logs.parent))
    main(["debug-bundle", "--days", "1"])
    printed = Path(capsys.readouterr().out.strip())
    assert printed.exists() and printed.parent == logs.parent

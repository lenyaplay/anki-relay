"""Every tool on a temporary collection, batch semantics, errors, switches, real MCP client."""

from __future__ import annotations

import base64
import json

import pytest

from anki_relay.config import ConfigError, load_settings
from anki_relay.server import build_app
from anki_relay.users import user_id_for

from .conftest import EMAIL, Harness, make_settings, mcp_session

PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
    )
).decode()


def basic(front: str, back: str = "", deck: str = "Default", tags=None) -> dict:
    note = {"deck": deck, "note_type": "Basic", "fields": {"Front": front, "Back": back}}
    if tags is not None:
        note["tags"] = tags
    return note


async def add(h: Harness, *notes: dict) -> list[int]:
    res = await h.call("add_notes", notes=list(notes))
    return [r["note_id"] for r in res["results"] if "note_id" in r]


async def card_ids(h: Harness, query: str = "") -> list[int]:
    res = await h.call("find_cards", query=query, limit=1000)
    return [c["id"] for c in res["cards"]]


# ---------------------------------------------------------------- every tool


async def test_every_tool_is_called(h: Harness) -> None:
    called: set[str] = set()

    async def call(tool: str, **args):
        called.add(tool)
        return await h.call(tool, **args)

    overview = await call("collection_overview")
    assert {"decks", "note_types", "notes", "cards", "tags"} <= overview.keys()

    res = await call(
        "add_notes",
        notes=[
            basic("cat", "Katze", "Lang::German", ["animal"]),
            basic("dog", "Hund", "Lang::German"),
        ],
    )
    nids = [r["note_id"] for r in res["results"]]
    assert res["added"] == 2

    assert (await call("find_notes", query="deck:Lang::German"))["total"] == 2
    cids = [c["id"] for c in (await call("find_cards", query="deck:Lang::German"))["cards"]]
    assert len(cids) == 2
    got = await call("get_notes", note_ids=nids)
    assert got["notes"][0]["fields"]["Front"] == "cat"
    info = await call("card_info", card_ids=cids[:1])
    assert info["cards"][0]["queue"] == "new"
    assert "due_today" in await call("stats")
    assert (await call("stats", deck="Lang"))["scope"] == "Lang"

    upd = await call(
        "update_notes", updates=[{"note_id": nids[0], "fields": {"Back": "die Katze"}}]
    )
    assert upd["updated"] == 1
    assert (await call("get_notes", note_ids=[nids[0]]))["notes"][0]["fields"] == {
        "Front": "cat",
        "Back": "die Katze",
    }

    assert (await call("move_cards", card_ids=cids, deck="Lang::Other"))["cards"] == 2
    assert (await call("set_suspended", card_ids=cids[:1], suspended=True))["cards"] == 1
    assert (await call("set_suspended", card_ids=cids[:1], suspended=False))["cards"] == 1
    assert (await call("set_buried", card_ids=cids[:1], buried=True))["cards"] == 1
    assert (await call("set_buried", card_ids=cids[:1], buried=False))["cards"] == 1
    await call("set_due_date", card_ids=cids[:1], days="3")
    assert (await call("find_cards", query="prop:due=3"))["total"] == 1
    assert (await call("forget_cards", card_ids=cids[:1], dry_run=False))["reset"] == 1

    assert (await call("create_deck", name="Tmp::Sub"))["created"] is True
    assert (await call("rename_deck", name="Tmp::Sub", new_name="Tmp::Renamed"))["deck"] == (
        "Tmp::Renamed"
    )
    opts = await call("get_deck_options", deck="Tmp::Renamed")
    assert opts["options"]["new"]["perDay"] == 20
    up = await call(
        "update_deck_options",
        deck="Tmp::Renamed",
        changes={"new": {"perDay": 5}},
        scope="new_preset",
    )
    assert up["preset"] != "Default"
    assert (await call("get_deck_options", deck="Tmp::Renamed"))["options"]["new"]["perDay"] == 5
    assert (await call("get_deck_options", deck="Default"))["options"]["new"]["perDay"] == 20
    assert (await call("delete_deck", name="Tmp", dry_run=False))["deleted"] is True

    await call(
        "create_note_type",
        name="Vocab",
        fields=["Word", "Meaning"],
        templates=[
            {"name": "Recognition", "front": "{{Word}}", "back": "{{FrontSide}}<hr>{{Meaning}}"}
        ],
    )
    assert (await call("get_note_type", name="Vocab"))["fields"][0]["name"] == "Word"
    await call("clone_note_type", name="Vocab", new_name="Vocab Copy")
    await call("update_note_type", name="Vocab Copy", css=".card{color:red}")
    await call("rename_note_type", name="Vocab Copy", new_name="Vocab Draft")
    prev = await call(
        "preview_card", note_type="Vocab", fields={"Word": "Haus", "Meaning": "house"}
    )
    assert "Haus" in prev["question"] and "house" in prev["answer"]
    sch = await call(
        "edit_note_type_schema",
        name="Vocab Draft",
        operations=[{"op": "add_field", "name": "Audio"}],
        dry_run=False,
    )
    assert sch["sync"]["full_upload"] == "done"
    vocab_note = await add(h, {"deck": "Default", "note_type": "Vocab", "fields": {"Word": "a"}})
    ch = await call(
        "change_note_type",
        note_ids=vocab_note,
        new_note_type="Vocab Draft",
        field_map={"Word": "Word", "Meaning": "Meaning"},
        dry_run=False,
    )
    assert ch["changed"] == 1
    dl = await call("delete_note_type", name="Vocab", dry_run=False)
    assert dl["deleted"] is True

    assert (await call("add_tags", note_ids=nids, tags=["verb", "lang::de"]))["notes_changed"] == 2
    assert (await call("remove_tags", note_ids=nids, tags=["verb"]))["notes_changed"] == 2
    tags = {t["tag"]: t["notes"] for t in (await call("list_tags"))["tags"]}
    assert tags["lang::de"] == 2 and tags["animal"] == 1
    assert (await call("rename_tag", old="lang", new="language"))["notes_changed"] == 2
    await call("remove_tags", note_ids=nids, tags=["animal"])
    assert (await call("clear_unused_tags"))["removed"] >= 1

    media = await call("add_media", filename="dot.png", data_base64=PNG)
    assert media["field"] == '<img src="dot.png">'
    assert (await call("list_media", pattern="*.png"))["files"][0]["filename"] == "dot.png"
    assert "unused" in await call("check_media")
    assert (await call("delete_media", filenames=["dot.png"], dry_run=False))["deleted"] == 1

    assert (await call("delete_notes", note_ids=nids, dry_run=False))["deleted"] == 2
    assert "media" in await call("sync_status")
    assert (await call("sync"))["synced"] is True

    assert called == set(h.app.registry.registered), set(h.app.registry.registered) - called


# ---------------------------------------------------------------- add_notes


async def test_add_notes_mixed_batch_reports_each_item(h: Harness) -> None:
    await add(h, basic("existing"))
    res = await h.call(
        "add_notes",
        notes=[
            basic("one", deck="A"),
            {"deck": "B::C", "note_type": "Cloze", "fields": {"Text": "{{c1::Paris}} is French"}},
            basic("existing"),  # duplicate
            basic(""),  # empty first field
            {"deck": "A", "note_type": "Cloze", "fields": {"Text": "no deletion"}},
            basic("{{c1::x}} in Basic"),  # cloze deletion in a non-cloze type
            {"deck": "A", "note_type": "Nope", "fields": {"Front": "x"}},
            {"deck": "A", "note_type": "Basic", "fields": {"Wrong": "x"}},
            basic("one", deck="A"),  # duplicate within the same batch
        ],
    )
    results = res["results"]
    assert res["added"] == 2
    assert "note_id" in results[0] and "note_id" in results[1]
    assert "duplicate" in results[2]["error"]
    assert "empty" in results[3]["error"]
    assert "no cloze deletion" in results[4]["error"]
    assert "not a cloze type" in results[5]["error"]
    assert "Unknown note type" in results[6]["error"] and "'Basic'" in results[6]["error"]
    assert "Unknown field" in results[7]["error"] and "'Front'" in results[7]["error"]
    assert "duplicate" in results[8]["error"]
    decks = {d["name"] for d in (await h.call("collection_overview"))["decks"]}
    assert {"A", "B", "B::C"} <= decks


async def test_add_notes_allow_duplicates(h: Harness) -> None:
    await add(h, basic("same"))
    res = await h.call("add_notes", notes=[basic("same")], allow_duplicates=True)
    assert res["added"] == 1


async def test_add_notes_respects_max_batch(harness_factory) -> None:
    h = await harness_factory(max_batch=2)
    h.sign_in()
    msg = await h.fails("add_notes", notes=[basic("a"), basic("b"), basic("c")])
    assert "MAX_BATCH" in msg


async def test_update_notes_partial_and_per_item(h: Harness) -> None:
    (nid,) = await add(h, basic("front", "back", tags=["t1"]))
    res = await h.call(
        "update_notes",
        updates=[
            {"note_id": nid, "tags": ["t2"]},
            {"note_id": 123, "fields": {"Front": "x"}},
            {"note_id": nid, "fields": {"Nope": "x"}},
        ],
    )
    assert res["updated"] == 1
    assert "not found" in res["results"][1]["error"]
    assert "'Back'" in res["results"][2]["error"]
    note = (await h.call("get_notes", note_ids=[nid]))["notes"][0]
    assert note["fields"] == {"Front": "front", "Back": "back"} and note["tags"] == ["t2"]


# ---------------------------------------------------------------- errors


async def test_unknown_names_list_valid_options(h: Harness) -> None:
    assert "'Basic'" in await h.fails("get_note_type", name="Basik")
    assert "'Default'" in await h.fails("get_deck_options", deck="Nope")
    assert "'Card 1'" in await h.fails(
        "preview_card", note_type="Basic", fields={"Front": "x"}, template="Card 9"
    )
    assert "'Front'" in await h.fails("preview_card", note_type="Basic", fields={"Fornt": "x"})
    msg = await h.fails(
        "update_note_type", name="Basic", templates=[{"name": "Nope", "front": "{{Front}}"}]
    )
    assert "'Card 1'" in msg


async def test_shared_deck_options_need_scope(h: Harness) -> None:
    await h.call("create_deck", name="One")
    await h.call("create_deck", name="Two")
    msg = await h.fails("update_deck_options", deck="One", changes={"new": {"perDay": 7}})
    assert "Two" in msg and "Default" in msg and "scope" in msg
    assert (await h.call("get_deck_options", deck="Two"))["options"]["new"]["perDay"] == 20
    res = await h.call(
        "update_deck_options", deck="One", changes={"new": {"perDay": 7}}, scope="shared"
    )
    assert set(res["affected_decks"]) >= {"One", "Two", "Default"}
    assert (await h.call("get_deck_options", deck="Two"))["options"]["new"]["perDay"] == 7
    bad = await h.fails("update_deck_options", deck="One", changes={"nwe": 1}, scope="shared")
    assert "Unknown option" in bad and "new" in bad


async def test_dangerous_tools_default_to_dry_run(h: Harness) -> None:
    nids = await add(h, basic("keep me", deck="Danger"), basic("me too", deck="Danger"))
    cids = await card_ids(h, "deck:Danger")
    await h.call("set_due_date", card_ids=cids, days="5")
    await h.call(
        "create_note_type", name="T", fields=["A"], templates=[{"name": "C", "front": "{{A}}"}]
    )
    await h.call("add_media", filename="x.png", data_base64=PNG)
    before = await h.call("collection_overview")
    sync_calls = h.fake.calls["sync"]

    dry = [
        await h.call("delete_notes", note_ids=nids),
        await h.call("delete_deck", name="Danger"),
        await h.call("forget_cards", card_ids=cids),
        await h.call("delete_note_type", name="T"),
        await h.call(
            "edit_note_type_schema",
            name="Basic",
            operations=[{"op": "remove_field", "name": "Back"}],
        ),
        await h.call(
            "change_note_type", note_ids=nids, new_note_type="T", field_map={"Front": "A"}
        ),
        await h.call("delete_media", filenames=["x.png"]),
    ]
    assert all(d["dry_run"] is True for d in dry)
    assert dry[0]["notes"] == 2 and dry[0]["cards"] == 2
    assert dry[1]["cards"] == 2
    assert dry[4]["requires_full_sync"] is True and "warning" in dry[4]
    after = await h.call("collection_overview")
    assert before == after
    assert (await h.call("find_cards", query="deck:Danger prop:due=5"))["total"] == 2
    assert (await h.call("list_media"))["total"] == 1
    assert h.fake.uploads.count(True) == 0
    assert h.fake.calls["sync"] == sync_calls  # dry runs do not trigger a schema sync


async def test_pagination(h: Harness) -> None:
    await h.call("add_notes", notes=[basic(f"n{i:02}", deck="P") for i in range(25)])
    first = await h.call("find_notes", query="deck:P", limit=10)
    assert first["total"] == 25 and first["count"] == 10 and first["next_offset"] == 10
    second = await h.call("find_notes", query="deck:P", limit=10, offset=10)
    last = await h.call("find_notes", query="deck:P", limit=10, offset=20)
    assert last["count"] == 5 and "next_offset" not in last
    ids = [n["id"] for page in (first, second, last) for n in page["notes"]]
    assert len(set(ids)) == 25
    cards = await h.call("find_cards", query="deck:P", limit=7, offset=21)
    assert cards["count"] == 4 and cards["total"] == 25
    only = await h.call("find_notes", query="deck:P", limit=1, fields=["Front"])
    assert list(only["notes"][0]["fields"]) == ["Front"]


async def test_compact_previews_strip_html(h: Harness) -> None:
    await add(h, basic("<b>bold</b>&nbsp;text<br>" + "x" * 500))
    note = (await h.call("find_notes", query=""))["notes"][0]
    assert note["fields"]["Front"].startswith("bold text")
    assert len(note["fields"]["Front"]) <= 300


# ---------------------------------------------------------------- switches & config


async def test_disabled_tools_are_not_registered(harness_factory) -> None:
    h = await harness_factory(disabled_tools="delete_notes,delete_deck")
    names = await h.tool_names()
    assert "delete_notes" not in names and "delete_deck" not in names
    assert "add_notes" in names


async def test_all_tools_enabled_by_default(h: Harness) -> None:
    assert len(await h.tool_names()) == 39


def test_unknown_disabled_tool_is_a_config_error(tmp_path) -> None:
    settings = make_settings(tmp_path, disabled_tools="delete_everything")
    with pytest.raises(ConfigError, match=r"DISABLED_TOOLS.*delete_everything"):
        build_app(settings)


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        ({"public_url": "ftp://x"}, "PUBLIC_URL"),
        ({"public_url": "http://example.com"}, "PUBLIC_URL"),
        ({"public_url": "https://example.com/mcp"}, "PUBLIC_URL"),
        ({"allowed_emails": ""}, "ALLOWED_EMAILS"),
        ({"media_max_mb": "lots"}, "MEDIA_MAX_MB"),
        ({"sync_push_delay": "-1"}, "SYNC_PUSH_DELAY"),
        ({"default_language": "de"}, "DEFAULT_LANGUAGE"),
        ({"trusted_proxies": "not-an-ip"}, "TRUSTED_PROXIES"),
        ({"media_sync": "maybe"}, "MEDIA_SYNC"),
    ],
)
def test_invalid_settings_name_the_variable(tmp_path, overrides, variable) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(tmp_path, **overrides)
    assert variable in str(info.value)


def test_missing_required_settings(monkeypatch) -> None:
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "PUBLIC_URL: is required" in str(info.value)
    assert "ALLOWED_EMAILS: is required" in str(info.value)


def test_settings_from_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PUBLIC_URL", "https://anki-relay.example.com/")
    monkeypatch.setenv("ALLOWED_EMAILS", "Me@Example.com, friend@example.com")
    monkeypatch.setenv("MEDIA_ALLOWED_EXTENSIONS", "*")
    monkeypatch.setenv("DISABLED_TOOLS", "delete_notes")
    s = load_settings(data_dir=tmp_path)
    assert s.public_url == "https://anki-relay.example.com"
    assert s.allowed_emails == ["me@example.com", "friend@example.com"]
    assert s.any_media_extension and s.disabled_tools == ["delete_notes"]


# ---------------------------------------------------------------- real MCP client


async def test_real_mcp_client_over_http(live_factory) -> None:
    server = live_factory()
    app = server.app
    user = app.users.for_email(EMAIL)
    user.save_auth("hkey", None)
    app.provider.store.users[user.id] = EMAIL
    token = app.provider._issue("client", ["anki"], user_id_for(EMAIL), None).access_token

    async with mcp_session(server.url, token) as (session, init):
        assert "collection_overview" in (init.instructions or "")
        tools = {t.name for t in (await session.list_tools()).tools}
        assert len(tools) == 39
        added = await session.call_tool("add_notes", {"notes": [basic("over http", deck="Remote")]})
        assert not added.isError
        assert json.loads(added.content[0].text)["added"] == 1  # type: ignore[union-attr]
        found = await session.call_tool("find_notes", {"query": "deck:Remote"})
        assert json.loads(found.content[0].text)["total"] == 1  # type: ignore[union-attr]
        err = await session.call_tool("get_note_type", {"name": "Missing"})
        assert err.isError and "Basic" in err.content[0].text  # type: ignore[union-attr]

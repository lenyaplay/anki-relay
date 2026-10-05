"""Users are isolated from each other."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from anki_relay.users import user_id_for

from .conftest import EMAIL, EMAIL_B, Harness, mcp_session
from .test_oauth import Flow


def test_user_id_is_sha256_prefix_of_lowercased_email() -> None:
    expected = hashlib.sha256(b"alice@example.com").hexdigest()[:16]
    assert user_id_for(" Alice@Example.COM ") == expected


def test_user_ids_cannot_escape_the_data_dir(h: Harness) -> None:
    for bad in ("../etc", "..", "", "ABC", "a/b"):
        with pytest.raises(ValueError):
            h.app.users.get(bad)


async def test_parallel_users_see_only_their_notes(h: Harness) -> None:
    async def add_many(email: str, prefix: str) -> None:
        for i in range(10):
            notes = [
                {"deck": prefix, "note_type": "Basic", "fields": {"Front": f"{prefix}-{i}-{j}"}}
                for j in range(5)
            ]
            res = await h.call("add_notes", email=email, notes=notes)
            assert res["added"] == 5

    await asyncio.gather(add_many(EMAIL, "alice"), add_many(EMAIL_B, "bob"))
    a = await h.call("find_notes", email=EMAIL, query="", limit=1000)
    b = await h.call("find_notes", email=EMAIL_B, query="", limit=1000)
    assert a["total"] == 50 and b["total"] == 50
    assert all(n["fields"]["Front"].startswith("alice") for n in a["notes"])
    assert all(n["fields"]["Front"].startswith("bob") for n in b["notes"])
    ua, ub = h.user(EMAIL), h.user(EMAIL_B)
    assert ua.dir != ub.dir and ua.col_path.exists() and ub.col_path.exists()


async def test_token_of_a_cannot_reach_b(live_factory) -> None:
    server = live_factory()
    token_a = Flow(server).tokens(EMAIL)["access_token"]
    token_b = Flow(server).tokens(EMAIL_B)["access_token"]
    async with mcp_session(server.url, token_b) as (session, _):
        await session.call_tool(
            "add_notes",
            {"notes": [{"deck": "Secret", "note_type": "Basic", "fields": {"Front": "b only"}}]},
        )
    async with mcp_session(server.url, token_a) as (session, _):
        res = await session.call_tool("find_notes", {"query": "deck:Secret"})
        assert '"total":0' in res.content[0].text  # type: ignore[union-attr]
    user_b = server.app.users.for_email(EMAIL_B)
    with user_b.lock:
        note_ids = list(user_b.collection().find_notes("deck:Secret"))
    assert note_ids
    async with mcp_session(server.url, token_a) as (session, _):
        got = await session.call_tool("get_notes", {"note_ids": note_ids})
        assert '"notes":[]' in got.content[0].text  # type: ignore[union-attr]
        deleted = await session.call_tool("delete_notes", {"note_ids": note_ids, "dry_run": False})
        assert '"notes":0' in deleted.content[0].text  # type: ignore[union-attr]
    with user_b.lock:
        assert list(user_b.collection().find_notes("deck:Secret")) == note_ids

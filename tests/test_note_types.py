"""Note type design tools and the 'changes schema' classification vs. reality."""

from __future__ import annotations

import copy
import os

import anki.collection  # noqa: F401
import pytest
from anki.collection import Collection
from anki.sync import SyncAuth, SyncOutput

from anki_relay.tools.note_types import SCHEMA_OPS, apply_schema_ops

from .conftest import Harness


def mark_synced(col: Collection) -> None:
    col.db.execute("update col set ls = scm + 1")
    assert not col.schema_changed()


def make_type(col: Collection, name: str, with_note: bool) -> dict:
    mm = col.models
    nt = mm.new(name)
    for f in ("F1", "F2", "F3"):
        mm.add_field(nt, mm.new_field(f))
    for tname, q in (("C1", "{{F1}}"), ("C2", "{{F2}}")):
        t = mm.new_template(tname)
        t["qfmt"], t["afmt"] = q, "{{F3}}"
        mm.add_template(nt, t)
    mm.add(nt)
    if with_note:
        note = col.new_note(mm.by_name(name))
        note["F1"], note["F2"], note["F3"] = "a", "b", "c"
        col.add_note(note, 1)
    return mm.by_name(name)


OPS = {
    "add_field": {"op": "add_field", "name": "New"},
    "rename_field": {"op": "rename_field", "name": "F2", "new_name": "F2r"},
    "remove_field": {"op": "remove_field", "name": "F3"},
    "reposition_field": {"op": "reposition_field", "name": "F3", "position": 1},
    "set_sort_field": {"op": "set_sort_field", "name": "F2"},
    "add_template": {"op": "add_template", "name": "C3", "front": "{{F3}}", "back": "x"},
    "rename_template": {"op": "rename_template", "name": "C2", "new_name": "C2r"},
    "remove_template": {"op": "remove_template", "name": "C2"},
    "reposition_template": {"op": "reposition_template", "name": "C2", "position": 1},
}


@pytest.fixture
def col(tmp_path):
    os.makedirs(tmp_path / "c")
    c = Collection(str(tmp_path / "c" / "collection.anki2"))
    yield c
    c.close()


def test_every_schema_op_is_classified() -> None:
    assert set(OPS) == set(SCHEMA_OPS)


@pytest.mark.parametrize("with_note", [True, False])
@pytest.mark.parametrize("op", sorted(OPS))
def test_schema_classification_matches_anki(col, op: str, with_note: bool) -> None:
    nt = make_type(col, f"T-{op}-{with_note}", with_note)
    mark_synced(col)
    apply_schema_ops(col, nt, [OPS[op]])  # type: ignore[list-item]
    col.models.update_dict(nt)
    assert col.schema_changed() == SCHEMA_OPS[op], op


def test_non_schema_note_type_edits_keep_schema(col) -> None:
    mm = col.models
    nt = make_type(col, "Plain", with_note=True)
    mark_synced(col)
    # create, clone, css, template text, field options, rename: what the non-schema tools do
    make_type(col, "Another", with_note=False)
    clone = mm.copy(nt, add=False)
    clone["name"] = "Plain copy"
    mm.add(clone)
    nt = mm.by_name("Plain")
    nt["css"] = ".card{}"
    nt["tmpls"][0]["qfmt"] = "{{F1}} {{F2}}"
    nt["flds"][0].update({"font": "Georgia", "size": 30, "description": "hint", "rtl": True})
    nt["name"] = "Plain renamed"
    mm.update_dict(nt)
    assert not col.schema_changed()


def test_change_note_type_and_delete_mark_schema(col) -> None:
    from anki_relay.tools.notes import change_note_type_plan

    make_type(col, "Src", with_note=True)
    mark_synced(col)
    nids = list(col.find_notes('"note:Src"'))
    req, plan = change_note_type_plan(col, nids, "Basic", {"F1": "Front", "F2": "Back"}, None)
    assert plan["old_fields_dropped"] == ["F3"]
    col.models.change_notetype_of_notes(req)
    assert col.schema_changed()

    make_type(col, "Gone", with_note=False)
    mark_synced(col)
    col.models.remove(col.models.by_name("Gone")["id"])
    assert col.schema_changed()


# ---------------------------------------------------------------- through the tools


async def no_full_sync_needed(h: Harness) -> None:
    """The next sync after the operation is a normal one."""
    user = h.user()
    with user.lock:
        out = h.fake.sync_collection(user.collection(), SyncAuth(hkey="x"))
    assert out.required == SyncOutput.NO_CHANGES


async def test_create_clone_update_preview(h: Harness) -> None:
    await h.call("collection_overview")
    created = await h.call(
        "create_note_type",
        name="Vocab",
        fields=["Word", "Meaning", "Example"],
        templates=[
            {"name": "Recognise", "front": "{{Word}}", "back": "{{FrontSide}}<hr>{{Meaning}}"},
            {"name": "Recall", "front": "{{Meaning}}", "back": "{{Word}}"},
        ],
        css=".card{font-size:20px}",
    )
    assert created["kind"] == "standard" and len(created["templates"]) == 2
    assert created["css"] == ".card{font-size:20px}"
    await no_full_sync_needed(h)

    await h.call("clone_note_type", name="Vocab", new_name="Vocab 2")
    await h.call(
        "update_note_type",
        name="Vocab 2",
        css=".card{color:blue}",
        templates=[{"name": "Recall", "back": "{{FrontSide}}<hr>{{Word}}"}],
        field_options={"Word": {"font": "Georgia", "size": 28, "description": "the word"}},
    )
    nt = await h.call("get_note_type", name="Vocab 2")
    assert nt["css"] == ".card{color:blue}"
    assert nt["templates"][1] == {
        "name": "Recall",
        "front": "{{Meaning}}",
        "back": "{{FrontSide}}<hr>{{Word}}",
    }
    assert nt["fields"][0]["font"] == "Georgia" and nt["fields"][0]["description"] == "the word"
    await no_full_sync_needed(h)
    bad = await h.fails("update_note_type", name="Vocab 2", field_options={"Word": {"colour": 1}})
    assert "font" in bad

    await h.call("rename_note_type", name="Vocab 2", new_name="Vocab B")
    await no_full_sync_needed(h)
    assert h.fake.uploads.count(True) == 0

    prev = await h.call(
        "preview_card",
        note_type="Vocab",
        fields={"Word": "Haus", "Meaning": "house"},
        template="Recall",
    )
    assert "house" in prev["question"] and "Haus" in prev["answer"]
    trial = await h.call(
        "preview_card",
        note_type="Vocab",
        fields={"Word": "Haus", "Example": "Das Haus ist alt."},
        front="<i>{{Word}}</i> — {{Example}}",
    )
    assert "<i>Haus</i> — Das Haus ist alt." in trial["question"]
    assert (await h.call("get_note_type", name="Vocab"))["templates"][0]["front"] == "{{Word}}"
    cloze = await h.call(
        "preview_card", note_type="Cloze", fields={"Text": "{{c1::a}} {{c2::b}}"}, template="c2"
    )
    assert "[...]" in cloze["question"] and "a" in cloze["question"]


async def test_create_cloze_note_type(h: Harness) -> None:
    res = await h.call(
        "create_note_type",
        name="My Cloze",
        fields=["Text", "Extra"],
        templates=[
            {"name": "Cloze", "front": "{{cloze:Text}}", "back": "{{cloze:Text}}<br>{{Extra}}"}
        ],
        is_cloze=True,
    )
    assert res["kind"] == "cloze"
    added = await h.call(
        "add_notes",
        notes=[{"deck": "C", "note_type": "My Cloze", "fields": {"Text": "{{c1::x}} {{c2::y}}"}}],
    )
    assert added["added"] == 1
    assert (await h.call("find_cards", query="deck:C"))["total"] == 2


async def test_create_note_type_errors(h: Harness) -> None:
    assert "already exists" in await h.fails(
        "create_note_type", name="Basic", fields=["A"], templates=[{"name": "C", "front": "{{A}}"}]
    )
    msg = await h.fails(
        "create_note_type", name="Bad", fields=["A"], templates=[{"name": "C", "front": "{{Nope}}"}]
    )
    assert "Nope" in msg


async def test_edit_schema_ops_and_protocol(h: Harness) -> None:
    await h.call(
        "add_notes",
        notes=[
            {
                "deck": "D",
                "note_type": "Basic (and reversed card)",
                "fields": {"Front": "f", "Back": "b"},
            }
        ],
    )
    name = "Basic (and reversed card)"
    dry = await h.call(
        "edit_note_type_schema",
        name=name,
        operations=[
            {"op": "add_field", "name": "Audio", "position": 2},
            {"op": "remove_template", "name": "Card 2"},
        ],
    )
    assert dry["requires_full_sync"] is True
    assert dry["operations"][1]["cards_deleted"] == 1
    assert [f["name"] for f in (await h.call("get_note_type", name=name))["fields"]] == [
        "Front",
        "Back",
    ]

    res = await h.call(
        "edit_note_type_schema",
        name=name,
        operations=[
            {"op": "add_field", "name": "Audio", "position": 2},
            {"op": "rename_field", "name": "Back", "new_name": "Answer"},
            {"op": "set_sort_field", "name": "Answer"},
            {"op": "add_template", "name": "Listen", "front": "{{Audio}}", "back": "{{Front}}"},
            {"op": "reposition_template", "name": "Listen", "position": 1},
            {"op": "remove_template", "name": "Card 2"},
        ],
        dry_run=False,
    )
    definition = res["definition"]
    assert [f["name"] for f in definition["fields"]] == ["Front", "Audio", "Answer"]
    assert definition["sort_field"] == "Answer"
    assert [t["name"] for t in definition["templates"]] == ["Listen", "Card 1"]
    assert "{{Answer}}" in definition["templates"][1]["back"]  # rename updated templates
    assert res["sync"]["full_upload"] == "done"
    assert h.fake.uploads == [False, True]  # initial download, then the one upload
    assert (await h.call("find_cards", query="deck:D"))["total"] == 1
    await no_full_sync_needed(h)


async def test_renames_only_skip_full_upload(h: Harness) -> None:
    await h.call("collection_overview")
    res = await h.call(
        "edit_note_type_schema",
        name="Basic",
        operations=[
            {"op": "rename_field", "name": "Back", "new_name": "Answer"},
            {"op": "rename_template", "name": "Card 1", "new_name": "Forward"},
        ],
        dry_run=False,
    )
    assert "sync" not in res  # no protocol needed
    assert h.fake.uploads.count(True) == 0
    assert h.user().state.dirty
    await no_full_sync_needed(h)


async def test_schema_op_errors(h: Harness) -> None:
    msg = await h.fails(
        "edit_note_type_schema", name="Basic", operations=[{"op": "remove_field", "name": "Nope"}]
    )
    assert "'Front'" in msg and "operation 0" in msg
    msg = await h.fails(
        "edit_note_type_schema",
        name="Cloze",
        operations=[{"op": "add_template", "name": "X", "front": "{{Text}}"}],
    )
    assert "cloze" in msg
    msg = await h.fails(
        "edit_note_type_schema",
        name="Basic",
        operations=[{"op": "reposition_field", "name": "Front", "position": 9}],
    )
    assert "between 1 and 2" in msg


async def test_change_note_type(h: Harness) -> None:
    await h.call(
        "create_note_type",
        name="QA",
        fields=["Question", "Answer", "Notes"],
        templates=[{"name": "Forward", "front": "{{Question}}", "back": "{{Answer}}"}],
    )
    added = await h.call(
        "add_notes",
        notes=[
            {"deck": "X", "note_type": "Basic", "fields": {"Front": f"q{i}", "Back": f"a{i}"}}
            for i in range(3)
        ],
    )
    nids = [r["note_id"] for r in added["results"]]
    dry = await h.call(
        "change_note_type",
        note_ids=nids,
        new_note_type="QA",
        field_map={"Front": "Question", "Back": "Answer"},
    )
    assert dry["dry_run"] and dry["notes"] == 3
    assert dry["fields"] == {"Question": "Front", "Answer": "Back", "Notes": None}
    res = await h.call(
        "change_note_type",
        note_ids=nids,
        new_note_type="QA",
        field_map={"Front": "Question", "Back": "Answer"},
        dry_run=False,
    )
    assert res["sync"]["full_upload"] == "done"
    note = (await h.call("get_notes", note_ids=nids[:1]))["notes"][0]
    assert note["note_type"] == "QA"
    assert note["fields"] == {"Question": "q0", "Answer": "a0", "Notes": ""}
    msg = await h.fails(
        "change_note_type",
        note_ids=nids,
        new_note_type="Basic",
        field_map={"Question": "Front", "Answer": "Front"},
    )
    assert "Two old fields" in msg


async def test_delete_note_type(h: Harness) -> None:
    await h.call(
        "create_note_type", name="Temp", fields=["A"], templates=[{"name": "C", "front": "{{A}}"}]
    )
    await h.call("add_notes", notes=[{"deck": "T", "note_type": "Temp", "fields": {"A": "x"}}])
    dry = await h.call("delete_note_type", name="Temp")
    assert dry["notes"] == 1 and dry["cards"] == 1 and dry["requires_full_sync"]
    res = await h.call("delete_note_type", name="Temp", dry_run=False)
    assert res["deleted"] and res["sync"]["full_upload"] == "done"
    assert "'Basic'" in await h.fails("get_note_type", name="Temp")


def test_dry_run_plan_does_not_touch_the_stored_type(col) -> None:
    nt = make_type(col, "Keep", with_note=True)
    before = copy.deepcopy(col.models.by_name("Keep"))
    apply_schema_ops(col, copy.deepcopy(nt), [OPS["remove_field"], OPS["add_template"]])  # type: ignore[list-item]
    assert col.models.by_name("Keep") == before

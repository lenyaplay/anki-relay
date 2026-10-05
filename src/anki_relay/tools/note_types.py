"""Note type tools: inspect, design and restructure note types."""

from __future__ import annotations

import copy
from typing import Any, Literal, Required, TypedDict

import anki.collection  # noqa: F401
from anki.collection import Collection
from mcp.server.fastmcp.exceptions import ToolError

from .runtime import (
    SCHEMA_DANGER,
    Registry,
    Result,
    field_index,
    notetype,
    template_index,
)

# Which edit_note_type_schema operations make Anki mark the schema modified (and so
# need a one-way upload). Verified against anki 26.9.3 by tests/test_note_types.py.
SCHEMA_OPS: dict[str, bool] = {
    "add_field": True,
    "rename_field": False,
    "remove_field": True,
    "reposition_field": True,
    "set_sort_field": True,
    "add_template": True,
    "rename_template": False,
    "remove_template": True,
    "reposition_template": True,
}

FIELD_KEYS_HIDDEN = {"ord", "id", "tag"}
FIELD_OPTION_KEYS_HIDDEN = FIELD_KEYS_HIDDEN | {"name"}
TEMPLATE_KEYS = {"front": "qfmt", "back": "afmt", "browser_front": "bqfmt", "browser_back": "bafmt"}


class TemplateSpec(TypedDict, total=False):
    name: Required[str]
    front: str
    back: str
    browser_front: str
    browser_back: str


class SchemaOperation(TypedDict, total=False):
    op: Required[
        Literal[
            "add_field",
            "rename_field",
            "remove_field",
            "reposition_field",
            "set_sort_field",
            "add_template",
            "rename_template",
            "remove_template",
            "reposition_template",
        ]
    ]
    name: Required[str]
    new_name: str
    position: int
    front: str
    back: str


def note_type_view(col: Collection, nt: dict[str, Any]) -> Result:
    templates = []
    for t in nt["tmpls"]:
        entry = {"name": t["name"], "front": t["qfmt"], "back": t["afmt"]}
        if t.get("bqfmt"):
            entry["browser_front"] = t["bqfmt"]
        if t.get("bafmt"):
            entry["browser_back"] = t["bafmt"]
        templates.append(entry)
    return {
        "name": nt["name"],
        "id": nt["id"],
        "kind": "cloze" if nt["type"] == 1 else "standard",
        "sort_field": nt["flds"][nt["sortf"]]["name"] if nt["flds"] else None,
        "fields": [{k: v for k, v in f.items() if k not in FIELD_KEYS_HIDDEN} for f in nt["flds"]],
        "templates": templates,
        "css": nt["css"],
        "notes": col.models.use_count(nt),
    }


def _position(nt: dict[str, Any], key: str, op: SchemaOperation, extra: int = 0) -> int:
    count = len(nt[key]) + extra
    pos = op.get("position")
    if pos is None:
        return count - 1
    if not 1 <= int(pos) <= count:
        raise ToolError(f"position must be between 1 and {count}")
    return int(pos) - 1


def apply_schema_ops(
    col: Collection, nt: dict[str, Any], operations: list[SchemaOperation]
) -> list[Result]:
    """Apply operations to the note type dict in memory (not saved)."""
    mm = col.models
    report: list[Result] = []
    for i, op in enumerate(operations):
        kind = op.get("op")
        if kind not in SCHEMA_OPS:
            raise ToolError(
                f"operation {i}: unknown op {kind!r}. Available: {', '.join(SCHEMA_OPS)}"
            )
        name = op.get("name")
        if not name:
            raise ToolError(f"operation {i} ({kind}): 'name' is required")
        entry: Result = {"op": kind, "name": name, "requires_full_sync": SCHEMA_OPS[kind]}
        try:
            if kind == "add_field":
                if name in [f["name"] for f in nt["flds"]]:
                    raise ToolError(f"field {name!r} already exists")
                mm.add_field(nt, mm.new_field(name))
                if op.get("position") is not None:
                    idx = _position(nt, "flds", op)
                    mm.reposition_field(nt, nt["flds"][-1], idx)
            elif kind == "rename_field":
                new_name = _new_name(op)
                mm.rename_field(nt, nt["flds"][field_index(nt, name)], new_name)
            elif kind == "remove_field":
                if len(nt["flds"]) == 1:
                    raise ToolError("a note type needs at least one field")
                entry["notes_losing_content"] = mm.use_count(nt)
                mm.remove_field(nt, nt["flds"][field_index(nt, name)])
            elif kind == "reposition_field":
                fld = nt["flds"][field_index(nt, name)]
                mm.reposition_field(nt, fld, _position(nt, "flds", op))
            elif kind == "set_sort_field":
                mm.set_sort_index(nt, field_index(nt, name))
            elif kind == "add_template":
                _standard_only(nt)
                if name in [t["name"] for t in nt["tmpls"]]:
                    raise ToolError(f"template {name!r} already exists")
                tmpl = mm.new_template(name)
                tmpl["qfmt"] = op.get("front") or ""
                tmpl["afmt"] = op.get("back") or ""
                if not tmpl["qfmt"]:
                    raise ToolError("add_template needs 'front' (and usually 'back')")
                mm.add_template(nt, tmpl)
                if op.get("position") is not None:
                    mm.reposition_template(nt, nt["tmpls"][-1], _position(nt, "tmpls", op))
            elif kind == "rename_template":
                new_name = _new_name(op)
                nt["tmpls"][template_index(nt, name)]["name"] = new_name
            elif kind == "remove_template":
                _standard_only(nt)
                idx = template_index(nt, name)
                if len(nt["tmpls"]) == 1:
                    raise ToolError("a note type needs at least one card template")
                if nt["id"]:
                    entry["cards_deleted"] = mm.template_use_count(
                        nt["id"], nt["tmpls"][idx]["ord"]
                    )
                mm.remove_template(nt, nt["tmpls"][idx])
            elif kind == "reposition_template":
                _standard_only(nt)
                tmpl = nt["tmpls"][template_index(nt, name)]
                mm.reposition_template(nt, tmpl, _position(nt, "tmpls", op))
        except ToolError as exc:
            raise ToolError(f"operation {i} ({kind} {name!r}): {exc}") from None
        report.append(entry)
    return report


def _new_name(op: SchemaOperation) -> str:
    new_name = (op.get("new_name") or "").strip()
    if not new_name:
        raise ToolError("'new_name' is required")
    return new_name


def _standard_only(nt: dict[str, Any]) -> None:
    if nt["type"] == 1:
        raise ToolError("cloze note types have exactly one template; template ops do not apply")


def register(r: Registry) -> None:
    rt = r.runtime

    @r.tool(read_only=True)
    async def get_note_type(name: str) -> str:
        """Full definition of a note type: fields with their options, card templates
        (front/back HTML), CSS, kind (standard or cloze), sort field and note count."""

        def run(col: Collection) -> Result:
            return note_type_view(col, notetype(col, name))

        return await rt.call(run)

    @r.tool()
    async def create_note_type(
        name: str,
        fields: list[str],
        templates: list[TemplateSpec],
        css: str = "",
        is_cloze: bool = False,
    ) -> str:
        """Create a note type. templates: [{"name", "front", "back"}] using {{Field}}
        references ({{FrontSide}} on the back shows the front; cloze types need exactly
        one template using {{cloze:Field}}). Empty css keeps Anki's default style.
        Creating a note type does not change the schema. Check templates with
        preview_card."""

        def run(col: Collection) -> Result:
            mm = col.models
            if mm.by_name(name) is not None:
                raise ToolError(f"A note type named {name!r} already exists.")
            if not fields:
                raise ToolError("At least one field is required.")
            if len(set(fields)) != len(fields):
                raise ToolError("Field names must be unique.")
            if not templates:
                raise ToolError("At least one card template is required.")
            if is_cloze and len(templates) != 1:
                raise ToolError("A cloze note type has exactly one template.")
            nt = mm.new(name)
            if is_cloze:
                nt["type"] = 1
            for f in fields:
                mm.add_field(nt, mm.new_field(f))
            for t in templates:
                tmpl = mm.new_template(t["name"])
                tmpl["qfmt"] = t.get("front", "")
                tmpl["afmt"] = t.get("back", "")
                tmpl["bqfmt"] = t.get("browser_front", "")
                tmpl["bafmt"] = t.get("browser_back", "")
                mm.add_template(nt, tmpl)
            if css:
                nt["css"] = css
            mm.add(nt)
            return note_type_view(col, notetype(col, name))

        return await rt.call(run, mutates=True)

    @r.tool()
    async def clone_note_type(name: str, new_name: str) -> str:
        """Copy a note type (fields, templates, CSS; no notes) to experiment safely."""

        def run(col: Collection) -> Result:
            mm = col.models
            src = notetype(col, name)
            if mm.by_name(new_name) is not None:
                raise ToolError(f"A note type named {new_name!r} already exists.")
            clone = mm.copy(src, add=False)
            clone["name"] = new_name
            mm.add(clone)
            return {"created": new_name, "from": name}

        return await rt.call(run, mutates=True)

    @r.tool()
    async def update_note_type(
        name: str,
        css: str | None = None,
        templates: list[TemplateSpec] | None = None,
        field_options: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """Change CSS, the text of existing card templates (matched by name; only
        the given sides change) and field options (font, size, rtl, description,
        sticky, plainText, collapsed, excludeFromSearch, …). None of this changes the
        schema. To add/remove/rename/reorder fields or templates or change the sort
        field use edit_note_type_schema."""

        def run(col: Collection) -> Result:
            nt = copy.deepcopy(notetype(col, name))
            changed: list[str] = []
            if css is not None:
                nt["css"] = css
                changed.append("css")
            for t in templates or []:
                tmpl = nt["tmpls"][template_index(nt, t["name"])]
                for key, legacy in TEMPLATE_KEYS.items():
                    if key in t and t[key] is not None:  # type: ignore[literal-required]
                        tmpl[legacy] = t[key]  # type: ignore[literal-required]
                changed.append(f"template {t['name']}")
            for fname, options in (field_options or {}).items():
                fld = nt["flds"][field_index(nt, fname)]
                allowed = sorted(k for k in fld if k not in FIELD_OPTION_KEYS_HIDDEN)
                for key, value in options.items():
                    if key not in allowed:
                        raise ToolError(
                            f"Unknown field option {key!r}. Available: {', '.join(allowed)}"
                        )
                    fld[key] = value
                changed.append(f"field {fname}")
            if not changed:
                raise ToolError("Nothing to change: pass css, templates or field_options.")
            was_changed = col.schema_changed()
            col.models.update_dict(nt)
            if not was_changed and col.schema_changed():
                col.undo()
                raise ToolError(
                    "This change would modify the schema; use edit_note_type_schema instead."
                )
            return {"note_type": name, "changed": changed}

        return await rt.call(run, mutates=True)

    @r.tool()
    async def rename_note_type(name: str, new_name: str) -> str:
        """Rename a note type (does not change the schema)."""

        def run(col: Collection) -> Result:
            nt = notetype(col, name)
            if col.models.by_name(new_name) is not None:
                raise ToolError(f"A note type named {new_name!r} already exists.")
            nt["name"] = new_name
            col.models.update_dict(nt)
            return {"renamed": name, "to": new_name}

        return await rt.call(run, mutates=True)

    @r.tool(destructive=True, schema=True)
    async def edit_note_type_schema(
        name: str, operations: list[SchemaOperation], dry_run: bool = True
    ) -> str:
        """Change the structure of a note type. operations, applied in order:
        {"op":"add_field","name","position"?}, {"op":"rename_field","name","new_name"},
        {"op":"remove_field","name"}, {"op":"reposition_field","name","position"},
        {"op":"set_sort_field","name"}, {"op":"add_template","name","front","back",
        "position"?}, {"op":"rename_template","name","new_name"},
        {"op":"remove_template","name"} (deletes its cards),
        {"op":"reposition_template","name","position"}. Positions are 1-based.
        Renames alone do not need a full sync; everything else does. dry_run=true
        (default) validates and reports the effect without changing anything."""
        if not operations:
            raise ToolError("No operations given.")
        needs_full_sync = any(SCHEMA_OPS.get(op.get("op", ""), True) for op in operations)

        def plan(col: Collection) -> Result:
            nt = copy.deepcopy(notetype(col, name))
            report = apply_schema_ops(col, nt, operations)
            result: Result = {
                "dry_run": True,
                "note_type": name,
                "notes": col.models.use_count(nt),
                "operations": report,
                "requires_full_sync": needs_full_sync,
            }
            if needs_full_sync:
                result["warning"] = SCHEMA_DANGER
            return result

        if dry_run:
            return await rt.call(plan)

        def apply(col: Collection) -> Result:
            nt = notetype(col, name)
            report = apply_schema_ops(col, nt, operations)
            col.models.update_dict(nt)
            return {
                "dry_run": False,
                "note_type": nt["name"],
                "operations": report,
                "definition": note_type_view(col, notetype(col, nt["name"])),
            }

        if needs_full_sync:
            return await rt.call_schema(apply)
        return await rt.call(apply, mutates=True)

    @r.tool(destructive=True, schema=True)
    async def delete_note_type(name: str, dry_run: bool = True) -> str:
        """Delete a note type together with all its notes and cards. dry_run=true
        (default) only reports how many notes and cards would be deleted."""

        def count(col: Collection) -> Result:
            nt = notetype(col, name)
            nids = col.models.nids(nt["id"])
            cards = len(col.find_cards("nid:" + ",".join(map(str, nids)))) if nids else 0
            return {"note_type": name, "notes": len(nids), "cards": cards}

        if dry_run:

            def preview(col: Collection) -> Result:
                return {
                    "dry_run": True,
                    **count(col),
                    "requires_full_sync": True,
                    "warning": SCHEMA_DANGER,
                }

            return await rt.call(preview)

        def apply(col: Collection) -> Result:
            result = count(col)
            if len(col.models.all_names_and_ids()) == 1:
                raise ToolError("Cannot delete the last note type.")
            col.models.remove(notetype(col, name)["id"])
            return {"dry_run": False, "deleted": True, **result}

        return await rt.call_schema(apply)

    @r.tool(read_only=True)
    async def preview_card(
        note_type: str,
        fields: dict[str, str],
        template: str | None = None,
        front: str | None = None,
        back: str | None = None,
    ) -> str:
        """Render the question and answer HTML for sample field values without saving
        anything. template: template name (standard) or cloze number like "c2"
        (cloze). Pass front/back to try out new template text before applying it with
        update_note_type or create_note_type."""

        def run(col: Collection) -> Result:
            nt = notetype(col, note_type)
            note = col.new_note(nt)
            for fname, value in fields.items():
                field_index(nt, fname)
                note[fname] = value
            if nt["type"] == 1:
                if template:
                    try:
                        ord_ = int(template.lower().lstrip("c")) - 1
                    except ValueError:
                        raise ToolError("For a cloze note type pass template like 'c1'.") from None
                else:
                    numbers = note.cloze_numbers_in_fields()
                    ord_ = (numbers[0] - 1) if numbers else 0
                base = nt["tmpls"][0]
            else:
                idx = template_index(nt, template) if template else 0
                ord_ = idx
                base = nt["tmpls"][idx]
            custom = None
            if front is not None or back is not None:
                custom = copy.deepcopy(base)
                if front is not None:
                    custom["qfmt"] = front
                if back is not None:
                    custom["afmt"] = back
            card = note.ephemeral_card(ord_, custom_template=custom, fill_empty=False)
            out = card.render_output()
            return {
                "question": out.question_text,
                "answer": out.answer_text,
                "empty": not out.question_text.strip(),
            }

        return await rt.call(run)

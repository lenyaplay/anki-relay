"""Shared plumbing for tools: current user, locking, sync hooks, lookups, output."""

from __future__ import annotations

import asyncio
import functools
import html
import inspect
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import anki.collection  # noqa: F401
from anki.collection import Collection
from anki.errors import AnkiException
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..config import ConfigError, Settings
from ..logging_setup import call_context, redact_args, summarize_result
from ..media import MediaError, MediaFetcher
from ..sync import SyncManager, SyncProblem
from ..users import User, UserManager

Result = dict[str, Any]

log = logging.getLogger("anki_relay.tools")

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
PREVIEW_CHARS = 300
MAX_LISTED_OPTIONS = 60

DANGER = (
    "DANGEROUS: before calling with dry_run=false, show the user what will be affected "
    "(call with dry_run=true first) and get their explicit confirmation."
)
SCHEMA_DANGER = (
    "Changes the collection schema: requires a one-way upload of the whole collection to "
    "AnkiWeb, after which every other device must choose 'Download from AnkiWeb'. "
    "Before confirming, the user should sync all devices; unsynced reviews on them will "
    "be lost. " + DANGER
)


def dumps(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        users: UserManager,
        sync: SyncManager,
        fetcher: MediaFetcher,
    ) -> None:
        self.settings = settings
        self.users = users
        self.sync = sync
        self.fetcher = fetcher

    def current_user(self) -> User:
        token = get_access_token()
        if token is None or not token.subject:
            raise ToolError("Not signed in.")
        return self.users.get(token.subject)

    async def call(
        self,
        fn: Callable[[Collection], Result],
        *,
        mutates: bool | Callable[[Result], bool] = False,
    ) -> str:
        """Run ``fn(col)`` in a worker thread under the user's lock, after a pull."""
        user = self.current_user()
        return dumps(await self.guard(asyncio.to_thread(self._call_locked, user, fn, mutates)))

    def _call_locked(
        self,
        user: User,
        fn: Callable[[Collection], Result],
        mutates: bool | Callable[[Result], bool],
    ) -> Result:
        with user.lock:
            col, warnings = self.sync.prepare_locked(user)
            result = fn(col)
            changed = mutates(result) if callable(mutates) else mutates
            if changed:
                self.sync.mark_dirty_locked(user)
        if warnings:
            result["warnings"] = warnings
        return result

    async def call_schema(self, fn: Callable[[Collection], Result]) -> str:
        """Run a schema-changing ``fn`` under the full upload protocol."""
        user = self.current_user()

        def run() -> Result:
            with user.lock:
                result, info = self.sync.schema_change_locked(user, fn)
            result["sync"] = info
            return result

        return dumps(await self.guard(asyncio.to_thread(run)))

    async def call_plain(self, fn: Callable[[User], Result]) -> str:
        """Run ``fn(user)`` in a worker thread without taking the lock or pulling."""
        user = self.current_user()
        return dumps(await self.guard(asyncio.to_thread(fn, user)))

    @staticmethod
    async def guard(awaitable: Any) -> Any:
        try:
            return await awaitable
        except ToolError:
            raise
        except SyncProblem as exc:
            message = str(exc) or type(exc).__name__
            if exc.details:
                message += "\nfull_sync: " + dumps(exc.details)
            raise ToolError(message) from exc
        except (MediaError, AnkiException) as exc:
            raise ToolError(str(exc) or type(exc).__name__) from exc


def logged(fn: Callable[..., Any], name: str) -> Callable[..., Any]:
    """Wrap a tool so every call writes one ``tool.call`` event (REQ-004).

    The wrapper keeps the signature (FastMCP builds the schema from it) and sets
    the call context, so log records of the same call carry its call_id and user.
    """

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        token = get_access_token()
        user = token.subject if token else None
        reset = call_context.set({"call_id": secrets.token_hex(4), "user": user})
        start = time.monotonic()
        base = {"event": "tool.call", "tool": name, "arguments": redact_args(kwargs)}
        try:
            result = await fn(**kwargs)
        except Exception as exc:
            log.warning(
                "tool %s failed",
                name,
                extra={
                    **base,
                    "outcome": "error",
                    "duration_ms": round((time.monotonic() - start) * 1000),
                    "error": str(exc)[:500],
                },
            )
            raise
        else:
            log.info(
                "tool %s",
                name,
                extra={
                    **base,
                    "outcome": "ok",
                    "duration_ms": round((time.monotonic() - start) * 1000),
                    "result": summarize_result(result),
                },
            )
            return result
        finally:
            call_context.reset(reset)

    return wrapper


class Registry:
    """Registers tools on FastMCP, honouring DISABLED_TOOLS and feature switches."""

    def __init__(self, mcp: FastMCP, runtime: Runtime) -> None:
        self.mcp = mcp
        self.runtime = runtime
        self.settings = runtime.settings
        self.all_names: list[str] = []
        self.registered: list[str] = []
        self.schema_tools: list[str] = []

    def tool(
        self,
        *,
        read_only: bool = False,
        destructive: bool = False,
        schema: bool = False,
        needs_media_sync: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            name = fn.__name__
            self.all_names.append(name)
            if schema:
                self.schema_tools.append(name)
            s = self.settings
            if (
                name in s.disabled_tools
                or (schema and not s.allow_schema_changes)
                or (needs_media_sync and not s.media_sync)
            ):
                return fn
            description = inspect.cleandoc(fn.__doc__ or "")
            if schema:
                description += "\n\n" + SCHEMA_DANGER
            elif destructive:
                description += "\n\n" + DANGER
            self.mcp.add_tool(
                logged(fn, name),
                name=name,
                description=description,
                annotations=ToolAnnotations(
                    readOnlyHint=read_only,
                    destructiveHint=destructive,
                    openWorldHint=False,
                ),
                structured_output=False,
            )
            self.registered.append(name)
            return fn

        return decorate

    def finish(self) -> None:
        unknown = sorted(set(self.settings.disabled_tools) - set(self.all_names))
        if unknown:
            raise ConfigError(
                "Invalid configuration:\n  DISABLED_TOOLS: unknown tool(s) "
                f"{', '.join(unknown)}. Known tools: {', '.join(sorted(self.all_names))}"
            )


# ---------------------------------------------------------------- lookups


def unknown(kind: str, name: object, options: Iterable[str]) -> ToolError:
    opts = sorted(options)
    shown = ", ".join(repr(o) for o in opts[:MAX_LISTED_OPTIONS])
    if len(opts) > MAX_LISTED_OPTIONS:
        shown += f", … ({len(opts) - MAX_LISTED_OPTIONS} more)"
    return ToolError(f"Unknown {kind} {name!r}. Available: {shown or '(none)'}")


def notetype(col: Collection, name: str) -> dict[str, Any]:
    nt = col.models.by_name(name)
    if nt is None:
        raise unknown("note type", name, col.models.all_names())
    return nt


def deck_id(col: Collection, name: str) -> int:
    did = col.decks.id_for_name(name)
    if did is None:
        raise unknown("deck", name, (d.name for d in col.decks.all_names_and_ids()))
    return did


def ensure_deck(col: Collection, name: str) -> int:
    name = name.strip()
    if not name:
        raise ToolError("Deck name is empty.")
    did = col.decks.id(name, create=True)
    assert did is not None
    return did


def field_names(nt: dict[str, Any]) -> list[str]:
    return [f["name"] for f in nt["flds"]]


def field_index(nt: dict[str, Any], name: str) -> int:
    names = field_names(nt)
    if name not in names:
        raise unknown(f"field of note type {nt['name']!r}", name, names)
    return names.index(name)


def template_index(nt: dict[str, Any], name: str) -> int:
    names = [t["name"] for t in nt["tmpls"]]
    if name not in names:
        raise unknown(f"card template of note type {nt['name']!r}", name, names)
    return names.index(name)


def check_batch(items: Sequence[Any], settings: Settings, what: str = "items") -> None:
    if not items:
        raise ToolError(f"No {what} given.")
    if len(items) > settings.max_batch:
        raise ToolError(
            f"Too many {what}: {len(items)} (MAX_BATCH is {settings.max_batch}). "
            "Split the request into several calls."
        )


def page(ids: Sequence[int], limit: int, offset: int) -> tuple[list[int], Result]:
    if limit < 1:
        raise ToolError("limit must be at least 1.")
    if offset < 0:
        raise ToolError("offset must not be negative.")
    limit = min(limit, MAX_LIMIT)
    chunk = list(ids[offset : offset + limit])
    meta: Result = {"total": len(ids), "offset": offset, "count": len(chunk)}
    if offset + limit < len(ids):
        meta["next_offset"] = offset + limit
    return chunk, meta


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def text_preview(value: str, limit: int = PREVIEW_CHARS) -> str:
    text = value.replace("<br>", " ").replace("<br/>", " ").replace("<div>", " ")
    text = html.unescape(_TAG_RE.sub("", text))
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def existing_ids(col: Collection, ids: Sequence[int], kind: str) -> tuple[list[int], list[int]]:
    """Split ids into (existing, missing) using an id search."""
    wanted = [int(i) for i in dict.fromkeys(ids)]
    if not wanted:
        return [], []
    prefix = "cid" if kind == "card" else "nid"
    found: set[int] = set()
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        search = f"{prefix}:{','.join(map(str, chunk))}"
        found.update(col.find_cards(search) if kind == "card" else col.find_notes(search))
    existing = [i for i in wanted if i in found]
    missing = [i for i in wanted if i not in found]
    return existing, missing


def tags_list(tags: str | list[str] | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return tags.split()
    out: list[str] = []
    for tag in tags:
        out.extend(tag.split())
    return out

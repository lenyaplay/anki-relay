"""FastMCP server: wiring, tool registration and the entry point."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import SCOPE, RelayOAuthProvider
from .config import ConfigError, Settings, load_settings
from .debug_bundle import build_bundle
from .logging_setup import setup_logging
from .media import MediaFetcher
from .sync import AnkiSyncBackend, SyncManager
from .tools import register_all
from .tools.runtime import Registry, Runtime
from .users import UserManager

log = logging.getLogger("anki_relay")

INSTRUCTIONS = """\
Anki Relay gives you the user's own Anki collection (a copy synced with AnkiWeb, so
changes reach their phone and computer). Work in Anki's terms: decks, note types,
fields, card templates, tags, deck options and Anki search syntax.
- Learn the structure with collection_overview and get_note_type before changing it.
- Fields take HTML. For images or audio call add_media first and put the returned
  snippet into a field.
- Add notes in batches with add_notes (many notes per call).
- Check new or edited templates with preview_card before applying them.
- Dangerous tools (deleting, resetting, schema changes) default to dry_run=true: show
  the user the dry-run result and get explicit confirmation before dry_run=false.
- Schema changes (fields/templates of an existing note type, deleting a note type,
  changing note types) upload the whole collection to AnkiWeb one-way; the user must
  sync all devices first and then choose "Download from AnkiWeb" on each of them.
- If an error carries full_sync with safe_to_download=false, tell the user what would
  be lost and call sync(force_download=true) only after they confirm.
"""


@dataclass
class App:
    settings: Settings
    mcp: FastMCP
    asgi: ASGIApp
    users: UserManager
    sync: SyncManager
    provider: RelayOAuthProvider
    registry: Registry


class HostCheckMiddleware:
    """Reject requests whose Host is not the public host (DNS rebinding), except /health."""

    def __init__(self, app: ASGIApp, allowed: list[str]) -> None:
        self.app = app
        self.allowed = allowed

    def _ok(self, host: str) -> bool:
        for allowed in self.allowed:
            if host == allowed:
                return True
            if allowed.endswith(":*") and host.startswith(allowed[:-1]):
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") != "/health":
            host = ""
            for key, value in scope.get("headers", []):
                if key == b"host":
                    host = value.decode("latin-1")
                    break
            if not self._ok(host):
                await PlainTextResponse("Invalid Host header", status_code=421)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def allowed_hosts(settings: Settings) -> list[str]:
    host = settings.public_host
    hostname = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    return sorted({host, hostname, f"{hostname}:*"})


def build_app(
    settings: Settings,
    *,
    backend: AnkiSyncBackend | None = None,
    fetcher: MediaFetcher | None = None,
) -> App:
    backend = backend or AnkiSyncBackend()
    users = UserManager(settings)
    sync = SyncManager(settings, users, backend)
    provider = RelayOAuthProvider(settings, users, backend)
    hosts = allowed_hosts(settings)
    # base64 inflates by 4/3; leave room for the JSON envelope.
    body_limit = max(4 * 1024 * 1024, settings.media_max_bytes * 4 // 3 + 1024 * 1024)

    mcp = FastMCP(
        name="Anki Relay",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,  # type: ignore[arg-type]
            resource_server_url=settings.public_url + "/mcp",  # type: ignore[arg-type]
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        stateless_http=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=["https://claude.ai", "https://claude.com"],
        ),
        max_request_body_size=body_limit,
        log_level=settings.log_level,
        host=settings.host,
        port=settings.port,
    )

    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request) -> Response:
        return await provider.login_endpoint(request)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    runtime = Runtime(settings, users, sync, fetcher or MediaFetcher(settings))
    registry = Registry(mcp, runtime)
    register_all(registry)

    starlette: Starlette = mcp.streamable_http_app()
    inner_lifespan = starlette.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with inner_lifespan(app):
            await sync.start()
            log.info(
                "Anki Relay ready at %s/mcp (%d tools)",
                settings.public_url,
                len(registry.registered),
            )
            try:
                yield
            finally:
                log.info("shutting down: sending unsynced changes")
                await sync.stop()

    starlette.router.lifespan_context = lifespan
    asgi = HostCheckMiddleware(starlette, hosts)
    return App(settings, mcp, asgi, users, sync, provider, registry)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="anki-relay", description="Self-hosted MCP server for Anki (see README)."
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (default)")
    bundle = sub.add_parser(
        "debug-bundle",
        help="write a debugging archive (logs, versions, sync state; no secrets) to LOG_DIR",
    )
    bundle.add_argument("--days", type=int, default=None, help="only logs of the last N days")
    bundle.add_argument("--output-dir", type=Path, default=None, help="default: LOG_DIR")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "debug-bundle":
            path = build_bundle(settings, days=args.days, out_dir=args.output_dir)
            print(path)
            print(
                "Copy it off the server, e.g.: docker compose cp "
                f"anki-relay:{path.as_posix()} ./  (then scp)",
                file=sys.stderr,
            )
            return
        setup_logging(settings)
        app = build_app(settings)
    except ConfigError as exc:
        print(f"anki-relay: {exc}", file=sys.stderr)
        sys.exit(2)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        app.asgi,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,  # our handlers (stdout + JSON file) receive uvicorn's records
        proxy_headers=False,
        timeout_graceful_shutdown=30,
        server_header=False,
    )


if __name__ == "__main__":
    main()

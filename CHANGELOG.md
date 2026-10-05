# Changelog

All notable changes to Anki Relay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Each entry links the requirements
document (`docs/requirements/REQ-NNN-*.md`) it implements.

## [Unreleased]

### Added
- Sync diagnostics in the JSON log ([REQ-005]):
  - `sync.request` for every request to the sync server: step (`meta`,
    `full_download`, `full_upload`, `media_start`), endpoint host, attempt,
    duration; on failure the error exactly as the anki library reported it
    (`error_type`, `error_kind`, `error_message`, plus `http_code` /
    `http_context` extracted literally from `HttpError { … }` texts);
  - `sync.recovered` (first success after failures) and `sync.media_failed`.
- `ANKI_RUST_LOG` (default `off`): the anki library's own log in
  `logs/anki-rust.log`, included in the debug bundle ([REQ-005]).
- `tests/test_messages.py`: guards against guessed causes in texts sent to
  Claude ([REQ-005]).
- Logging system ([REQ-004]):
  - JSON Lines log in `./logs/anki-relay.log`, daily rotation, 14 days by
    default (`LOG_DIR`, `LOG_RETENTION_DAYS`);
  - one `tool.call` record per tool call with redacted arguments (names kept,
    contents replaced by their length); `sync.*` and `auth.*` events; `call_id`
    ties sync events to the tool call that caused them;
  - `anki-relay debug-bundle` command: logs, versions, configuration (emails
    replaced by user ids) and per-user sync state, without AnkiWeb keys, OAuth data
    or collections.
- Full-sync decisions ([REQ-003]): when the sync server answers with a full sync
  and the server copy has no unsynced changes and no local schema change, the
  server downloads the collection by itself (after a backup). Otherwise tool errors
  and `sync_status` carry a structured `full_sync` block (`safe_to_download`,
  `download_offered`, …) for Claude to decide together with the user.
- Initial MVP ([REQ-001]):
  - MCP server (Streamable HTTP, stateless) with 39 tools: overview and search,
    notes, cards, decks and deck options, note types, tags, media, sync;
  - per-user collections synced with AnkiWeb through the official `anki`
    library: delayed push, interval pull, exponential backoff, push on shutdown;
  - schema change protocol: pre-sync, backup, one-way upload; the only code path
    that uploads;
  - OAuth 2.1 with dynamic client registration and sign-in through AnkiWeb
    (password never stored), `ALLOWED_EMAILS`, token rotation, IP rate limit,
    Russian/English sign-in page;
  - `add_media` with SSRF protection, size and extension limits;
  - Docker image (non-root), Compose with built-in Caddy or your own nginx, CI;
  - `scripts/validate.py`: lint, all tests (including a real local
    `anki.syncserver`), repository hygiene, Compose and container checks.

### Changed
- Texts sent to Claude and shown on the sign-in page state only what the anki
  library or the sync server returned, facts about the server copy and what each
  action does; guessed causes removed ([REQ-005]). Sign-in page errors now read
  "The sync server returned an error" / "A network error occurred while contacting
  the sync server" instead of "AnkiWeb did not respond" / "No connection to
  AnkiWeb".
- The `full_sync` field `ankiweb_empty` was replaced by `download_offered`
  ([REQ-005]).
- `sync.normal`, `sync.full_download*` and `sync.full_upload*` log events were
  replaced by `sync.request` ([REQ-005]).
- Changing a note type's sort field moved from `update_note_type` to
  `edit_note_type_schema` (`set_sort_field`): Anki marks it as a schema change
  ([REQ-001]).

### Fixed
- Extra log fields could overwrite the `level` of a JSON log record ([REQ-005]).
- ANSI colour codes in `anki-rust.log` ([REQ-005]).
- Successful Docker healthcheck probes no longer fill the access log; uvicorn's
  `color_message` field removed from JSON records ([REQ-004]).

[REQ-001]: docs/requirements/REQ-001-mvp.md
[REQ-003]: docs/requirements/REQ-003-full-sync-decisions.md
[REQ-004]: docs/requirements/REQ-004-logging.md
[REQ-005]: docs/requirements/REQ-005-sync-diagnostics.md

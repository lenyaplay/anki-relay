# Anki Relay

A self-hosted [MCP](https://modelcontextprotocol.io) server that connects Claude
(claude.ai, desktop and mobile apps) to the Anki collections of a few trusted
people. Ask Claude to explore your collection, design note types, add a hundred
cards with pictures and audio, retag, reorganise decks or tune deck options — the
changes reach your phone and computer through normal Anki sync.

[Русская версия](README.ru.md)

```
Claude ──HTTPS + OAuth──▶ anki-relay.example.com/mcp
                           │  MCP server + one copy of each user's collection
                           ▼
                        AnkiWeb ◀──▶ your phone, your computer
```

**The server is just one more Anki device.** For every user it keeps its own copy
of the collection (`collection.anki2` plus the media folder) on disk and syncs it
with AnkiWeb using the official `anki` Python library — the same engine Anki
Desktop uses. Anki Desktop and AnkiConnect are not needed.

- Only email addresses listed in `ALLOWED_EMAILS` can sign in.
- Each user has their own collection, AnkiWeb session and OAuth tokens and cannot
  reach anybody else's data.
- Users sign in with their AnkiWeb email and password. The password is checked by
  AnkiWeb and never stored.

> Unofficial project: not affiliated with Anki, AnkiWeb, Ankitects or Anthropic.
> AnkiWeb is a free service — the server syncs about as often as a regular Anki
> client and no more.

## Requirements

- A VPS (1 GB RAM is enough for a few users; disk space for collections and media).
- Docker with the Compose plugin.
- A subdomain, e.g. `anki-relay.example.com`, with an A (and/or AAAA) record
  pointing to the VPS.
- Each user's AnkiWeb account must already contain their collection (sync Anki
  Desktop or AnkiMobile at least once). The server never uploads a collection to
  an empty account on its own.

## Installation

```bash
git clone https://github.com/<you>/anki-relay.git
cd anki-relay
cp .env.example .env
nano .env            # set PUBLIC_URL and ALLOWED_EMAILS
docker compose up -d
docker compose logs -f anki-relay
```

### Mode 1: everything included (default)

`COMPOSE_PROFILES=caddy` in `.env` starts the app and [Caddy](https://caddyserver.com).
Caddy reads the host name from `PUBLIC_URL` (the Caddyfile uses `{$PUBLIC_URL}` as
its site address), obtains a Let's Encrypt certificate and proxies to the app over
the internal Docker network. Ports 80 and 443 must be open. The app's own port is
then not needed on the host at all and is bound to `127.0.0.1` only.

### Mode 2: your own nginx

Set `COMPOSE_PROFILES=` (empty). Only the app starts, published on
`${BIND_ADDRESS}:${APP_PORT}` (default `127.0.0.1:8000`). Your nginx terminates
HTTPS and proxies to it — start from
[`deploy/nginx.conf.example`](deploy/nginx.conf.example), which explains every
directive (`proxy_pass` port = `APP_PORT`, `Host` header, `X-Forwarded-For`,
streaming settings, `client_max_body_size`).

### Ports

| Variable | Default | Meaning |
|---|---|---|
| `BIND_ADDRESS` | `127.0.0.1` | Host address the app port is published on. `127.0.0.1`: only reachable from the server itself (nginx on the same VPS). `0.0.0.0`: all interfaces. |
| `APP_PORT` | `8000` | App port on the host (what `proxy_pass` points to). |
| `HTTP_PORT`, `HTTPS_PORT` | `80`, `443` | Caddy's ports on the host (caddy mode only). |

> **`BIND_ADDRESS=0.0.0.0`** makes the app reachable over plain HTTP from the
> outside. Only do that when your proxy runs on another machine, close the port
> with a firewall for everybody else, and add the proxy's address to
> `TRUSTED_PROXIES`.

> **Non-standard `HTTP_PORT`/`HTTPS_PORT`**: Let's Encrypt validates domains on
> ports 80/443 only, so Caddy cannot get a certificate the usual way on other
> ports. That setup makes sense when another proxy in front of Caddy forwards
> 80/443 to it (then keep the external ports standard), or when you configure
> Caddy for the DNS challenge (requires a Caddy build with your DNS provider's
> plugin and an edited Caddyfile).

## Connect Claude

1. In Claude: **Settings → Connectors → Add custom connector**.
2. URL: `https://anki-relay.example.com/mcp` → **Add** → **Connect**.
3. A sign-in page of your server opens: enter your AnkiWeb email and password.
4. Done. The first request downloads your collection (and, in the background,
   your media) from AnkiWeb; for big collections this takes a while.

### How the sign-in (OAuth 2.1) works

1. Claude registers itself as an OAuth client (dynamic client registration) and
   opens `/authorize` with a PKCE challenge.
2. The server redirects to its `/login?req=…` page (the link lives 10 minutes and
   works once).
3. You enter your AnkiWeb email and password. Addresses not in `ALLOWED_EMAILS` are
   refused without contacting AnkiWeb. Otherwise the server asks AnkiWeb to check
   the credentials, and on success stores **only** the AnkiWeb session key
   (`hkey`) and endpoint in `data/users/<id>/sync_auth.json` (mode 600).
4. You are redirected back to Claude with a one-time code (valid 5 minutes),
   which Claude exchanges for an access token (`ACCESS_TOKEN_TTL`) and a refresh
   token (`REFRESH_TOKEN_TTL_DAYS`, rotated on every use).
5. Every request re-checks that the token owner's email is still allowed.

Clients and tokens (as SHA-256 hashes) live in `data/oauth.json`, so restarts do not
sign anyone out. Repeated failed sign-ins from one IP are blocked
(`LOGIN_MAX_FAILS` per `LOGIN_FAIL_WINDOW`, plus a global limit five times higher).

## Configuration

Only `PUBLIC_URL` and `ALLOWED_EMAILS` are required. **By default every feature is
enabled**; the settings restrict features or relax protections. Invalid values stop
the server at startup with a message naming the variable.

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_URL` | — (required) | External address, without `/mcp`. `https://` (or `http://localhost` / `http://127.0.0.1` for development). |
| `ALLOWED_EMAILS` | — (required) | Comma-separated AnkiWeb emails allowed to sign in. |
| `COMPOSE_PROFILES` | `caddy` | `caddy` for built-in HTTPS, empty for your own proxy. |
| `DISABLED_TOOLS` | empty | Tools not to register at all, e.g. `delete_notes,delete_deck`. |
| `ALLOW_SCHEMA_CHANGES` | `true` | Allow note type restructuring (one-way uploads by protocol). `false` removes those tools and the server never uploads. |
| `MEDIA_SYNC` | `true` | Two-way media sync; `false` also disables `add_media`. |
| `MEDIA_ALLOWED_EXTENSIONS` | all Anki formats | List of extensions or `*`. |
| `MEDIA_MAX_MB` | `100` | Size limit per file. |
| `MEDIA_DOWNLOAD_TIMEOUT` | `60` | Download timeout for `add_media(url=…)`, seconds. |
| `MEDIA_ALLOW_PRIVATE_URLS` | `false` | Allow downloads from internal addresses (see Media). |
| `SYNC_PULL_INTERVAL` | `60` | Pull from AnkiWeb at most this often, seconds. |
| `SYNC_PUSH_DELAY` | `10` | Push after this many seconds without new changes. |
| `SYNC_PUSH_MAX_DELAY` | `60` | …but no later than this after the first unsynced change. |
| `SYNC_ENDPOINT` | empty | Self-hosted Anki sync server instead of AnkiWeb. |
| `BACKUP_KEEP` | `10` | Collection backups kept per user. |
| `MAX_BATCH` | `500` | Max items per batch call. |
| `ACCESS_TOKEN_TTL` | `3600` | Access token lifetime, seconds. |
| `REFRESH_TOKEN_TTL_DAYS` | `90` | Days without use before signing in again. |
| `LOGIN_MAX_FAILS` | `10` | Failed sign-ins per IP per window before blocking. |
| `LOGIN_FAIL_WINDOW` | `900` | That window, seconds. |
| `COLLECTION_IDLE_MINUTES` | `30` | Close idle collections after this many minutes. |
| `DEFAULT_LANGUAGE` | `auto` | Sign-in page: `auto` (browser), `ru`, `en`. |
| `TRUSTED_PROXIES` | `127.0.0.1/32,::1/128,172.16.0.0/12` | Proxies whose `X-Forwarded-For` is trusted (Docker networks + localhost). |
| `BIND_ADDRESS` | `127.0.0.1` | See Ports. |
| `APP_PORT` | `8000` | See Ports. |
| `HTTP_PORT`, `HTTPS_PORT` | `80`, `443` | See Ports. |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Where the app listens inside the container; needed only without Docker. |
| `DATA_DIR` | `/data` | Data directory. |
| `LOG_LEVEL` | `INFO` | Passwords, tokens, emails and card contents are never logged at any level. |
| `LOG_DIR` | `/logs` | JSON log files (mounted to `./logs`); empty = stdout only. |
| `LOG_RETENTION_DAYS` | `14` | Days of log files to keep. |
| `ANKI_RUST_LOG` | `off` | The anki library's own log (`off`…`trace`) in `logs/anki-rust.log`. |

A restricted setup — no deletions, no resets, no schema changes, no media:

```ini
DISABLED_TOOLS=delete_notes,delete_deck,forget_cards,delete_media
ALLOW_SCHEMA_CHANGES=false
MEDIA_SYNC=false
```

## Tools

The server is a thin wrapper over Anki in Anki's own terms — no presets, built-in
note types or tagging schemes. Batch tools take lists and report a result per item.
List tools take `limit` and `offset`. Dangerous tools default to `dry_run=true`
and only report what would change.

| Group | Tool | What it does |
|---|---|---|
| Overview | `collection_overview` | Decks (cards, options group), note types (fields, templates, notes), tags, totals |
| | `find_notes` | Search notes with Anki syntax; plain-text field previews |
| | `find_cards` | Search cards: deck, template, queue, due, interval, ease/FSRS |
| | `get_notes` | Full notes with exact field HTML |
| | `card_info` | Card details with review history |
| | `stats` | Due today, studied today, totals; collection or deck |
| Notes | `add_notes` | Batch add; mixed decks and note types; skips empty/duplicate/bad cloze |
| | `update_notes` | Batch change of given fields and/or tags |
| | `delete_notes` | Delete notes (dangerous, dry run) |
| | `change_note_type` | Convert notes to another type with field/template mapping (schema) |
| Cards | `move_cards` | Move to a deck |
| | `set_suspended` / `set_buried` | Suspend/unsuspend, bury/unbury |
| | `forget_cards` | Reset to new (dangerous, dry run) |
| | `set_due_date` | Due date in Anki syntax (`0`, `1-7`, `3!`) |
| Decks | `create_deck` / `rename_deck` | Create (`::` for subdecks), rename or move |
| | `delete_deck` | Delete with cards (dangerous, dry run) |
| | `get_deck_options` / `update_deck_options` | Whole options group; partial changes; shared groups need `scope` |
| Note types | `get_note_type` | Fields with options, templates, CSS, kind, sort field |
| | `create_note_type` / `clone_note_type` | Create (standard or cloze), copy to experiment |
| | `update_note_type` | CSS, template text, field options (no schema change) |
| | `rename_note_type` | Rename |
| | `edit_note_type_schema` | Add/rename/remove/reorder fields and templates, sort field (schema) |
| | `delete_note_type` | Delete with all notes (schema, dangerous) |
| | `preview_card` | Render question/answer for sample data, also for unsaved template text |
| Tags | `list_tags` | Tags with note counts |
| | `add_tags` / `remove_tags` | Batch |
| | `rename_tag` | Rename incl. `parent::child` hierarchy |
| | `clear_unused_tags` | Remove unused tags |
| Media | `add_media` | Save a file from URL or base64, returns a field snippet |
| | `list_media` / `delete_media` | List; delete (dry run) |
| | `check_media` | Unused and missing files |
| Sync | `sync` | Sync now; `force_download=true` replaces the server copy |
| | `sync_status` | Last sync, pending changes, pending uploads, errors, media |

Things to ask Claude:

- "Look through my collection and explain how it's organised."
- "Add an Audio field to my Vocabulary note type and show me a preview of the card."
- "Move all cards tagged `leech` into the deck `Review::Hard`."
- "Make 40 Spanish verb cards from this list with example sentences, tagged by tense."
- "Lower new cards per day to 10 for my Japanese deck only."

## How syncing works

The server behaves like a regular Anki client and avoids extra requests (a typical
burst is 20–60 notes in half an hour):

- **Pull.** Before a tool touches the collection, the server syncs if the last
  successful sync is older than `SYNC_PULL_INTERVAL`. A burst of calls causes one
  sync, not one per call.
- **Push.** Changes start a timer: after `SYNC_PUSH_DELAY` seconds without new
  changes one sync sends the whole batch; continuous changes are still pushed within
  `SYNC_PUSH_MAX_DELAY` of the first one. Media sync starts together with the push.
- **Shutdown and restart.** On `docker compose down`/SIGTERM unsynced changes are
  pushed (60 s grace period); anything left over is pushed on the next start.
- **Errors and rate limits.** Network errors and AnkiWeb errors are retried with an
  exponential pause (5 s, 10 s, 20 s … up to 5 minutes). Meanwhile tools keep
  working on the server copy and say so. `sync_status` shows the state.
- **Full syncs.** The server never uploads the whole collection during normal
  work. When AnkiWeb answers with a full sync (the integration test shows this, for
  example, after a note type was restructured on another device), the server
  decides like this:

  | Situation | What happens |
  |---|---|
  | The server copy has nothing that AnkiWeb lacks (no unsynced changes, no local schema change) | It downloads the collection from AnkiWeb by itself, after a backup; nothing is lost. The tool result carries a warning. |
  | The server copy has unsynced changes | Error with a structured `full_sync` block (`safe_to_download: false` …). Claude tells you what would be lost and calls `sync(force_download=true)` only after you confirm. |
  | AnkiWeb answers `FULL_UPLOAD`, i.e. it offers only a one-way upload (`download_offered: false`; in tests this happened when the AnkiWeb collection was empty) | Never handled automatically: the server never uploads. Decide yourself; `sync(force_download=true)` would replace the server copy with what AnkiWeb has. |

  `sync_status` shows the same `full_sync` block while the situation lasts. A
  download only replaces the server copy; AnkiWeb and your devices are untouched.

## Schema changes

Anki can't transfer some structural changes with a normal sync: adding, removing or
reordering fields or card templates of an existing note type, changing its sort
field, deleting a note type and changing the note type of notes. After such a
change Anki Desktop warns that "this will require a one-way sync" — the whole
collection is uploaded to AnkiWeb and every other device must download it.
(Creating, cloning or renaming note types, renaming fields/templates and editing
CSS, template text and field options do **not** need this.)

The server allows schema changes only through a fixed protocol:

1. Claude calls the tool with `dry_run=true` (the default) and shows you what will
   change, with a warning.
2. **Sync all your devices first**, then confirm. Reviews that are not synced from
   other devices at that moment will be lost.
3. With `dry_run=false` the server first runs a normal sync, which must succeed
   without requiring a full sync — otherwise nothing is changed.
4. It backs up the collection file to `data/users/<id>/backups/` (the last
   `BACKUP_KEEP` are kept), applies the change and immediately uploads the
   collection one-way.
5. On every other device, choose **Download from AnkiWeb** at the next sync.

If the upload fails, the change stays on the server copy and normal sync is paused
until `sync()` retries the upload (or `sync(force_download=true)` discards the
change). `sync_status` and every tool report this.

**Restoring a backup:** stop the server (`docker compose stop anki-relay`), copy a
file from `data/users/<id>/backups/` over `data/users/<id>/collection.anki2`, start
it, and ask Claude to run `sync(force_download=true)` if you want AnkiWeb's version
instead — or open the backup in Anki Desktop (it is a regular collection file).

**Disable:** `ALLOW_SCHEMA_CHANGES=false` removes `edit_note_type_schema`,
`delete_note_type` and `change_note_type`, and the server then never uploads.

## Media and disk space

- Media sync is two-way, like in Anki: **the first sign-in downloads all of the
  user's media** to the server, which can be gigabytes. Check free disk space, or
  set `MEDIA_SYNC=false` (this also disables `add_media`).
- `add_media` takes a URL or base64 data, up to `MEDIA_MAX_MB`, with the formats
  Anki can show or play (`MEDIA_ALLOWED_EXTENSIONS`, `*` for anything). It returns
  the final file name and a snippet for a field: `<img src="…">` or `[sound:…]`.
- SVG files are not sanitised: note fields accept arbitrary HTML anyway, so
  sanitising SVG alone would add nothing.
- URL downloads accept only http/https, follow at most 5 redirects and stop after
  `MEDIA_DOWNLOAD_TIMEOUT`. **SSRF protection** is on by default: host names are
  resolved first and private, loopback, link-local and cloud metadata addresses
  (`169.254.169.254` …) are refused, on every redirect. `MEDIA_ALLOW_PRIVATE_URLS=true`
  lets Claude fetch from your NAS or other internal services — but then anything
  that can talk to Claude in your chats (a web page or document with hidden
  instructions, for example) could make the server request internal URLs and store
  the responses in your collection. Enable it only if your internal network has
  nothing sensitive reachable over HTTP.

## Security

- Passwords are never stored or logged. AnkiWeb session keys are stored in
  `data/`; protect that directory (and its backups) like a password.
- Add to `ALLOWED_EMAILS` only people you trust: whoever runs the server can
  technically read and change their collections.
- **Revoke a user:** remove their email from `ALLOWED_EMAILS` and restart
  (`docker compose up -d`). Their tokens stop working immediately. Optionally delete
  `data/users/<id>/` (`<id>` = first 16 hex characters of the SHA-256 of the
  lowercase email).
- **Sign everybody out:** delete `data/oauth.json` and restart.
- The server checks the `Host` header against `PUBLIC_URL` (DNS rebinding
  protection) and accepts browser origins only from `claude.ai` / `claude.com`.

## Logs and debugging

Logs can only be read on the server itself — nothing serves them over HTTP or MCP.

- **Live:** `docker compose logs -f anki-relay` (readable text).
- **Files:** `./logs/anki-relay.log`, one JSON object per line, a new file every day
  (UTC), the last `LOG_RETENTION_DAYS` (14) days kept. Every tool call writes a
  `tool.call` record: tool, user id, duration, outcome, arguments *without contents*
  (deck, note type, field and tag names and search queries are kept; field values,
  base64 data, template text and CSS are replaced by their length) and the numbers
  of the answer. Sync (`sync.*`) and sign-in (`auth.*`) events carry the user id, and
  sync events triggered by a tool call carry its `call_id`. Passwords, tokens,
  AnkiWeb keys, email addresses and card contents are never written; a last
  redaction pass scrubs anything that looks like a secret.
- **Debug bundle** for a bug report:

  ```bash
  docker compose exec anki-relay anki-relay debug-bundle          # --days 3 to limit
  docker compose cp anki-relay:/logs/debug-bundle-<time>.tar.gz .  # then scp it home
  ```

  It contains the logs, versions, the configuration (emails replaced by user ids) and
  each user's sync state, collection/media sizes and backup list. It never contains
  AnkiWeb keys, OAuth data, collections or media.
- **Sync diagnostics.** Every request to the sync server writes a `sync.request`
  record: step (`meta`, `full_download`, `full_upload`, `media_start`), endpoint
  host, attempt, duration and, on failure, the error exactly as the anki library
  reported it (`error_type`, `error_kind`, `error_message`, plus `http_code` and
  `http_context` when the text has the form `HttpError { code: …, context: … }`).
  Nothing is interpreted. `sync.recovered` marks the first success after failures,
  `sync.media_failed` a media sync error.
- **The anki library's own log.** `ANKI_RUST_LOG=debug` (default `off`) writes
  `./logs/anki-rust.log` (the previous run is kept as `.1`; the lines also appear
  in `docker compose logs`). At `debug` it shows the sync metadata of both sides,
  the full-sync decision (`upload_ok` / `download_ok`) and network errors; tests
  check it contains no password, AnkiWeb key or email. HTTP responses (status,
  headers, body) are not visible there or anywhere else: the library does not
  expose them. Turn it on while investigating, then back off.

Find a user's id: first 16 hex characters of the SHA-256 of the lowercase email,
e.g. `python3 -c "import hashlib;print(hashlib.sha256(b'me@example.com').hexdigest()[:16])"`.

## Maintenance

- Logs: see above; `docker compose logs -f caddy` for Caddy.
- Backup: copy the `data/` directory (stop the app first for a consistent copy).
- Update: `git pull && docker compose up -d --build`.

## Development

```bash
python -m pip install uv
python -m uv venv -p 3.12 .venv
python -m uv pip install --python .venv -e ".[dev]"
.venv/bin/python scripts/validate.py      # Windows: .venv\Scripts\python scripts\validate.py
```

`scripts/validate.py` runs everything: ruff, the full test suite (including an
integration test against a real local `anki.syncserver` that plays AnkiWeb),
repository hygiene, `docker compose config` for both modes, the Docker build and
container smoke tests. Run all of it after every change. Changes start with a
requirements document in `docs/requirements/` (see `CLAUDE.md`); the plan is in
`ROADMAP.md`.

## License

MIT — see [LICENSE](LICENSE). Anki is © Ankitects Pty Ltd and contributors and
licensed under the AGPL; this project uses its published Python package.

"""``anki-relay debug-bundle``: one archive with everything needed to debug.

Run on the server (``docker compose exec anki-relay anki-relay debug-bundle``).
The archive contains the logs, versions, the configuration with email addresses
replaced by user ids, and per-user sync state. It never contains AnkiWeb keys
(``sync_auth.json``), OAuth data (``oauth.json``), collections or media.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.metadata
import io
import json
import os
import platform
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .fileutil import read_json
from .logging_setup import LOG_FILE, RUST_LOG_FILE
from .users import user_id_for

PACKAGES = ("anki-relay", "anki", "mcp", "httpx", "uvicorn", "starlette", "pydantic")


def _versions() -> dict[str, str]:
    out = {"python": sys.version.split()[0], "platform": platform.platform()}
    for name in PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def _config(settings: Settings) -> dict[str, Any]:
    config = settings.model_dump(mode="json")
    emails = config.pop("allowed_emails")
    config["allowed_users"] = {
        "count": len(emails),
        "user_ids": sorted(user_id_for(e) for e in emails),
    }
    return config


def _dir_stats(path: Path) -> dict[str, int]:
    count = size = 0
    if path.is_dir():
        for entry in os.scandir(path):
            if entry.is_file():
                count += 1
                size += entry.stat().st_size
    return {"files": count, "bytes": size}


def _oauth_summary(settings: Settings) -> tuple[dict[str, Any], dict[str, dict[str, int]]]:
    data = read_json(settings.oauth_file, {})
    per_user: dict[str, dict[str, int]] = {}
    for kind in ("access", "refresh"):
        for entry in data.get(kind, {}).values():
            counts = per_user.setdefault(entry.get("subject", "?"), {"access": 0, "refresh": 0})
            counts[kind] += 1
    summary = {
        "clients": len(data.get("clients", {})),
        "users_signed_in": len(data.get("users", {})),
    }
    return summary, per_user


def _users(settings: Settings, tokens: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    allowed = {user_id_for(e) for e in settings.allowed_emails}
    out: dict[str, dict[str, Any]] = {}
    root = settings.users_dir
    if not root.is_dir():
        return out
    for udir in sorted(p for p in root.iterdir() if p.is_dir()):
        col = udir / "collection.anki2"
        auth = read_json(udir / "sync_auth.json", None)
        out[udir.name] = {
            "allowed": udir.name in allowed,
            "state": read_json(udir / "state.json", None),
            "has_ankiweb_login": bool(auth and auth.get("hkey")),
            "collection_bytes": col.stat().st_size if col.exists() else None,
            "media": _dir_stats(udir / "collection.media"),
            "backups": [
                {"name": b.name, "bytes": b.stat().st_size}
                for b in sorted((udir / "backups").glob("*.anki2"))
            ],
            "tokens": tokens.get(udir.name, {"access": 0, "refresh": 0}),
        }
    return out


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def _dump(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode()


def build_bundle(settings: Settings, days: int | None = None, out_dir: Path | None = None) -> Path:
    target_dir = out_dir or settings.log_dir or settings.data_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"debug-bundle-{stamp}.tar.gz"
    oauth, tokens = _oauth_summary(settings)
    cutoff = time.time() - days * 86400 if days else None

    with tarfile.open(path, "w:gz") as tar:
        _add_bytes(
            tar,
            "system.json",
            _dump(
                {
                    "created": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    "versions": _versions(),
                    "config": _config(settings),
                    "oauth": oauth,
                }
            ),
        )
        for user_id, info in _users(settings, tokens).items():
            _add_bytes(tar, f"users/{user_id}.json", _dump(info))
        if settings.log_dir is not None and settings.log_dir.is_dir():
            logs = [
                *settings.log_dir.glob(LOG_FILE + "*"),
                *settings.log_dir.glob(RUST_LOG_FILE + "*"),
            ]
            for log in sorted(logs):
                if cutoff is None or log.stat().st_mtime >= cutoff:
                    tar.add(log, arcname=f"logs/{log.name}")

    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    # Run as root via `docker compose exec`: hand the file to the owner of the folder.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        owner = target_dir.stat()
        with contextlib.suppress(OSError):
            os.chown(path, owner.st_uid, owner.st_gid)
    return path

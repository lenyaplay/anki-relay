"""Run as a separate process by test_integration_syncserver.py.

The anki library allows its Rust logging to be initialised once per process, so
this check cannot run inside the pytest process. It enables the log through
anki_relay's own setup (ANKI_RUST_LOG), then signs in, syncs and downloads the
collection from a real local sync server. Prints the hkey so the caller can check
that it does not appear in the log.

Usage: python rust_log_probe.py <endpoint> <data_dir> <log_dir> <email> <password>
"""

from __future__ import annotations

import sys
from pathlib import Path

from anki_relay.config import load_settings
from anki_relay.logging_setup import setup_rust_logging


def main() -> None:
    endpoint, data_dir, log_dir, email, password = sys.argv[1:6]
    settings = load_settings(
        public_url="http://localhost:8000",
        allowed_emails=email,
        data_dir=data_dir,
        log_dir=log_dir,
        anki_rust_log="debug",
    )
    path = setup_rust_logging(settings)
    assert path is not None and path == Path(log_dir) / "anki-rust.log"

    import anki.collection  # noqa: F401 - after logging is initialised
    from anki.collection import Collection

    folder = Path(data_dir) / "probe"
    folder.mkdir(parents=True)
    col = Collection(str(folder / "collection.anki2"))
    auth = col.sync_login(email, password, endpoint)
    col.sync_collection(auth, sync_media=False)
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
    col.reopen(after_full_sync=True)
    col.close()
    print(auth.hkey)


if __name__ == "__main__":
    main()

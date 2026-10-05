#!/bin/sh
# Make the data directory writable for the unprivileged "anki" user, then run the
# app as that user. When the container is already started as a non-root user
# (docker run --user ...), just run the command.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
LOG_DIR="${LOG_DIR-/logs}"

own() {
    mkdir -p "$1"
    # Only touch files with a different owner, so large media folders stay fast.
    find "$1" \( ! -user anki -o ! -group anki \) -exec chown anki:anki {} +
    chmod 700 "$1"
}

if [ "$(id -u)" = "0" ]; then
    own "$DATA_DIR"
    # LOG_DIR= (empty) means stdout only.
    if [ -n "$LOG_DIR" ]; then
        own "$LOG_DIR"
    fi
    exec setpriv --reuid=anki --regid=anki --init-groups "$@"
fi

exec "$@"

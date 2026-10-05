# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (better layer caching), then the package itself.
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p src/anki_relay && touch src/anki_relay/__init__.py \
    && pip install . && pip uninstall -y anki-relay && rm -rf src
COPY src ./src
RUN pip install --no-deps . && rm -rf /app/src

# The app runs as an unprivileged user. The entrypoint starts as root only to make
# a bind-mounted ./data writable for that user, then drops privileges.
RUN groupadd --system --gid 10001 anki \
    && useradd --system --uid 10001 --gid anki --home-dir /data --shell /usr/sbin/nologin anki \
    && mkdir -p /data /logs && chown anki:anki /data /logs
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENV DATA_DIR=/data LOG_DIR=/logs HOST=0.0.0.0 PORT=8000
VOLUME ["/data", "/logs"]
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["anki-relay"]

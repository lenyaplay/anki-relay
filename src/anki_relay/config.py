"""All configuration is read and validated here, once, at startup."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Everything Anki can show or play.
DEFAULT_MEDIA_EXTENSIONS = frozenset(
    [
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "svg",
        "avif",
        "bmp",
        "tif",
        "tiff",
        "ico",
        "mp3",
        "ogg",
        "oga",
        "opus",
        "wav",
        "flac",
        "m4a",
        "aac",
        "mp4",
        "webm",
        "mkv",
        "mov",
    ]
)

DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128", "172.16.0.0/12")


class ConfigError(Exception):
    """Invalid or missing configuration; the message names the variable."""


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    public_url: str
    allowed_emails: CsvList

    disabled_tools: CsvList = Field(default_factory=list)
    media_sync: bool = True
    media_allowed_extensions: CsvList = Field(
        default_factory=lambda: sorted(DEFAULT_MEDIA_EXTENSIONS)
    )
    media_max_mb: float = Field(default=100, gt=0)
    media_download_timeout: float = Field(default=60, gt=0)
    media_allow_private_urls: bool = False
    sync_pull_interval: float = Field(default=60, ge=0)
    sync_push_delay: float = Field(default=10, ge=0)
    sync_push_max_delay: float = Field(default=60, ge=0)
    sync_endpoint: str = ""
    allow_schema_changes: bool = True
    backup_keep: int = Field(default=10, ge=1)
    max_batch: int = Field(default=500, ge=1)
    access_token_ttl: int = Field(default=3600, ge=60)
    refresh_token_ttl_days: float = Field(default=90, gt=0)
    login_max_fails: int = Field(default=10, ge=1)
    login_fail_window: int = Field(default=900, ge=1)
    collection_idle_minutes: float = Field(default=30, gt=0)
    default_language: Literal["auto", "ru", "en"] = "auto"
    trusted_proxies: CsvList = Field(default_factory=lambda: list(DEFAULT_TRUSTED_PROXIES))
    host: str = "0.0.0.0"  # noqa: S104 - the app listens inside a container
    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = Path("/data")
    log_dir: Path | None = Path("/logs")
    log_retention_days: int = Field(default=14, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("public_url")
    @classmethod
    def _check_public_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("must be set, e.g. https://anki-relay.example.com")
        parts = urlsplit(value)
        local_http = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")
        if parts.scheme != "https" and not local_http:
            raise ValueError(
                "must start with https:// (http:// is only allowed for localhost and 127.0.0.1)"
            )
        if not parts.hostname:
            raise ValueError("must contain a host name")
        if parts.path or parts.query or parts.fragment:
            raise ValueError("must be the bare server address without a path (no /mcp)")
        return value

    @field_validator(
        "allowed_emails",
        "disabled_tools",
        "media_allowed_extensions",
        "trusted_proxies",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("allowed_emails")
    @classmethod
    def _check_emails(cls, value: list[str]) -> list[str]:
        emails = [email.lower() for email in value]
        if not emails:
            raise ValueError("must contain at least one AnkiWeb email address")
        bad = [email for email in emails if "@" not in email]
        if bad:
            raise ValueError(f"not an email address: {', '.join(bad)}")
        return emails

    @field_validator("media_allowed_extensions")
    @classmethod
    def _check_extensions(cls, value: list[str]) -> list[str]:
        exts = [ext.lower().lstrip(".") for ext in value]
        if not exts:
            raise ValueError("must list extensions or be * to allow any file")
        return exts

    @field_validator("trusted_proxies")
    @classmethod
    def _check_proxies(cls, value: list[str]) -> list[str]:
        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"not an IP address or network: {item}") from exc
        return value

    @field_validator("log_dir", mode="before")
    @classmethod
    def _empty_log_dir(cls, value: object) -> object:
        # LOG_DIR= (empty) means: log to stdout only.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("sync_endpoint")
    @classmethod
    def _check_endpoint(cls, value: str) -> str:
        value = value.strip()
        if value and urlsplit(value).scheme not in ("http", "https"):
            raise ValueError("must be an http(s) URL or empty for AnkiWeb")
        if value and not value.endswith("/"):
            value += "/"
        return value

    # Derived values -----------------------------------------------------

    @property
    def any_media_extension(self) -> bool:
        return "*" in self.media_allowed_extensions

    @property
    def media_max_bytes(self) -> int:
        return int(self.media_max_mb * 1024 * 1024)

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(item, strict=False) for item in self.trusted_proxies]

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @property
    def oauth_file(self) -> Path:
        return self.data_dir / "oauth.json"


def load_settings(**overrides: object) -> Settings:
    """Read settings from the environment (plus overrides) with readable errors."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        lines = []
        for error in exc.errors():
            name = str(error["loc"][0]).upper() if error["loc"] else "?"
            message = error["msg"]
            if error["type"] == "missing":
                message = "is required but not set"
            message = message.removeprefix("Value error, ")
            lines.append(f"  {name}: {message}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from None

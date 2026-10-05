"""``add_media`` checks and safe downloading.

Defaults allow everything Anki can show or play; each protection can be relaxed
in the configuration. SVG is not sanitised: note fields accept arbitrary HTML
anyway, so sanitising SVG alone would add nothing.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import ipaddress
import mimetypes
import re
import socket
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .config import Settings

IMAGE_EXTENSIONS = frozenset(
    ["png", "jpg", "jpeg", "gif", "webp", "svg", "avif", "bmp", "tif", "tiff", "ico"]
)
MAX_REDIRECTS = 5
MAX_NAME_LENGTH = 120

MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/tiff": "tif",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "video/quicktime": "mov",
}

Resolver = Callable[[str, int], Awaitable[list[str]]]


class MediaError(Exception):
    """Rejected media; the message is shown to Claude."""


@dataclass
class MediaFile:
    filename: str
    data: bytes


def sanitize_filename(name: str) -> str:
    """Strip paths, '..', control and reserved characters; keep it readable."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r'[<>:"|?*\[\]]', "_", name)
    name = name.replace("..", "_").strip().lstrip(".").strip()
    if len(name) > MAX_NAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: MAX_NAME_LENGTH - len(ext) - 1] + "." + ext
        else:
            name = name[:MAX_NAME_LENGTH]
    return name or "file"


def extension_of(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot else ""


def ext_for_content_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    mime = content_type.split(";")[0].strip().lower()
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    guessed = mimetypes.guess_extension(mime) or ""
    return guessed.lstrip(".")


def with_extension(name: str, content_type: str | None) -> str:
    if extension_of(name):
        return name
    ext = ext_for_content_type(content_type)
    return f"{name}.{ext}" if ext else name


def check_extension(name: str, settings: Settings) -> None:
    if settings.any_media_extension:
        return
    ext = extension_of(name)
    if ext not in settings.media_allowed_extensions:
        allowed = ", ".join(sorted(settings.media_allowed_extensions))
        shown = f".{ext}" if ext else "no extension"
        raise MediaError(f"File type {shown} is not allowed. Allowed: {allowed}")


def field_snippet(filename: str) -> str:
    """What to put into a note field to show or play the file."""
    if extension_of(filename) in IMAGE_EXTENSIONS:
        return f'<img src="{html.escape(filename, quote=True)}">'
    return f"[sound:{filename}]"


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def decode_base64(data: str, settings: Settings) -> tuple[bytes, str | None]:
    """Decode plain base64 or a data: URI. Returns (bytes, content type or None)."""
    content_type = None
    text = data.strip()
    if text.startswith("data:"):
        header, _, text = text.partition(",")
        content_type = header[5:].split(";")[0] or None
    text = re.sub(r"\s+", "", text)
    if len(text) * 3 // 4 > settings.media_max_bytes + 3:
        raise MediaError(f"File is larger than the limit of {settings.media_max_mb:g} MB.")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaError(f"data_base64 is not valid base64: {exc}") from None
    if len(raw) > settings.media_max_bytes:
        raise MediaError(f"File is larger than the limit of {settings.media_max_mb:g} MB.")
    return raw, content_type


class MediaFetcher:
    """Downloads media by URL with SSRF protection and a streaming size limit."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.resolver = resolver or system_resolver

    async def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise MediaError("Only http and https URLs are allowed.")
        if not parts.hostname:
            raise MediaError("URL has no host.")
        if self.settings.media_allow_private_urls:
            return
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await self.resolver(parts.hostname, port)
        except OSError as exc:
            raise MediaError(f"Cannot resolve {parts.hostname}: {exc}") from None
        if not addresses:
            raise MediaError(f"Cannot resolve {parts.hostname}.")
        blocked = [a for a in addresses if not is_public_address(a)]
        if blocked:
            raise MediaError(
                f"{parts.hostname} resolves to a private or internal address ({blocked[0]}); "
                "downloads from the internal network are disabled "
                "(MEDIA_ALLOW_PRIVATE_URLS=false)."
            )

    async def fetch(self, url: str) -> tuple[bytes, str | None, str]:
        """Return (data, content type, final URL)."""
        try:
            async with asyncio.timeout(self.settings.media_download_timeout):
                return await self._fetch(url)
        except TimeoutError:
            raise MediaError(
                f"Download timed out after {self.settings.media_download_timeout:g} s."
            ) from None
        except httpx.HTTPError as exc:
            raise MediaError(f"Download failed: {exc}") from None

    async def _fetch(self, url: str) -> tuple[bytes, str | None, str]:
        limit = self.settings.media_max_bytes
        too_big = f"File is larger than the limit of {self.settings.media_max_mb:g} MB."
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=self.settings.media_download_timeout,
            headers={"User-Agent": "anki-relay"},
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await self._check_url(url)
                async with client.stream("GET", url) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise MediaError("Redirect without a Location header.")
                        url = urljoin(url, location)
                        continue
                    if resp.status_code >= 400:
                        raise MediaError(f"Server answered HTTP {resp.status_code}.")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise MediaError(too_big)
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise MediaError(too_big)
                    return bytes(chunks), resp.headers.get("content-type"), url
            raise MediaError(f"More than {MAX_REDIRECTS} redirects.")


def name_from_url(url: str) -> str:
    path = unquote(urlsplit(url).path)
    return path.rsplit("/", 1)[-1]


async def prepare_media(
    settings: Settings,
    fetcher: MediaFetcher,
    filename: str,
    url: str | None,
    data_base64: str | None,
) -> MediaFile:
    """Validate the request and load the bytes (no collection access here)."""
    if (url is None) == (data_base64 is None):
        raise MediaError("Pass exactly one source: url or data_base64.")
    if url is not None:
        data, content_type, final_url = await fetcher.fetch(url)
        name = filename or name_from_url(final_url)
    else:
        data, content_type = decode_base64(data_base64 or "", settings)
        name = filename
    name = with_extension(sanitize_filename(name), content_type)
    check_extension(name, settings)
    if not data:
        raise MediaError("File is empty.")
    return MediaFile(filename=name, data=data)

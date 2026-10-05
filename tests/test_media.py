"""add_media: sources, limits, extensions, SSRF protection."""

from __future__ import annotations

import base64
import ipaddress

import httpx
import pytest

from anki_relay.media import MediaFetcher, sanitize_filename

from .conftest import EMAIL, EMAIL_B
from .test_tools import PNG

HOSTS = {
    "cdn.example.com": ["93.184.216.34"],
    "evil.example.com": ["10.0.0.5"],
    "rebind.example.com": ["93.184.216.35", "127.0.0.1"],
    "metadata.example.com": ["169.254.169.254"],
    "nas.local": ["192.168.1.20"],
}


async def resolver(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        if host not in HOSTS:
            raise OSError("unknown host") from None
        return HOSTS[host]


PNG_BYTES = base64.b64decode(PNG)


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/cat":
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
    if path == "/song.mp3":
        return httpx.Response(
            200, content=b"ID3" + b"\0" * 100, headers={"content-type": "audio/mpeg"}
        )
    if path == "/redirect-private":
        return httpx.Response(302, headers={"location": "http://evil.example.com/x.png"})
    if path == "/redirect-ok":
        return httpx.Response(301, headers={"location": "/cat"})
    if path.startswith("/loop"):
        n = int(path.removeprefix("/loop") or 0)
        return httpx.Response(302, headers={"location": f"/loop{n + 1}"})
    if path == "/big":
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "image/png"})
    if path == "/big-stream":

        async def gen():
            for _ in range(10):
                yield b"x" * 1000

        return httpx.Response(200, content=gen(), headers={"content-type": "image/png"})
    if path == "/private.png":
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
    return httpx.Response(404)


@pytest.fixture
async def mh(harness_factory):
    async def make(**overrides):
        h = await harness_factory(**overrides)
        h.app.registry.runtime.fetcher = MediaFetcher(
            h.settings, transport=httpx.MockTransport(handler), resolver=resolver
        )
        h.sign_in(EMAIL)
        h.sign_in(EMAIL_B)
        await h.call("collection_overview")
        return h

    return make


async def test_base64_image_returns_field_snippet(mh) -> None:
    h = await mh()
    res = await h.call("add_media", filename="dot.png", data_base64=PNG)
    assert res == {"filename": "dot.png", "field": '<img src="dot.png">', "bytes": len(PNG_BYTES)}
    data_uri = "data:image/png;base64," + PNG
    res = await h.call("add_media", filename="no-extension", data_base64=data_uri)
    assert res["filename"] == "no-extension.png"
    audio = await h.call(
        "add_media", filename="a.mp3", data_base64=base64.b64encode(b"ID3x").decode()
    )
    assert audio["field"] == "[sound:a.mp3]"
    video = await h.call(
        "add_media", filename="v.mp4", data_base64=base64.b64encode(b"vid").decode()
    )
    assert video["field"] == "[sound:v.mp4]"


async def test_name_collision_returns_name_given_by_anki(mh) -> None:
    h = await mh()
    first = await h.call("add_media", filename="same.png", data_base64=PNG)
    other = base64.b64encode(PNG_BYTES + b"different").decode()
    second = await h.call("add_media", filename="same.png", data_base64=other)
    assert first["filename"] == "same.png" and second["filename"] != "same.png"
    assert second["field"] == f'<img src="{second["filename"]}">'


async def test_url_download_and_content_type_extension(mh) -> None:
    h = await mh()
    res = await h.call("add_media", filename="", url="https://cdn.example.com/cat")
    assert res["filename"] == "cat.png"
    res = await h.call("add_media", filename="kitty", url="https://cdn.example.com/redirect-ok")
    assert res["filename"] == "kitty.png"
    res = await h.call("add_media", filename="", url="https://cdn.example.com/song.mp3")
    assert res["field"] == "[sound:song.mp3]"


async def test_forbidden_type_rejected(mh) -> None:
    h = await mh()
    msg = await h.fails("add_media", filename="run.exe", data_base64=PNG)
    assert ".exe is not allowed" in msg and "png" in msg


async def test_size_limit(mh) -> None:
    h = await mh(media_max_mb=0.001)  # ~1 KB
    big = base64.b64encode(b"x" * 5000).decode()
    assert "larger than the limit" in await h.fails("add_media", filename="b.png", data_base64=big)
    for path in ("/big", "/big-stream"):
        msg = await h.fails("add_media", filename="b.png", url="https://cdn.example.com" + path)
        assert "larger than the limit" in msg


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.png",
        "http://10.1.2.3/x.png",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.png",
        "http://metadata.example.com/x.png",
        "http://rebind.example.com/x.png",
        "https://cdn.example.com/redirect-private",
    ],
)
async def test_private_addresses_rejected(mh, url: str) -> None:
    h = await mh()
    msg = await h.fails("add_media", filename="x.png", url=url)
    assert "private or internal address" in msg


async def test_other_url_problems(mh) -> None:
    h = await mh()
    assert "http and https" in await h.fails(
        "add_media", filename="x.png", url="file:///etc/passwd"
    )
    assert "redirects" in await h.fails(
        "add_media", filename="x.png", url="https://cdn.example.com/loop"
    )
    assert "HTTP 404" in await h.fails(
        "add_media", filename="x.png", url="https://cdn.example.com/nope"
    )
    assert "exactly one source" in await h.fails("add_media", filename="x.png")
    assert "exactly one source" in await h.fails(
        "add_media", filename="x.png", url="https://cdn.example.com/cat", data_base64=PNG
    )
    assert "not valid base64" in await h.fails("add_media", filename="x.png", data_base64="@@@")


async def test_private_urls_allowed_when_enabled(mh) -> None:
    h = await mh(media_allow_private_urls=True)
    res = await h.call("add_media", filename="", url="http://nas.local/private.png")
    assert res["filename"] == "private.png"


async def test_media_sync_off_disables_add_media(harness_factory) -> None:
    h = await harness_factory(media_sync=False)
    names = await h.tool_names()
    assert "add_media" not in names and "list_media" in names


async def test_extension_settings(mh, tmp_path) -> None:
    h = await mh(media_allowed_extensions="*")
    assert (await h.call("add_media", filename="notes.txt", data_base64=PNG))[
        "filename"
    ] == "notes.txt"
    h2 = await mh(media_allowed_extensions="png,jpg", data_dir=tmp_path / "second")
    assert (await h2.call("add_media", filename="ok.png", data_base64=PNG))["filename"] == "ok.png"
    msg = await h2.fails("add_media", filename="song.mp3", data_base64=PNG)
    assert ".mp3 is not allowed" in msg and "jpg, png" in msg


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\win.ini", "win.ini"),
        ("a\x00b\x1fc.png", "abc.png"),
        ("..hidden.png", "_hidden.png"),
        ("we..ird.png", "we_ird.png"),
        ("", "file"),
        ("x" * 300 + ".png", "x" * 116 + ".png"),
    ],
)
def test_sanitize_filename(raw: str, clean: str) -> None:
    assert sanitize_filename(raw) == clean

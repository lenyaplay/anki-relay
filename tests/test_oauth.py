"""OAuth 2.1 + AnkiWeb sign-in against a live HTTP server."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from anki.errors import NetworkError

from .conftest import CALLBACK, EMAIL, EMAIL_B, PASSWORD, LiveServer, mcp_session


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


class Flow:
    def __init__(self, server: LiveServer) -> None:
        self.server = server
        self.http = httpx.Client(base_url=server.url, follow_redirects=False, timeout=30)
        self.verifier, self.challenge = pkce()
        self.state = secrets.token_urlsafe(8)
        self.client_id: str | None = None

    def register(self) -> str:
        r = self.http.post(
            "/register",
            json={
                "redirect_uris": [CALLBACK],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "client_name": "Claude",
            },
        )
        assert r.status_code == 201, r.text
        self.client_id = r.json()["client_id"]
        return self.client_id

    def authorize(self) -> str:
        r = self.http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": CALLBACK,
                "code_challenge": self.challenge,
                "code_challenge_method": "S256",
                "state": self.state,
                "scope": "anki",
            },
        )
        assert r.status_code == 302, r.text
        location = r.headers["location"]
        assert location.startswith(self.server.url + "/login?req=")
        return parse_qs(urlsplit(location).query)["req"][0]

    def login(self, req: str, email: str = EMAIL, password: str = PASSWORD, **headers):
        return self.http.post(
            "/login", data={"req": req, "email": email, "password": password}, headers=headers
        )

    def code(self, email: str = EMAIL) -> str:
        self.register()
        r = self.login(self.authorize(), email=email)
        assert r.status_code == 302, r.text
        query = parse_qs(urlsplit(r.headers["location"]).query)
        assert r.headers["location"].startswith(CALLBACK)
        assert query["state"] == [self.state]
        return query["code"][0]

    def exchange(self, code: str) -> httpx.Response:
        return self.http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": self.client_id,
                "code_verifier": self.verifier,
            },
        )

    def refresh(self, refresh_token: str) -> httpx.Response:
        return self.http.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
        )

    def tokens(self, email: str = EMAIL) -> dict:
        r = self.exchange(self.code(email))
        assert r.status_code == 200, r.text
        return r.json()


async def mcp_call(server: LiveServer, token: str, tool: str, args: dict | None = None):
    async with mcp_session(server.url, token) as (session, _):
        result = await session.call_tool(tool, args or {})
        assert not result.isError, result.content
        return json.loads(result.content[0].text)  # type: ignore[union-attr]


def mcp_status(server: LiveServer, token: str | None, host: str | None = None) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if host:
        headers["Host"] = host
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    return httpx.post(server.url + "/mcp", json=body, headers=headers, timeout=30)


# ---------------------------------------------------------------- discovery


def test_unauthenticated_request_gets_401_with_resource_metadata(live_factory) -> None:
    server = live_factory()
    r = mcp_status(server, None)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource/mcp" in challenge


def test_well_known_metadata(live_factory) -> None:
    server = live_factory()
    prm = httpx.get(server.url + "/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == server.url + "/mcp"
    assert prm["authorization_servers"] == [server.url + "/"]
    asm = httpx.get(server.url + "/.well-known/oauth-authorization-server").json()
    assert asm["registration_endpoint"] == server.url + "/register"
    assert asm["revocation_endpoint"] == server.url + "/revoke"
    assert "S256" in asm["code_challenge_methods_supported"]


# ---------------------------------------------------------------- sign-in


def test_full_flow_and_one_time_links(live_factory, fake) -> None:
    server = live_factory()
    flow = Flow(server)
    flow.register()
    req = flow.authorize()
    page = flow.http.get("/login", params={"req": req})
    assert page.status_code == 200 and 'name="password"' in page.text

    r = flow.login(req)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith(CALLBACK + "?")
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == [flow.state] and query["code"]

    again = flow.login(req)  # the sign-in link is single-use
    assert again.status_code == 400
    tokens = flow.exchange(query["code"][0])
    assert tokens.status_code == 200
    body = tokens.json()
    assert body["token_type"].lower() == "bearer" and body["refresh_token"]
    reuse = flow.exchange(query["code"][0])  # the code is single-use
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"

    # only the hkey and endpoint are stored; the password nowhere
    data_dir = server.app.settings.data_dir
    stored = json.loads((server.app.users.for_email(EMAIL).auth_path).read_text())
    assert set(stored) == {"hkey", "endpoint"}
    for path in data_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ""}:
            assert PASSWORD not in path.read_text(errors="ignore")


def test_email_not_allowed_is_refused_without_ankiweb(live_factory, fake) -> None:
    server = live_factory()
    flow = Flow(server)
    flow.register()
    r = flow.login(flow.authorize(), email="stranger@example.com")
    assert r.status_code == 401
    assert "Invalid email or password" in r.text or "Неверный email или пароль" in r.text
    assert fake.calls["login"] == 0


def test_wrong_password(live_factory, fake) -> None:
    server = live_factory()
    flow = Flow(server)
    flow.register()
    req = flow.authorize()
    r = flow.login(req, password="nope", **{"Accept-Language": "en"})
    assert r.status_code == 401 and "Invalid email or password." in r.text
    assert fake.calls["login"] == 1
    assert flow.login(req).status_code == 302  # the same link still works after a typo


def test_sync_network_error_during_sign_in(live_factory, fake) -> None:
    server = live_factory(default_language="ru")
    flow = Flow(server)
    flow.register()
    fake.login_error = NetworkError("down", None, None, None)
    r = flow.login(flow.authorize())
    assert r.status_code == 502 and "Сетевая ошибка при обращении к серверу синхронизации" in r.text


# ---------------------------------------------------------------- tokens


def test_refresh_rotation(live_factory) -> None:
    server = live_factory()
    flow = Flow(server)
    first = flow.tokens()
    r = flow.refresh(first["refresh_token"])
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    old = flow.refresh(first["refresh_token"])
    assert old.status_code == 400 and old.json()["error"] == "invalid_grant"
    assert mcp_status(server, first["access_token"]).status_code == 401
    assert mcp_status(server, second["access_token"]).status_code == 200

    revoke = flow.http.post(
        "/revoke",
        # the SDK's RevocationRequest requires the client_secret key, even if empty
        data={"token": second["refresh_token"], "client_id": flow.client_id, "client_secret": ""},
    )
    assert revoke.status_code == 200
    assert mcp_status(server, second["access_token"]).status_code == 401


async def test_token_survives_restart_and_allowlist_revocation(live_factory) -> None:
    server = live_factory()
    tokens = Flow(server).tokens()
    token = tokens["access_token"]
    added = await mcp_call(
        server,
        token,
        "add_notes",
        {"notes": [{"deck": "R", "note_type": "Basic", "fields": {"Front": "persist"}}]},
    )
    assert added["added"] == 1
    server.stop()

    restarted = live_factory()
    found = await mcp_call(restarted, token, "find_notes", {"query": "deck:R"})
    assert found["total"] == 1
    restarted.stop()

    reduced = live_factory(allowed_emails=EMAIL_B)
    assert mcp_status(reduced, token).status_code == 401
    flow = Flow(reduced)
    flow.register()
    assert flow.refresh(tokens["refresh_token"]).status_code == 400


# ---------------------------------------------------------------- transport security


def test_foreign_host_rejected(live_factory) -> None:
    server = live_factory()
    token = Flow(server).tokens()["access_token"]
    assert mcp_status(server, token).status_code == 200
    assert mcp_status(server, token, host="evil.example.com").status_code == 421
    r = httpx.get(server.url + "/login?req=x", headers={"Host": "evil.example.com"})
    assert r.status_code == 421
    assert httpx.get(server.url + "/health", headers={"Host": "anything"}).status_code == 200


def test_ip_blocking(live_factory, fake) -> None:
    server = live_factory(login_max_fails=3)
    flow = Flow(server)
    flow.register()
    req = flow.authorize()
    xff = {"X-Forwarded-For": "203.0.113.7"}
    for _ in range(3):
        assert flow.login(req, password="bad", **xff).status_code == 401
    blocked = flow.login(req, **xff)
    assert blocked.status_code == 429
    logins = fake.calls["login"]
    # another client IP (behind the trusted proxy) is not affected
    assert flow.login(req, **{"X-Forwarded-For": "198.51.100.9"}).status_code == 302
    assert fake.calls["login"] == logins + 1


def test_global_limit(live_factory) -> None:
    server = live_factory(login_max_fails=1)
    flow = Flow(server)
    flow.register()
    req = flow.authorize()
    for i in range(5):
        flow.login(req, password="bad", **{"X-Forwarded-For": f"203.0.113.{i}"})
    r = flow.login(req, **{"X-Forwarded-For": "198.51.100.1"})
    assert r.status_code == 429


def test_untrusted_peer_cannot_spoof_forwarded_for(live_factory) -> None:
    from starlette.requests import Request

    from anki_relay.auth import client_ip

    server = live_factory(trusted_proxies="10.0.0.0/8")
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4")],
        "client": ("127.0.0.1", 5000),
    }
    assert client_ip(Request(scope), server.app.settings) == "127.0.0.1"
    scope["client"] = ("10.0.0.2", 5000)
    assert client_ip(Request(scope), server.app.settings) == "1.2.3.4"


# ---------------------------------------------------------------- language


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("en-US,en;q=0.9", "Sign in with your AnkiWeb account"),
        ("ru-RU,ru;q=0.9", "Войдите аккаунтом AnkiWeb"),
    ],
)
def test_accept_language(live_factory, accept: str, expected: str) -> None:
    server = live_factory()
    flow = Flow(server)
    flow.register()
    page = flow.http.get(
        "/login", params={"req": flow.authorize()}, headers={"Accept-Language": accept}
    )
    assert expected in page.text


def test_language_switch_keeps_request_and_sets_cookie(live_factory) -> None:
    server = live_factory()
    flow = Flow(server)
    flow.register()
    req = flow.authorize()
    page = flow.http.get("/login", params={"req": req}, headers={"Accept-Language": "en"})
    links = re.findall(r'href="([^"]+)"', page.text)
    ru_link = next(link for link in links if "lang=ru" in link).replace("&amp;", "&")
    assert f"req={req}" in ru_link
    switched = flow.http.get(ru_link, headers={"Accept-Language": "en"})
    assert "Войти" in switched.text and switched.cookies.get("lang") == "ru"
    again = flow.http.get("/login", params={"req": req}, headers={"Accept-Language": "en"})
    assert "Войти" in again.text  # the cookie wins over the header
    r = flow.login(req, password="bad")
    assert "Неверный email или пароль" in r.text


def test_login_page_escapes_input(live_factory) -> None:
    server = live_factory()
    flow = Flow(server)
    flow.register()
    r = flow.login(flow.authorize(), email='"><script>alert(1)</script>@x.com', password="x")
    assert "<script>alert(1)</script>" not in r.text
    assert r.headers["content-security-policy"].startswith("default-src 'none'")

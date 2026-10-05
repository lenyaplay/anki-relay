"""OAuth 2.1 authorization server with sign-in through AnkiWeb.

Flow: Claude registers itself (DCR) → ``/authorize`` (PKCE checked by the SDK) →
our ``/login?req=…`` page → AnkiWeb checks email and password → only the AnkiWeb
session key is stored → redirect back to Claude with a one-time code → tokens.

Clients and tokens persist in ``data/oauth.json`` (tokens stored as SHA-256 hashes)
so a restart does not sign anybody out. Every token check also verifies that the
owner's email is still in ``ALLOWED_EMAILS``.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import ipaddress
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import anki.collection  # noqa: F401
from anki.errors import NetworkError, SyncError, SyncErrorKind
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import Settings
from .fileutil import read_json, write_private_json
from .login_page import pick_language, render_expired, render_login
from .sync import AnkiSyncBackend, error_fields
from .users import UserManager, user_id_for

log = logging.getLogger(__name__)

LOGIN_REQUEST_TTL = 600
AUTH_CODE_TTL = 300
MAX_CLIENTS = 500
UNUSED_CLIENT_TTL = 24 * 3600
SCOPE = "anki"

# Seconds to wait after each failed sign-in (tests lower it).
FAIL_DELAY = 1.0


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class LoginRequest:
    client_id: str
    params: AuthorizationParams
    expires_at: float


class OAuthStore:
    """Clients, token hashes and user_id → email, persisted atomically."""

    def __init__(self, path: Any) -> None:
        self.path = path
        data = read_json(path, {})
        self.clients: dict[str, dict[str, Any]] = data.get("clients", {})
        self.access: dict[str, dict[str, Any]] = data.get("access", {})
        self.refresh: dict[str, dict[str, Any]] = data.get("refresh", {})
        self.users: dict[str, str] = data.get("users", {})

    def save(self) -> None:
        self.cleanup()
        write_private_json(
            self.path,
            {
                "clients": self.clients,
                "access": self.access,
                "refresh": self.refresh,
                "users": self.users,
            },
        )

    def cleanup(self) -> None:
        now = time.time()
        for table in (self.access, self.refresh):
            for key in [k for k, v in table.items() if v["expires_at"] < now]:
                del table[key]
        in_use = {v["client_id"] for v in self.refresh.values()}
        in_use |= {v["client_id"] for v in self.access.values()}
        stale = [
            cid
            for cid, c in self.clients.items()
            if cid not in in_use and now - c.get("created", 0) > UNUSED_CLIENT_TTL
        ]
        for cid in stale:
            del self.clients[cid]
        if len(self.clients) > MAX_CLIENTS:
            spare = sorted(
                (c.get("created", 0), cid) for cid, c in self.clients.items() if cid not in in_use
            )
            for _, cid in spare[: len(self.clients) - MAX_CLIENTS]:
                del self.clients[cid]

    def revoke_family(self, family: str) -> None:
        for table in (self.access, self.refresh):
            for key in [k for k, v in table.items() if v.get("family") == family]:
                del table[key]


class LoginLimiter:
    """Failed sign-ins per IP and globally within a sliding window."""

    def __init__(self, max_fails: int, window: float) -> None:
        self.max_fails = max_fails
        self.window = window
        self.per_ip: dict[str, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )
        self.total: collections.deque[float] = collections.deque()

    def _trim(self, q: collections.deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def blocked(self, ip: str) -> bool:
        now = time.monotonic()
        q = self.per_ip.get(ip)
        if q is not None:
            self._trim(q, now)
        self._trim(self.total, now)
        return (q is not None and len(q) >= self.max_fails) or len(self.total) >= self.max_fails * 5

    def fail(self, ip: str) -> None:
        now = time.monotonic()
        self.per_ip[ip].append(now)
        self.total.append(now)


def client_ip(request: Request, settings: Settings) -> str:
    """Real client IP: X-Forwarded-For is honoured only from a trusted proxy."""
    peer = request.client.host if request.client else "unknown"
    nets = settings.proxy_networks

    def trusted(addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr.strip())
        except ValueError:
            return False
        return any(ip in net for net in nets)

    if not trusted(peer):
        return peer
    chain = [a.strip() for a in request.headers.get("x-forwarded-for", "").split(",") if a.strip()]
    for addr in reversed(chain):
        if not trusted(addr):
            return addr
    return chain[0] if chain else peer


@dataclass
class RelayOAuthProvider:
    settings: Settings
    users: UserManager
    backend: AnkiSyncBackend
    store: OAuthStore = field(init=False)
    login_requests: dict[str, LoginRequest] = field(default_factory=dict)
    codes: dict[str, AuthorizationCode] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.store = OAuthStore(self.settings.oauth_file)
        self.limiter = LoginLimiter(self.settings.login_max_fails, self.settings.login_fail_window)

    # ------------------------------------------------------------------ helpers

    def _allowed(self, subject: str | None) -> bool:
        email = self.store.users.get(subject or "")
        return email is not None and email in self.settings.allowed_emails

    def _issue(
        self, client_id: str, scopes: list[str], subject: str, resource: str | None
    ) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        family = secrets.token_urlsafe(12)
        base = {"client_id": client_id, "scopes": scopes, "subject": subject, "family": family}
        if resource:
            base["resource"] = resource
        self.store.access[_hash(access)] = {
            **base,
            "expires_at": now + self.settings.access_token_ttl,
        }
        self.store.refresh[_hash(refresh)] = {
            **base,
            "expires_at": now + self.settings.refresh_token_ttl_days * 86400,
        }
        self.store.save()
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 - not a password
            expires_in=self.settings.access_token_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    def _expire_old(self) -> None:
        now = time.time()
        for key in [k for k, v in self.login_requests.items() if v.expires_at < now]:
            del self.login_requests[key]
        for key in [k for k, v in self.codes.items() if v.expires_at < now]:
            del self.codes[key]

    # ------------------------------------------------------------------ clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        entry = self.store.clients.get(client_id)
        if entry is None:
            return None
        return OAuthClientInformationFull.model_validate(entry["info"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id
        self.store.clients[client_info.client_id] = {
            "info": client_info.model_dump(mode="json", exclude_none=True),
            "created": time.time(),
        }
        self.store.save()

    # ------------------------------------------------------------------ authorize

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        self._expire_old()
        req_id = secrets.token_urlsafe(32)
        assert client.client_id
        self.login_requests[req_id] = LoginRequest(
            client_id=client.client_id,
            params=params,
            expires_at=time.time() + LOGIN_REQUEST_TTL,
        )
        return f"{self.settings.public_url}/login?req={req_id}"

    def pending_login(self, req_id: str | None) -> LoginRequest | None:
        self._expire_old()
        return self.login_requests.get(req_id or "")

    def complete_login(self, req_id: str, user_id: str, email: str) -> str:
        """Consume the login request, create a one-time code, return the redirect URL."""
        req = self.login_requests.pop(req_id)
        self.store.users[user_id] = email
        self.store.save()
        code = secrets.token_urlsafe(32)
        p = req.params
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=p.scopes or [SCOPE],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=req.client_id,
            code_challenge=p.code_challenge,
            redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=p.resource,
            subject=user_id,
        )
        return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    # ------------------------------------------------------------------ codes & tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._expire_old()
        code = self.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or not self._allowed(code.subject):
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        log.info(
            "tokens issued for user %s",
            authorization_code.subject,
            extra={"event": "auth.token", "user": authorization_code.subject},
        )
        assert client.client_id and authorization_code.subject
        return self._issue(
            client.client_id,
            authorization_code.scopes,
            authorization_code.subject,
            authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        entry = self.store.refresh.get(_hash(refresh_token))
        if (
            entry is None
            or entry["client_id"] != client.client_id
            or entry["expires_at"] < time.time()
            or not self._allowed(entry["subject"])
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=entry["client_id"],
            scopes=entry["scopes"],
            expires_at=int(entry["expires_at"]),
            resource=entry.get("resource"),
            subject=entry["subject"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        entry = self.store.refresh.pop(_hash(refresh_token.token), None)
        if entry is None:
            raise TokenError("invalid_grant", "refresh token already used")
        # Rotation: the old family (its access tokens too) stops working.
        self.store.revoke_family(entry["family"])
        log.info(
            "tokens refreshed for user %s",
            entry["subject"],
            extra={"event": "auth.refresh", "user": entry["subject"]},
        )
        assert client.client_id
        return self._issue(
            client.client_id, scopes or entry["scopes"], entry["subject"], entry.get("resource")
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        entry = self.store.access.get(_hash(token))
        if entry is None or entry["expires_at"] < time.time():
            return None
        if not self._allowed(entry["subject"]):
            log.info(
                "token of user %s rejected: email no longer allowed",
                entry["subject"],
                extra={"event": "auth.denied", "user": entry["subject"]},
            )
            return None
        return AccessToken(
            token=token,
            client_id=entry["client_id"],
            scopes=entry["scopes"],
            expires_at=int(entry["expires_at"]),
            resource=entry.get("resource"),
            subject=entry["subject"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        table = self.store.access if isinstance(token, AccessToken) else self.store.refresh
        entry = table.get(_hash(token.token))
        if entry is not None:
            self.store.revoke_family(entry["family"])
            self.store.save()
            log.info(
                "tokens revoked for user %s",
                entry["subject"],
                extra={"event": "auth.revoke", "user": entry["subject"]},
            )

    # ------------------------------------------------------------------ /login

    async def login_endpoint(self, request: Request) -> Response:
        s = self.settings
        form: dict[str, Any] = {}
        if request.method == "POST":
            form = dict(await request.form())
        query_lang = request.query_params.get("lang") or form.get("lang")
        lang = pick_language(
            query_lang,
            request.cookies.get("lang"),
            request.headers.get("accept-language"),
            s.default_language,
        )
        req_id = request.query_params.get("req") or form.get("req")
        pending = self.pending_login(str(req_id) if req_id else None)

        def page(body: str, status: int = 200) -> Response:
            resp = HTMLResponse(body, status_code=status)
            resp.headers.update(SECURITY_HEADERS)
            if request.query_params.get("lang") in ("ru", "en"):
                resp.set_cookie(
                    "lang",
                    lang,
                    max_age=365 * 86400,
                    httponly=True,
                    samesite="lax",
                    secure=s.public_url.startswith("https://"),
                )
            return resp

        if pending is None:
            return page(render_expired(lang), 400)
        assert req_id is not None
        req_id = str(req_id)
        if request.method == "GET":
            return page(render_login(lang, req_id))

        ip = client_ip(request, s)
        email = str(form.get("email", "")).strip().lower()
        password = str(form.get("password", ""))
        if self.limiter.blocked(ip):
            log.warning(
                "sign-in blocked for %s (too many failures)",
                ip,
                extra={"event": "auth.blocked", "ip": ip},
            )
            return page(render_login(lang, req_id, "err_blocked", email), 429)
        if not email or not password:
            return page(render_login(lang, req_id, "err_missing", email), 400)

        async def failure(key: str, status: int) -> Response:
            self.limiter.fail(ip)
            await asyncio.sleep(FAIL_DELAY)
            return page(render_login(lang, req_id, key, email), status)

        if email not in s.allowed_emails:
            log.info(
                "sign-in refused for a non-allowed address from %s",
                ip,
                extra={"event": "auth.refused", "ip": ip},
            )
            return await failure("err_invalid", 401)

        user = self.users.for_email(email)

        def check() -> Any:
            with user.lock:
                col = user.collection()
                return self.backend.login(col, email, password, s.sync_endpoint or None)

        try:
            auth = await asyncio.to_thread(check)
        except SyncError as exc:
            if exc.kind == SyncErrorKind.AUTH:
                log.info(
                    "sync login of user %s rejected (authentication error)",
                    user.id,
                    extra={"event": "auth.failed", "user": user.id, "ip": ip},
                )
                return await failure("err_invalid", 401)
            log.warning(
                "sync login raised SyncError",
                extra={"event": "auth.sync_error", "user": user.id, "ip": ip, **error_fields(exc)},
            )
            return page(render_login(lang, req_id, "err_sync", email), 502)
        except NetworkError as exc:
            log.warning(
                "sync login raised NetworkError",
                extra={
                    "event": "auth.sync_network_error",
                    "user": user.id,
                    "ip": ip,
                    **error_fields(exc),
                },
            )
            return page(render_login(lang, req_id, "err_network", email), 502)

        user.save_auth(auth.hkey, auth.endpoint or None)
        if user.state.auth_invalid:
            user.state.auth_invalid = False
            user.save_state()
        elif not user.state_path.exists():
            user.save_state()
        log.info(
            "user %s signed in", user.id, extra={"event": "auth.login", "user": user.id, "ip": ip}
        )
        assert user.id == user_id_for(email)
        redirect = self.complete_login(req_id, user.id, email)
        resp = RedirectResponse(redirect, status_code=302)
        resp.headers.update(SECURITY_HEADERS)
        return resp


SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

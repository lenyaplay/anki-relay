"""The sign-in page shown during OAuth authorization, in Russian and English.

Self-contained: no external resources, responsive, light and dark theme. Every
interpolated value goes through ``html.escape``.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode

LANGUAGES = ("ru", "en")

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Anki Relay — sign in",
        "heading": "Connect Anki to Claude",
        "lead": "Claude is asking for access to your Anki collection.",
        "email": "AnkiWeb email",
        "password": "AnkiWeb password",
        "submit": "Sign in",
        "note": (
            "Sign in with your AnkiWeb account. Your password is checked by AnkiWeb "
            "and never stored on this server."
        ),
        "err_invalid": "Invalid email or password.",
        "err_timeout": "AnkiWeb did not respond. Please try again later.",
        "err_network": "No connection to AnkiWeb. Please try again later.",
        "err_blocked": "Too many failed attempts. Please try again later.",
        "err_missing": "Enter your email and password.",
        "err_expired": (
            "This sign-in link has expired or was already used. Go back to Claude "
            "and press Connect again."
        ),
        "expired_heading": "Link expired",
    },
    "ru": {
        "title": "Anki Relay — вход",
        "heading": "Подключение Anki к Claude",
        "lead": "Claude запрашивает доступ к вашей коллекции Anki.",
        "email": "Email AnkiWeb",
        "password": "Пароль AnkiWeb",
        "submit": "Войти",
        "note": (
            "Войдите аккаунтом AnkiWeb. Пароль проверяется у AnkiWeb и на сервере не сохраняется."
        ),
        "err_invalid": "Неверный email или пароль.",
        "err_timeout": "AnkiWeb не ответил. Попробуйте позже.",
        "err_network": "Нет связи с AnkiWeb. Попробуйте позже.",
        "err_blocked": "Слишком много неудачных попыток. Попробуйте позже.",
        "err_missing": "Введите email и пароль.",
        "err_expired": (
            "Ссылка для входа устарела или уже использована. Вернитесь в Claude и "
            "нажмите Connect ещё раз."
        ),
        "expired_heading": "Ссылка устарела",
    },
}


def pick_language(
    query_lang: str | None, cookie_lang: str | None, accept_language: str | None, default: str
) -> str:
    for candidate in (query_lang, cookie_lang):
        if candidate in LANGUAGES:
            return candidate  # type: ignore[return-value]
    if default in LANGUAGES:
        return default
    return language_from_header(accept_language)


def language_from_header(header: str | None) -> str:
    """Pick ru or en from Accept-Language by quality value; English by default."""
    best, best_q = "en", -1.0
    for part in (header or "").split(","):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        primary = tag.strip().lower().split("-")[0]
        if primary in LANGUAGES and q > best_q:
            best, best_q = primary, q
    return best


CSS = """
:root{--bg:#f5f6f8;--card:#fff;--text:#1d2129;--muted:#5f6670;--border:#d5d9e0;
--accent:#2f6fde;--accent-text:#fff;--error-bg:#fdecec;--error:#a32020}
@media (prefers-color-scheme: dark){:root{--bg:#121417;--card:#1c1f24;--text:#e8eaed;
--muted:#9aa1ab;--border:#343a42;--accent:#5b8ef0;--accent-text:#0b0d10;
--error-bg:#3a1d1d;--error:#ff9c9c}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:16px}
main{width:100%;max-width:400px;background:var(--card);border:1px solid var(--border);
border-radius:12px;padding:28px 24px}
.lang{display:flex;justify-content:flex-end;gap:8px;font-size:14px;margin:-8px 0 8px}
.lang a{color:var(--muted);text-decoration:none;padding:2px 6px;border-radius:6px}
.lang a.on{color:var(--text);font-weight:600;border:1px solid var(--border)}
h1{font-size:22px;margin:0 0 6px}
p.lead{color:var(--muted);margin:0 0 20px}
label{display:block;font-size:14px;margin:14px 0 6px}
input{width:100%;padding:10px 12px;font-size:16px;border:1px solid var(--border);
border-radius:8px;background:var(--bg);color:var(--text)}
button{width:100%;margin-top:22px;padding:11px;font-size:16px;font-weight:600;border:0;
border-radius:8px;background:var(--accent);color:var(--accent-text);cursor:pointer}
.error{background:var(--error-bg);color:var(--error);border-radius:8px;padding:10px 12px;
margin:0 0 12px;font-size:14px}
.note{color:var(--muted);font-size:13px;margin:18px 0 0}
"""


def _switcher(lang: str, req_id: str | None) -> str:
    links = []
    for code in LANGUAGES:
        params = {"lang": code}
        if req_id:
            params = {"req": req_id, "lang": code}
        cls = ' class="on"' if code == lang else ""
        href = html.escape("/login?" + urlencode(params), quote=True)
        links.append(f'<a href="{href}"{cls} hreflang="{code}">{code.upper()}</a>')
    return '<nav class="lang">' + "".join(links) + "</nav>"


def _page(lang: str, title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f'<meta name="robots" content="noindex"><title>{html.escape(title)}</title>'
        f"<style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def render_login(lang: str, req_id: str, error_key: str | None = None, email: str = "") -> str:
    t = TRANSLATIONS[lang]
    e = html.escape
    error = f'<p class="error" role="alert">{e(t[error_key])}</p>' if error_key else ""
    body = (
        f"{_switcher(lang, req_id)}<h1>{e(t['heading'])}</h1>"
        f'<p class="lead">{e(t["lead"])}</p>{error}'
        '<form method="post" action="/login">'
        f'<input type="hidden" name="req" value="{e(req_id, quote=True)}">'
        f'<input type="hidden" name="lang" value="{e(lang, quote=True)}">'
        f'<label for="email">{e(t["email"])}</label>'
        f'<input id="email" name="email" type="email" autocomplete="username" required '
        f'autofocus value="{e(email, quote=True)}">'
        f'<label for="password">{e(t["password"])}</label>'
        '<input id="password" name="password" type="password" '
        'autocomplete="current-password" required>'
        f'<button type="submit">{e(t["submit"])}</button></form>'
        f'<p class="note">{e(t["note"])}</p>'
    )
    return _page(lang, t["title"], body)


def render_expired(lang: str) -> str:
    t = TRANSLATIONS[lang]
    e = html.escape
    body = (
        f"{_switcher(lang, None)}<h1>{e(t['expired_heading'])}</h1>"
        f'<p class="error" role="alert">{e(t["err_expired"])}</p>'
    )
    return _page(lang, t["title"], body)

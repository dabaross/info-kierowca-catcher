from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from urllib.parse import urlsplit

INFO_ORIGIN = "https://info-kierowca.pl"
PROFILE_PATH = "/bknd/exam/api/v1/pkk/get_profiles_for_reservation"
AUTH_HOST = "login.mobywatel.gov.pl"


def host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def accepted_handoff(url: str, source: str, schemes: set[str]) -> bool:
    """Only an app URI actually seen on the official page; never invent one."""
    if host(source) != AUTH_HOST or not source.startswith("https://"):
        return False
    if len(url) > 16000 or re.search(r"[\x00-\x20\x7f]", url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    forbidden = {"http", "https", "javascript", "data", "file", "intent", "about"}
    return bool(parsed.scheme and parsed.scheme in schemes and parsed.scheme not in forbidden)


def profile_response_valid(data: object) -> bool:
    return isinstance(data, list) and all(
        isinstance(row, dict) and "pkkNumber" in row and "categoryName" in row
        for row in data
    )


def portal_cookies(cookies: list[dict], now: float | None = None) -> CookieJar:
    """Export only portal cookies, preserving path, expiry and Secure scope."""
    now = time.time() if now is None else now
    jar = CookieJar()
    for c in cookies:
        domain = c.get("domain", "")
        expiry = c.get("expires", -1)
        if domain.lstrip(".").lower() != "info-kierowca.pl":
            continue
        if expiry > 0 and expiry <= now:
            continue
        jar.set_cookie(Cookie(
            version=0, name=c["name"], value=c["value"], port=None,
            port_specified=False, domain=domain, domain_specified=True,
            domain_initial_dot=domain.startswith("."), path=c.get("path", "/"),
            path_specified=True, secure=c.get("secure", False),
            expires=int(expiry) if expiry > 0 else None, discard=expiry <= 0,
            comment=None, comment_url=None, rest={"HttpOnly": c.get("httpOnly", False)},
            rfc2109=False,
        ))
    return jar


@dataclass
class Attempt:
    id: str
    state: str = "STARTING"
    message: str = "Uruchamiamy przeglądarkę serwerową."
    handoff_url: str | None = None
    started: float = field(default_factory=time.monotonic)
    finished: bool = False
    browser_verified: bool = False
    http_verified: bool = False
    verified_at: str | None = None
    diagnostics: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "id": self.id, "state": self.state, "message": self.message,
            "handoff_url": None if self.finished else self.handoff_url,
            "browser_verified": self.browser_verified,
            "http_verified": self.http_verified,
            "verified_at": self.verified_at,
            "diagnostics": self.diagnostics,
        }

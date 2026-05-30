"""Authentication against MUN Brightspace via CAS SSO.

Verified flow (login.mun.ca is an Apereo CAS server):
  1. GET  https://login.mun.ca/cas/login?service=<d2l cas endpoint>
         -> Apereo CAS form #fm1 with a dynamic hidden `execution` token.
  2. POST username/password/execution/_eventId=submit/geolocation back to it.
  3. CAS 302s to https://online.mun.ca/d2l/custom/cas?ticket=ST-...
  4. D2L validates the ticket and sets d2lSessionVal / d2lSecureSessionVal cookies.

The returned httpx.Client carries those cookies and is reused for all data calls.
No browser is required.
"""

from __future__ import annotations

import re
from html import unescape

import httpx

from .config import Config

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Pull a hidden input's value out of the CAS form regardless of attribute order.
_HIDDEN_RE = (
    r'<input[^>]*\bname=["\']{name}["\'][^>]*\bvalue=["\']([^"\']*)["\']'
    r'|<input[^>]*\bvalue=["\']([^"\']*)["\'][^>]*\bname=["\']{name}["\']'
)


class AuthError(RuntimeError):
    """Raised when login fails (bad credentials, unexpected flow, etc.)."""


def _extract_hidden(html: str, name: str) -> str | None:
    m = re.search(_HIDDEN_RE.format(name=re.escape(name)), html, re.IGNORECASE)
    if not m:
        return None
    return unescape(m.group(1) if m.group(1) is not None else m.group(2))


def login(config: Config, *, timeout: float = 30.0) -> httpx.Client:
    """Authenticate and return an httpx.Client with valid D2L session cookies."""
    client = httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": _UA},
    )
    try:
        # 1. Load the CAS login page and grab the one-time `execution` token.
        resp = client.get(config.cas_login_url, params={"service": config.cas_service})
        resp.raise_for_status()
        execution = _extract_hidden(resp.text, "execution")
        if not execution:
            raise AuthError(
                "Could not find the CAS `execution` token on the login page; "
                "the login flow may have changed."
            )

        # 2. Submit credentials to the same CAS URL (service kept in the query).
        resp = client.post(
            config.cas_login_url,
            params={"service": config.cas_service},
            data={
                "username": config.username,
                "password": config.password,
                "execution": execution,
                "_eventId": "submit",
                "geolocation": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()

        # 3/4. Success means a Service Ticket was issued and D2L set its session
        # cookie. Detect both: a `ticket=` redirect in the chain AND the cookie.
        issued_ticket = any(
            "ticket=" in str(r.headers.get("location", ""))
            for r in resp.history
        )
        has_session = bool(client.cookies.get("d2lSessionVal"))

        landed_on_login = "/cas/login" in str(resp.url) or "/d2l/login" in str(resp.url)
        if landed_on_login or not (issued_ticket or has_session):
            raise AuthError(
                "Login failed — CAS did not issue a ticket. Check MUN_USERNAME / "
                "MUN_PASSWORD."
            )
        return client
    except Exception:
        client.close()
        raise


if __name__ == "__main__":  # Verification: log in and print the authenticated user.
    from .client import BrightspaceClient
    from .config import load_config

    cfg = load_config()
    http = login(cfg)
    bs = BrightspaceClient(http, cfg)
    who = bs.whoami()
    print("Logged in as:", who.get("FirstName"), who.get("LastName"),
          f"(UniqueName={who.get('UniqueName')}, Identifier={who.get('Identifier')})")

"""Hosted-tier accounts: Google Web OAuth, sessions, and refresh-token encryption.

"Sign in with Google" is both identity *and* the Calendar grant in one consent flow, so
there are no passwords and MUN credentials never reach the backend. We persist only an
**encrypted** refresh token (Fernet); the user's identity is the Google subject id.

Pure httpx (no Google SDK), matching the house style in auth.py / calendar_sync.py — this
is the web-redirect sibling of `calendar_sync.GoogleCalendar.authorize`'s loopback flow.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet

from .config import Config

# Identity (openid+email) + least-privilege calendar scope (only a calendar we create).
SCOPES = "openid email https://www.googleapis.com/auth/calendar.app.created"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


class AuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- token crypto
def _fernet(cfg: Config) -> Fernet:
    if not cfg.token_encryption_key:
        raise AuthError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with:\n"
            "    python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(cfg.token_encryption_key.encode())


def encrypt_token(cfg: Config, plaintext: str) -> str:
    return _fernet(cfg).encrypt(plaintext.encode()).decode()


def decrypt_token(cfg: Config, ciphertext: str) -> str:
    return _fernet(cfg).decrypt(ciphertext.encode()).decode()


# --------------------------------------------------------------------------- OAuth flow
def redirect_uri(cfg: Config) -> str:
    return cfg.backend_base_url.rstrip("/") + "/auth/callback"


def consent_url(cfg: Config, state: str) -> str:
    """The Google consent URL to redirect the user to."""
    if not cfg.google_web_client_id:
        raise AuthError(
            "GOOGLE_WEB_CLIENT_ID is not set. Create a 'Web application' OAuth client in "
            "Google Cloud Console with redirect URI " + redirect_uri(cfg)
        )
    return AUTH_ENDPOINT + "?" + urlencode({
        "client_id": cfg.google_web_client_id,
        "redirect_uri": redirect_uri(cfg),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",         # force refresh-token issuance
        "state": state,
        "include_granted_scopes": "true",
    })


def exchange_code(cfg: Config, code: str, *, timeout: float = 30.0) -> dict:
    """Exchange an authorization code for tokens ({access_token, refresh_token, ...})."""
    resp = httpx.post(TOKEN_ENDPOINT, data={
        "code": code,
        "client_id": cfg.google_web_client_id,
        "client_secret": cfg.google_web_client_secret,
        "redirect_uri": redirect_uri(cfg),
        "grant_type": "authorization_code",
    }, timeout=timeout)
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed: {resp.status_code} {resp.text}")
    return resp.json()


def fetch_identity(access_token: str, *, timeout: float = 30.0) -> dict:
    """Return the user's OpenID identity ({sub, email, ...}) from the userinfo endpoint."""
    resp = httpx.get(USERINFO_ENDPOINT,
                     headers={"Authorization": f"Bearer {access_token}"}, timeout=timeout)
    if resp.status_code != 200:
        raise AuthError(f"userinfo failed: {resp.status_code} {resp.text}")
    return resp.json()

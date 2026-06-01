"""Configuration loaded from environment / .env.

Secrets (MUN credentials) live only in the environment or a gitignored .env file;
they are never hardcoded or written to the data store.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .credentials import resolve_credentials

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    cred_source: str        # "keychain" or "env" — provenance, not the secret
    base_url: str          # Brightspace host, e.g. https://online.mun.ca
    cas_base_url: str       # CAS host, e.g. https://login.mun.ca
    data_dir: Path          # where the SQLite store + raw content live
    # Google Calendar sync (non-secret config; the OAuth refresh token lives in the
    # keychain, never here). client_secret for a "Desktop app" OAuth client is not
    # truly confidential, so it may sit in .env alongside the id.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    default_due_time: str = "23:59"          # applied to date-only deadlines (flagged)
    calendar_timezone: str = "America/St_Johns"  # MUN is UTC-3:30 — mind the half hour
    oauth_port: int = 8765                   # loopback port for the OAuth consent flow

    @property
    def cas_service(self) -> str:
        """The D2L endpoint CAS redirects back to after authenticating."""
        return f"{self.base_url}/d2l/custom/cas"

    @property
    def cas_login_url(self) -> str:
        return f"{self.cas_base_url}/cas/login"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "brightspace.sqlite"

    @property
    def content_dir(self) -> Path:
        return self.data_dir / "content"


def load_config(*, require_credentials: bool = True) -> Config:
    """Load config from env/.env.

    `require_credentials=False` lets credential-free entry points (e.g. Google
    Calendar sync, which reads the already-built `deadlines` table) run without MUN
    creds configured.
    """
    creds = resolve_credentials()
    if creds is None and require_credentials:
        raise ConfigError(
            "No MUN credentials found. Store them securely with\n"
            "    python -m brightspace_scraper.credentials set\n"
            "or set MUN_USERNAME / MUN_PASSWORD (see .env.example)."
        )

    base_url = os.environ.get("BRIGHTSPACE_BASE_URL", "https://online.mun.ca").rstrip("/")
    cas_base_url = os.environ.get("CAS_BASE_URL", "https://login.mun.ca").rstrip("/")
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()

    return Config(
        username=creds.username if creds else "",
        password=creds.password if creds else "",
        cred_source=creds.source if creds else "none",
        base_url=base_url,
        cas_base_url=cas_base_url,
        data_dir=data_dir,
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip() or None,
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() or None,
        default_due_time=os.environ.get("DEFAULT_DUE_TIME", "23:59").strip() or "23:59",
        calendar_timezone=(
            os.environ.get("CALENDAR_TIMEZONE", "America/St_Johns").strip()
            or "America/St_Johns"
        ),
        oauth_port=int(os.environ.get("GOOGLE_OAUTH_PORT", "8765")),
    )

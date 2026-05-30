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


def load_config() -> Config:
    creds = resolve_credentials()
    if creds is None:
        raise ConfigError(
            "No MUN credentials found. Store them securely with\n"
            "    python -m brightspace_scraper.credentials set\n"
            "or set MUN_USERNAME / MUN_PASSWORD (see .env.example)."
        )

    base_url = os.environ.get("BRIGHTSPACE_BASE_URL", "https://online.mun.ca").rstrip("/")
    cas_base_url = os.environ.get("CAS_BASE_URL", "https://login.mun.ca").rstrip("/")
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()

    return Config(
        username=creds.username,
        password=creds.password,
        cred_source=creds.source,
        base_url=base_url,
        cas_base_url=cas_base_url,
        data_dir=data_dir,
    )

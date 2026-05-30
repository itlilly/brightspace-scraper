"""Credential resolution: OS keychain first, environment/.env as fallback.

Priority when resolving the password:
  1. OS keychain (via `keyring`)  -- encrypted at rest, the secure default.
  2. MUN_PASSWORD env / .env       -- convenient dev fallback.

The username is not secret; it may come from MUN_USERNAME, or the "default username"
we stash in the keychain alongside the password so a scheduled run knows which account
to use without any env config.

CLI:
    python -m brightspace_scraper.credentials set      # prompt + store in keychain
    python -m brightspace_scraper.credentials status   # show what's configured
    python -m brightspace_scraper.credentials delete    # remove from keychain
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:  # keyring may have no usable backend (e.g. a bare headless box).
    import keyring
    from keyring.errors import KeyringError
except Exception:  # pragma: no cover - import-time backend issues
    keyring = None  # type: ignore[assignment]

    class KeyringError(Exception):  # type: ignore[no-redef]
        pass


SERVICE = "brightspace-scraper"
_USERNAME_KEY = "__default_username__"  # where we remember the account name


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    source: str  # "keychain" or "env" — for transparency, never the secret itself


def _keychain_available() -> bool:
    return keyring is not None


def store_credentials(username: str, password: str) -> None:
    """Persist credentials in the OS keychain (encrypted at rest)."""
    if not _keychain_available():
        raise RuntimeError(
            "No keyring backend is available on this system. Install/enable an OS "
            "secret store (GNOME Keyring or KWallet on Linux), or use a .env file."
        )
    keyring.set_password(SERVICE, username, password)
    keyring.set_password(SERVICE, _USERNAME_KEY, username)


def delete_credentials(username: str | None = None) -> None:
    if not _keychain_available():
        return
    username = username or _keychain_username()
    if username:
        try:
            keyring.delete_password(SERVICE, username)
        except KeyringError:
            pass
    try:
        keyring.delete_password(SERVICE, _USERNAME_KEY)
    except KeyringError:
        pass


def _keychain_username() -> str | None:
    if not _keychain_available():
        return None
    try:
        return keyring.get_password(SERVICE, _USERNAME_KEY)
    except KeyringError:
        return None


def _keychain_password(username: str) -> str | None:
    if not _keychain_available():
        return None
    try:
        return keyring.get_password(SERVICE, username)
    except KeyringError:
        return None


def resolve_credentials() -> Credentials | None:
    """Resolve (username, password). Keychain first, env fallback. None if unset."""
    env_username = os.environ.get("MUN_USERNAME", "").strip() or None
    env_password = os.environ.get("MUN_PASSWORD") or None

    # Account name: explicit env wins for selection; otherwise the keychain default.
    username = env_username or _keychain_username()
    if not username:
        return None

    # Password: keychain first, then env.
    kc_password = _keychain_password(username)
    if kc_password:
        return Credentials(username=username, password=kc_password, source="keychain")
    if env_password:
        return Credentials(username=username, password=env_password, source="env")
    return None


# --------------------------------------------------------------------------- CLI
def _main(argv: list[str]) -> int:
    import getpass
    import sys

    cmd = argv[0] if argv else "status"
    flags = argv[1:]

    if cmd == "set":
        if not _keychain_available():
            print("No keyring backend available. Use a .env file instead.")
            return 1

        # Non-interactive store: read from MUN_USERNAME / MUN_PASSWORD env vars.
        if "--from-env" in flags:
            username = os.environ.get("MUN_USERNAME", "").strip()
            password = os.environ.get("MUN_PASSWORD", "")
            if not username or not password:
                print("--from-env needs MUN_USERNAME and MUN_PASSWORD in the environment.")
                return 1
            store_credentials(username, password)
            print(f"Stored credentials for '{username}' in the OS keychain.")
            return 0

        # Interactive store needs a real terminal (a TTY) for hidden input.
        if not sys.stdin.isatty():
            print(
                "Interactive prompts need a real terminal (a TTY), which this context "
                "doesn't provide.\nEither run this in a normal terminal window, or store "
                "non-interactively:\n"
                "    read -rsp 'MUN password: ' MUN_PASSWORD && echo\n"
                "    MUN_USERNAME=<your-id> MUN_PASSWORD=\"$MUN_PASSWORD\" \\\n"
                "        .venv/bin/python -m brightspace_scraper.credentials set --from-env\n"
                "    unset MUN_PASSWORD"
            )
            return 1

        default_user = _keychain_username() or os.environ.get("MUN_USERNAME", "")
        prompt = f"MUN username{f' [{default_user}]' if default_user else ''}: "
        username = input(prompt).strip() or default_user
        if not username:
            print("Username is required.")
            return 1
        password = getpass.getpass("MUN password (input hidden): ")
        if not password:
            print("Password is required.")
            return 1
        store_credentials(username, password)
        print(f"Stored credentials for '{username}' in the OS keychain.")
        return 0

    if cmd == "delete":
        delete_credentials()
        print("Removed credentials from the OS keychain.")
        return 0

    # status
    print(f"keyring backend available: {_keychain_available()}")
    creds = resolve_credentials()
    if creds:
        masked = creds.password[0] + "*" * (len(creds.password) - 1) if creds.password else ""
        print(f"resolved username: {creds.username}")
        print(f"password source:   {creds.source}")
        print(f"password:          {masked}")
    else:
        print("No credentials resolved. Run `set`, or define MUN_USERNAME/MUN_PASSWORD.")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

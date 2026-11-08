"""API keys: read from the environment (or a local .env file), never from source.

The .env file is only a convenience. Real environment variables win.
Keys are never logged; the Credentials repr is masked.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import constants as C
from .safety import SafetyError

KEY_ID_VAR = "APCA_API_KEY_ID"
SECRET_VAR = "APCA_API_SECRET_KEY"
BASE_URL_VAR = "APCA_API_BASE_URL"  # optional; if set it must be the paper host


class CredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    key_id: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __repr__(self) -> str:  # never print the values
        return f"Credentials(key_id=***{self.key_id[-4:]}, secret_key=***)"

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret_key}

    def secrets(self) -> tuple[str, str]:
        return (self.key_id, self.secret_key)


def load_env_file(path: str | Path, environ: dict | None = None) -> list[str]:
    """Load KEY=VALUE lines into environ without overriding existing values.

    Returns warnings (for example, a file readable by other users).
    """
    environ = os.environ if environ is None else environ
    path = Path(path)
    warnings: list[str] = []
    if not path.is_file():
        return warnings
    if os.name == "posix":
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            warnings.append(f"{path} is readable by other users; run: chmod 600 {path}")
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise CredentialsError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        environ.setdefault(key, value)
    return warnings


def check_base_url_env(environ: dict | None = None) -> None:
    """APCA_API_BASE_URL is optional. If present, it must point at paper.

    This catches a shell or .env that was set up for a live account.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(BASE_URL_VAR, "").strip()
    if not raw:
        return
    host = (urlsplit(raw).hostname or "").lower()
    if host != C.PAPER_TRADING_HOST:
        raise SafetyError(
            f"{BASE_URL_VAR}={raw!r} is not the Alpaca paper host. "
            f"ORB-IEX trades on {C.PAPER_TRADING_BASE_URL} only. Remove or fix the variable."
        )


def load_credentials(environ: dict | None = None) -> Credentials:
    environ = os.environ if environ is None else environ
    check_base_url_env(environ)
    key_id = environ.get(KEY_ID_VAR, "").strip()
    secret = environ.get(SECRET_VAR, "").strip()
    missing = [n for n, v in ((KEY_ID_VAR, key_id), (SECRET_VAR, secret)) if not v]
    if missing:
        raise CredentialsError(
            f"missing {', '.join(missing)}. Set them in the environment or in .env "
            f"(copy .env.example). Use PAPER account keys."
        )
    return Credentials(key_id=key_id, secret_key=secret)

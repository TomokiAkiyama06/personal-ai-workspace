"""Codex's ChatGPT login in a session (Decision 0083, 6; Decision 0087, 4's
table; Decision 0089; issue #239).

:class:`CodexAuthFile` is the :class:`~.codex_cli.CodexCredentials` of the
ChatGPT login. The connection's credential is the login's ``auth.json`` (put
into the secret store with ``paw-connection-credential-put codex``):

* :meth:`~CodexAuthFile.place` writes it as ``$CODEX_HOME/auth.json`` of the
  session (the session's directory on ``/run``, a tmpfs) with mode 0660: the
  backend owns it and the CLI's account writes it through the group of the
  session's directory (``paw-cli-bridge``). Codex (``rust-v0.162.1``,
  ``login/src/auth/storage.rs``) saves a refreshed login by truncating and
  rewriting that file in place, so the file keeps its owner and mode and the
  backend can read it back. In the container, ``CODEX_HOME`` is a tmpfs of
  the container (0700, the CLI's account) with only ``config.toml``
  (read-only) and this file mounted into it (``paw_cli_session_run.py``):
  what else Codex writes there goes with the container. The session's
  ``config.toml`` sets ``cli_auth_credentials_store = "file"`` and
  ``forced_login_method = "chatgpt"``.
* :meth:`~CodexAuthFile.collect` reads it back once the CLI was stopped
  (also after a failure or a cancellation: the adapter runs it to its end).
  When it changed (another JSON value than the one placed: Codex refreshed
  its tokens), it is handed to the service's ``write_back``, which stores it
  as the credential's next version only while the version the session was
  given is still the current one (compare-and-swap, under the credential's
  lock, while the session still holds the connection's session slot).

Nothing of the file is logged: only whether it changed and what the
write-back came to.
"""

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Final

from paw_backend.connections import Secret, WriteBack
from paw_backend.connections.limits import MAX_SECRET_CHARS
from paw_backend.connections.session import CredentialWriter

logger = logging.getLogger(__name__)

AUTH_NAME: Final = "auth.json"
#: The backend and the CLI's account (the group of the session's directory).
AUTH_MODE: Final = 0o660
#: The most of the file that is read back (a value over the store's limit is
#: refused by the store anyway).
MAX_AUTH_BYTES: Final = MAX_SECRET_CHARS * 4


def _json_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class CodexAuthFile:
    """See the module."""

    def __repr__(self) -> str:
        return "CodexAuthFile()"

    async def place(self, secret: Secret, codex_home: Path) -> None:
        """Write the login as ``auth.json`` (a new file, 0660). A credential
        that is not a JSON object is not a ChatGPT login: ``ValueError``."""
        value = secret.reveal()
        if _json_object(value) is None:
            raise ValueError("the credential is not a Codex auth.json")
        await asyncio.to_thread(_write_new, Path(codex_home) / AUTH_NAME, value)

    async def collect(
        self,
        secret: Secret,
        codex_home: Path,
        write_back: CredentialWriter | None,
    ) -> WriteBack | None:
        """Read the login back and write it back when it changed. Returns
        what the write-back came to (``None``: nothing to write back)."""
        try:
            data = await asyncio.to_thread(_read, Path(codex_home) / AUTH_NAME)
        except FileNotFoundError:
            logger.warning("Codex login file is gone after the session")
            return None
        except (OSError, ValueError) as error:
            logger.error(
                "Codex login file could not be read back: exception_type=%s",
                type(error).__name__,
            )
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            logger.error("Codex login file is not text after the session")
            return None
        placed = _json_object(secret.reveal())
        current = _json_object(text)
        if current is None:
            logger.error("Codex login file is not a login after the session")
            return None
        if current == placed:
            return None
        if write_back is None:
            logger.error(
                "Codex refreshed its login but it cannot be written back"
                " (no secret store): the stored login may no longer work"
            )
            return None
        outcome = await write_back(text)
        if outcome is WriteBack.WRITTEN:
            logger.info("Codex refreshed its login; written back")
        else:
            logger.error(
                "Codex refreshed its login but it was not written back: %s",
                outcome.value,
            )
        return outcome


def _write_new(path: Path, value: str) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        AUTH_MODE,
    )
    try:
        os.fchmod(fd, AUTH_MODE)  # whatever the umask
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(value)
    finally:
        os.close(fd)


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUTH_BYTES:
            raise ValueError("not a login file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_AUTH_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_AUTH_BYTES:
        raise ValueError("not a login file")
    return data


__all__ = ["AUTH_MODE", "AUTH_NAME", "CodexAuthFile"]

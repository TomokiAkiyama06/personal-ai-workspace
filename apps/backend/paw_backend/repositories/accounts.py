"""Which Linux account a workspace user acts as (PAW-027).

A checkout is a directory of one Linux user (``REQUIREMENTS.md``: Linux user
separation, ``/home/<user>/workspaces/...``). The mapping "workspace user" to
"Linux account" is **not defined by the requirements**; Decision 0017 (Approved)
chose the simplest one, :class:`LoginNameAccountDirectory`: the user's
``login_name`` is the Linux user name. The seam (:class:`AccountDirectory`) lets a
deployment with another rule (a mapping table, LDAP) replace it.

A system account is never a checkout owner: a uid below the configured minimum
(``root`` is 0), ``nobody``, and accounts whose shell is ``nologin`` / ``false``
are refused, so a login name such as ``root`` or ``www-data`` (which the login
name rules allow) cannot make the backend write into a system directory. The
minimum uid is the one of the ``RepositoryPolicy`` (see
:meth:`RepositoryService.from_policy`, which builds the directory and the service
from one policy).
"""

import asyncio
import os
import pwd
import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import text

from paw_backend.db import Database
from paw_backend.repositories.errors import LinuxAccountUnavailableError
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import RepositoryPolicy

_NOBODY_UID = 65534
_NO_LOGIN_SHELLS = ("nologin", "false")

_LOGIN_NAME = text("SELECT login_name FROM users WHERE id = :id AND status = 'active'")


class AccountDirectory(Protocol):
    """Finds the Linux account of a workspace user.

    ``account_of`` raises :class:`LinuxAccountUnavailableError` when there is
    none (an unknown, inactive or unmapped user, a system account, a home that
    is not absolute). It never returns an account for another user.
    """

    async def account_of(self, user_id: uuid.UUID) -> LinuxAccount: ...


class LoginNameAccountDirectory:
    """``users.login_name`` is the Linux user name; the account is read with ``pwd``.

    The lowest uid that counts as a person is ``policy.min_uid`` (the
    ``PAW_REPOSITORY_MIN_LINUX_UID`` setting): **there is no second value** here, so
    a raised setting really refuses the accounts below it. ``lookup`` (default
    ``pwd.getpwnam``) is the account database; a test replaces it. The lookup runs in
    a thread (it can block on NSS / LDAP).
    """

    def __init__(
        self,
        database: Database,
        *,
        policy: RepositoryPolicy,
        lookup: Callable[[str], pwd.struct_passwd] = pwd.getpwnam,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(policy, RepositoryPolicy):
            raise TypeError("policy must be a RepositoryPolicy")
        self._database = database
        self._min_uid = policy.min_uid
        self._lookup = lookup

    @property
    def min_uid(self) -> int:
        """The lowest uid this directory hands out (the policy's)."""
        return self._min_uid

    async def account_of(self, user_id: uuid.UUID) -> LinuxAccount:
        async with self._database.session() as session:
            login = (await session.execute(_LOGIN_NAME, {"id": user_id})).scalar()
        if not isinstance(login, str):
            raise LinuxAccountUnavailableError()
        try:
            entry = await asyncio.to_thread(self._lookup, login)
        except (KeyError, OSError):
            raise LinuxAccountUnavailableError() from None
        shell = os.path.basename(str(entry.pw_shell))
        if (
            entry.pw_name != login
            or entry.pw_uid < self._min_uid
            or entry.pw_uid >= _NOBODY_UID
            or shell in _NO_LOGIN_SHELLS
        ):
            raise LinuxAccountUnavailableError()
        try:
            return LinuxAccount(user_id, login, entry.pw_uid, entry.pw_dir)
        except ValueError:
            raise LinuxAccountUnavailableError() from None

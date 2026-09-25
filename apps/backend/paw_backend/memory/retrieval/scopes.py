"""What a caller may read, once the backend has decided (an internal record)."""

from dataclasses import dataclass
from uuid import UUID

from paw_backend.memory.acl import Principal as AclPrincipal
from paw_backend.memory.models import MemoryScope


@dataclass(frozen=True, slots=True)
class ResolvedScopes:
    """The result of the permission step, and the only input of the SQL prefilter.

    Built by ``resolver.py`` from the Authorizer's decisions and the database
    (never from the caller's query alone). ``scopes`` are the scopes that may
    contribute at all; a scope that was denied, not asked for, or has no id to
    read is not in it. ``project_ids`` / ``repo_ids`` / ``project_group_ids`` are
    the ids whose memories may be read: an id the caller narrowed away, or is not
    allowed to read, is not in them. Every query applies BOTH: the ACL condition
    of ``memory.acl`` over these ids, and ``scope IN scopes``. The second can only
    narrow the first.
    """

    user_id: UUID
    scopes: frozenset[MemoryScope]
    project_ids: frozenset[UUID] = frozenset()
    repo_ids: frozenset[UUID] = frozenset()
    project_group_ids: frozenset[UUID] = frozenset()

    @property
    def is_empty(self) -> bool:
        return not self.scopes

    def acl_principal(self) -> AclPrincipal:
        return AclPrincipal(
            self.user_id,
            project_ids=self.project_ids,
            repo_ids=self.repo_ids,
            project_group_ids=self.project_group_ids,
        )

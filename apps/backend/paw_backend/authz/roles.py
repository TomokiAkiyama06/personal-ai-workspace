"""System roles and project roles.

The two are independent: a system role says what a person may do across the
whole workspace, a project role says what a member may do inside one project
(``docs/SECURITY_RBAC_AUDIT.md``, ``REQUIREMENTS.md`` "RBAC / Admin" and
"Project roles and membership").
"""

from enum import StrEnum


class SystemRole(StrEnum):
    """Workspace-wide role. The Owner includes every Admin capability."""

    OWNER = "owner"
    ADMIN = "admin"
    USER = "user"
    # Backend-internal identity; a human can never log in as it. It holds no
    # capability until an issue that needs one grants it explicitly.
    SYSTEM = "system"


class ProjectRole(StrEnum):
    """Role of a member inside one project."""

    MANAGER = "manager"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"

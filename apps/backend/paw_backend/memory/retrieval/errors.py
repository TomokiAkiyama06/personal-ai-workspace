"""Typed errors of Hybrid Retrieval.

Messages are fixed strings built from closed vocabularies (field names,
:class:`~paw_backend.memory.shared.errors.InputProblem` values, component names,
authorization reasons). They never contain the query, memory text, ids, driver
messages or SQL, so they are safe to log and to map to an API response. ``code``
is the stable machine-readable identifier. Database errors that are not handled
here (connection loss and so on) propagate unchanged; their text can contain SQL
parameters, so a caller must never show ``str(error)`` of those to a user.
"""

from enum import StrEnum
from typing import ClassVar

from paw_backend.memory.shared.errors import InputProblem


class Component(StrEnum):
    """A foreign component whose failure stops (or degrades) a retrieval."""

    EMBEDDER = "embedder"
    RERANKER = "reranker"
    POLICY_SOURCE = "policy_source"
    REPO_ACL_SOURCE = "repo_acl_source"
    PROJECT_GROUP_SOURCE = "project_group_source"


class RetrievalError(Exception):
    """Base class of every error raised by Hybrid Retrieval."""

    code: ClassVar[str] = "retrieval_error"


class InvalidRetrievalInputError(RetrievalError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_retrieval_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class RetrievalPermissionError(RetrievalError):
    """The retrieval cannot be authorized right now.

    Only raised when the decision could not be made or recorded
    (``reason="audit_unavailable"``: the audit write of ``memory.use`` failed and
    the Authorizer fails closed, or ``invalid_decision``). A policy denial is not
    an error: the scope it covers simply contributes nothing (an answer that says
    which project or repository exists would be a leak).
    """

    code = "retrieval_forbidden"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Not allowed: {reason}")


class RetrievalSourceError(RetrievalError):
    """A component the retrieval needs failed, and no safe answer exists without it.

    The System Policy source (a shared memory is only returned when the policy
    could be applied) and the repository / project-group sources (what a caller
    may read cannot be guessed) fail the call. An Embedder or Reranker that fails
    does not: see ``RetrievalResult.degraded``.
    """

    code = "retrieval_source_unavailable"

    def __init__(self, component: Component) -> None:
        self.component = component
        super().__init__(f"Unavailable: {component.value}")


class RetrievalTimeoutError(RetrievalError):
    """The whole call did not finish within ``timeout_seconds``."""

    code = "retrieval_timeout"

    def __init__(self) -> None:
        super().__init__("The retrieval did not finish in time")


class RetrievalScopeLimitError(RetrievalError):
    """The caller belongs to more projects than one call may search.

    Narrow the query with ``project_ids``.
    """

    code = "retrieval_scope_limit"

    def __init__(self) -> None:
        super().__init__("Too many projects: narrow the query with project_ids")


class RetrievalDataError(RetrievalError):
    """A stored row is not something this code can interpret (fail closed)."""

    code = "retrieval_data_error"

    def __init__(self) -> None:
        super().__init__("A stored memory could not be interpreted")

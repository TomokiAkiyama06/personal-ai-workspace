"""The components a retrieval calls but does not own (structural interfaces).

* :class:`Embedder`: turns the query into a vector. Which model it is, and its
  dimension, is decided by the PAW-019 benchmark, so it is a Protocol; a vector
  leg only ever compares vectors of ONE registered ``embedding_models`` row
  (``model_id`` and ``dimensions``).
* :class:`Reranker`: scores the fused candidates against the query. A model
  (a cross-encoder, a small LLM) is chosen by the benchmark too.
* :class:`RepoAclSource` and :class:`ProjectGroupSource`: where the repository
  ACLs and the project groups of a user come from. Neither exists as a store yet
  (PAW-027; the requirements do not define a project group), so a retrieval built
  without one reads no Repo / Project Group Memory.

Every value these return is checked by the caller of the retrieval before it is
used (``service.py``): a foreign component is never trusted for its shape, and an
Embedder or Reranker that fails or answers nonsense degrades the result instead
of failing the call. **A Reranker and an Embedder are only ever given text of the
query and of memories the caller may read.**
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from paw_backend.authz import RepoAcl


class Embedder(Protocol):
    """Embeds texts. ``model_id`` names a row of ``embedding_models``."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """One vector of ``dimensions`` finite numbers per text, in order."""
        ...


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """One candidate shown to a Reranker: its position and its text, no ids."""

    index: int
    title: str
    content: str


class Reranker(Protocol):
    """Scores candidates against a query."""

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> Sequence[float]:
        """One score per candidate, in order: finite, 0..1, higher is more relevant."""
        ...


class RepoAclSource(Protocol):
    """The repositories of the given projects that ``user_id`` is known to hold.

    Returns a :class:`~paw_backend.authz.RepoAcl` for each repository, bound to
    the project it belongs to (``inherit`` or an override). It is only consulted
    for projects the user is an accepted member of; the retrieval still decides
    each repository through the Authorizer (an override that removes ``read``
    denies it), so a source cannot widen access, only describe the repositories.
    """

    async def repo_acls(
        self, user_id: UUID, project_ids: Collection[UUID]
    ) -> Sequence[RepoAcl]: ...


class ProjectGroupSource(Protocol):
    """The project groups whose memories ``user_id`` may read.

    What a group is, and who belongs to it, is not defined by the requirements
    (``memory/acl.py``), so this is the caller's decision, trusted as given: there
    is no capability behind it.
    """

    async def project_group_ids(self, user_id: UUID) -> Collection[UUID]: ...

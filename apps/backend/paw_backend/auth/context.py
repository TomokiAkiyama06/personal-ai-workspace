"""What the services need to know about the request they serve."""

import uuid
from dataclasses import dataclass

from paw_backend.authz.subjects import is_valid_client_request_id


@dataclass(frozen=True, slots=True)
class RequestContext:
    """The audit correlation of one request and the bucket its client belongs to.

    ``source`` is a source bucket (``tokens.source_bucket``), not an address as
    received. ``client_request_id`` is the client's ``X-Request-ID`` if it is
    well-formed (it is client-controlled and only a hint).
    """

    correlation_id: uuid.UUID
    source: str
    client_request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_id, uuid.UUID):
            raise TypeError("correlation_id must be a UUID")
        if not isinstance(self.source, str) or not self.source:
            raise TypeError("source must be a non-empty str")
        if self.client_request_id is not None and not is_valid_client_request_id(
            self.client_request_id
        ):
            object.__setattr__(self, "client_request_id", None)

    @classmethod
    def new(cls, source: str = "unknown") -> "RequestContext":
        return cls(uuid.uuid4(), source)

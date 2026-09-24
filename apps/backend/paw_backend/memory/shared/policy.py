"""Where the System Security Policy comes from, and how it is loaded safely.

The policy is defined, stored and enforced elsewhere (Admin configuration, the
authorization layer, the tool broker); none of that exists as a store yet. This
module only defines the seam Shared Memory needs: a :class:`SystemPolicySource`
answering ``items()``, a fixed :class:`StaticPolicySource` for tests and for a
deployment whose policy is a constant, and :func:`load_policies`, which turns
whatever a source returns into a validated tuple or fails closed.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from paw_backend.memory.shared import limits
from paw_backend.memory.shared.errors import PolicySourceError
from paw_backend.memory.shared.records import SystemPolicyItem

logger = logging.getLogger(__name__)


class SystemPolicySource(Protocol):
    """Gives the current System Security Policy items.

    ``items`` returns a ``list`` or ``tuple`` of :class:`SystemPolicyItem`
    (never a generator, ``str`` or mapping) with unique ``policy_id`` values and
    at most ``MAX_POLICY_ITEMS`` entries. It is called on every effective view,
    so a change of the policy takes effect at once.
    """

    async def items(self) -> Sequence[SystemPolicyItem]: ...


class StaticPolicySource:
    """A source with a fixed list of items (validated when it is built)."""

    def __init__(self, items: Sequence[SystemPolicyItem] = ()) -> None:
        self._items = _validated_items(items)

    async def items(self) -> tuple[SystemPolicyItem, ...]:
        return self._items


def _validated_items(items: object) -> tuple[SystemPolicyItem, ...]:
    """``items`` as a tuple, or ``PolicySourceError`` (fail closed)."""
    if not isinstance(items, list | tuple) or len(items) > limits.MAX_POLICY_ITEMS:
        raise PolicySourceError
    if not all(isinstance(item, SystemPolicyItem) for item in items):
        raise PolicySourceError
    if len({item.policy_id for item in items}) != len(items):
        raise PolicySourceError
    return tuple(items)


async def load_policies(
    source: SystemPolicySource, *, timeout_seconds: float
) -> tuple[SystemPolicyItem, ...]:
    """The policy items of ``source``, or :class:`PolicySourceError`.

    Fails closed for every problem of the source: it raises (any ``Exception``),
    it does not answer within ``timeout_seconds``, it returns something that is
    not a ``list`` / ``tuple`` of ``SystemPolicyItem``, it returns more than
    ``MAX_POLICY_ITEMS`` items or two items with the same ``policy_id``. The
    original exception is logged by type name only and is not chained (its text
    can carry a connection string or policy text).
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            items = await source.items()
    except Exception as error:  # a foreign source: any failure must fail closed
        logger.warning("System policy source failed (%s)", type(error).__name__)
        raise PolicySourceError from None
    return _validated_items(items)

"""KV-cache based admission and dynamic concurrency (PAW-036): pure functions.

Local agents share one runtime per model (the requirements: Agent数とGPU上の
Model instance数は分離する). A runtime allocates its KV cache pool when it loads;
what limits how many requests it can run at once is how many tokens of context
they need, not a fixed number of agents. So each admitted request reserves its
context (prompt and answer) in tokens out of the model's pool, and the number of
requests that fit follows the context length: few long ones or many short ones
(並列Agent数は固定値にしない).

* Only ``safety`` of the pool may be reserved at all (fragmentation, the
  runtime's own blocks).
* Each class may fill only its ``ceiling`` of that: Background work leaves room
  for Interactive and Coding work.
* When the runtime reports how full its KV cache really is (``observed``), the
  larger of the reservations and the observation counts.
* A model without a pool (an embedding model) is limited by its number of
  sequences only.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from paw_backend.compute.domain import Refusal, ResourceClass

_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class KvState:
    capacity_tokens: int  # 0: the model has no KV pool
    reserved_tokens: int
    sequences: int
    max_sequences: int
    observed_fraction: float | None  # what the runtime reports, 0..1


def usable_tokens(
    state: KvState,
    resource_class: ResourceClass,
    *,
    safety: float,
    ceilings: Mapping[ResourceClass, float],
) -> int:
    return math.floor(
        state.capacity_tokens * safety * ceilings[resource_class] + _EPSILON
    )


def used_tokens(state: KvState) -> int:
    observed = 0
    if state.observed_fraction is not None:
        observed = math.ceil(state.capacity_tokens * state.observed_fraction - _EPSILON)
    return max(state.reserved_tokens, observed)


def kv_refusal(
    state: KvState,
    tokens: int,
    resource_class: ResourceClass,
    *,
    safety: float,
    ceilings: Mapping[ResourceClass, float],
) -> Refusal | None:
    """``None`` when a request of ``tokens`` fits now, else why not."""
    if state.sequences >= state.max_sequences:
        return Refusal.SEQUENCES_FULL
    if state.capacity_tokens == 0:
        return None
    usable = usable_tokens(state, resource_class, safety=safety, ceilings=ceilings)
    if used_tokens(state) + tokens > usable:
        return Refusal.KV_FULL
    return None


def parallelism(
    state: KvState,
    tokens: int,
    resource_class: ResourceClass,
    *,
    safety: float,
    ceilings: Mapping[ResourceClass, float],
) -> int:
    """How many more requests of ``tokens`` each would be admitted now."""
    sequences = max(0, state.max_sequences - state.sequences)
    if state.capacity_tokens == 0:
        return sequences
    free = usable_tokens(
        state, resource_class, safety=safety, ceilings=ceilings
    ) - used_tokens(state)
    if tokens <= 0:
        return sequences if free >= 0 else 0
    return min(sequences, max(0, free) // tokens)

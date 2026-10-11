"""How full a runtime's KV cache is, from its Prometheus ``/metrics`` (#200).

Decision 0076, 3 (Approved) wires vLLM's ``vllm:kv_cache_usage_perc`` (a
fraction, 0..1, despite its name) into ``ModelControl.kv_usage``, so that the
scheduler's "the larger of the reservations and the observation" (Decision
0037, 2) really counts what the runtime holds. :class:`CommandModelControl`
reads it here when a deployment is given a ``metrics_url``.

The reading is read-only and best effort: any failure (no answer, a status
other than 200, a body over :data:`MAX_METRICS_BYTES`, a missing or malformed
sample) is ``None`` ("not known"), which the scheduler already treats as no
observation. Nothing the server answers is logged or put in an error. The
request does not use the environment's proxy and follows no redirect.
"""

import math
from urllib.parse import urlsplit

import httpx

from paw_backend.compute.errors import InvalidComputeArgumentError

# vLLM's gauge of the KV cache in use (vLLM 0.30: one sample per engine).
KV_USAGE_METRIC = "vllm:kv_cache_usage_perc"
# A vLLM ``/metrics`` page is a few hundred KiB (its histograms); more is not
# a page of the runtime.
MAX_METRICS_BYTES = 4 * 1024 * 1024
# The reading is taken at every refresh (every 5 s): a slow server must not hold
# the refresh up for long.
DEFAULT_METRICS_TIMEOUT_SECONDS = 2.0


def check_metrics_url(value: object) -> str | None:
    """``None``, or an ``http(s)`` URL without credentials, a query or a
    fragment (``InvalidComputeArgumentError("metrics_url")`` otherwise)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise InvalidComputeArgumentError("metrics_url")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises for a bad port
    except ValueError:
        raise InvalidComputeArgumentError("metrics_url") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise InvalidComputeArgumentError("metrics_url")
    return value


def parse_kv_usage(text: str) -> float | None:
    """The largest ``vllm:kv_cache_usage_perc`` sample of a Prometheus text page
    (several engines: the fullest counts), ``None`` without one or when any is
    not a number in 0..1."""
    largest = None
    for line in text.splitlines():
        if not line.startswith(KV_USAGE_METRIC):
            continue
        rest = line[len(KV_USAGE_METRIC) :]
        if rest.startswith("{"):
            end = rest.find("}")
            if end < 0:
                return None
            rest = rest[end + 1 :]
        elif not rest.startswith((" ", "\t")):
            continue  # another metric with this prefix
        fields = rest.split()
        if not fields or len(fields) > 2:  # the value and an optional timestamp
            return None
        try:
            value = float(fields[0])
        except ValueError:
            return None
        if not math.isfinite(value) or not 0 <= value <= 1:
            return None
        largest = value if largest is None else max(largest, value)
    return largest


async def read_kv_usage(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_METRICS_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> float | None:
    """The KV cache usage the runtime at ``url`` reports, ``None`` when it
    cannot be read (see the module)."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_METRICS_BYTES:
                        return None
        return parse_kv_usage(body.decode("utf-8", errors="replace"))
    except (httpx.HTTPError, OSError, ValueError):
        return None


__all__ = [
    "DEFAULT_METRICS_TIMEOUT_SECONDS",
    "KV_USAGE_METRIC",
    "MAX_METRICS_BYTES",
    "check_metrics_url",
    "parse_kv_usage",
    "read_kv_usage",
]

"""The runtime's KV cache usage from its ``/metrics`` (Decision 0076, 3; #200).

``CommandModelControl.kv_usage`` reads ``vllm:kv_cache_usage_perc`` from the
deployment's ``metrics_url``; anything it cannot read is ``None`` (no
observation), and nothing the server answers is logged. The server is a fake
(``httpx.MockTransport``): no network, no GPU.
"""

import unittest

import httpx

from paw_backend.compute import (
    CommandModelControl,
    ComputeRequest,
    DeploymentCommands,
    InvalidComputeArgumentError,
    Refusal,
    ResourceClass,
)
from paw_backend.compute.metrics import (
    MAX_METRICS_BYTES,
    parse_kv_usage,
    read_kv_usage,
)
from tests.compute_support import build

URL = "http://127.0.0.1:8000/metrics"

# Part of a vLLM 0.30 page (two engines).
PAGE = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="main"} 3.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="main"} 0.4125
vllm:kv_cache_usage_perc{engine="1",model_name="main"} 0.73
vllm:kv_cache_usage_perc_other 0.99
# TYPE vllm:num_preemptions_total counter
vllm:num_preemptions_total{engine="0",model_name="main"} 12.0
"""


class Server:
    def __init__(self, body=PAGE, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handle(request):
            self.requests.append(request)
            body = self.body.encode() if isinstance(self.body, str) else self.body
            return httpx.Response(self.status, content=body, headers=self.headers)

        return httpx.MockTransport(handle)


class ParseTest(unittest.TestCase):
    def test_the_fullest_engine_counts(self):
        self.assertEqual(parse_kv_usage(PAGE), 0.73)

    def test_a_single_sample_with_or_without_labels_and_a_timestamp(self):
        self.assertEqual(parse_kv_usage("vllm:kv_cache_usage_perc 0.5\n"), 0.5)
        self.assertEqual(
            parse_kv_usage('vllm:kv_cache_usage_perc{a="b"} 0 1700000000000\n'), 0.0
        )
        self.assertEqual(parse_kv_usage("vllm:kv_cache_usage_perc 1\n"), 1.0)

    def test_no_sample_or_a_bad_one_is_unknown(self):
        for text in (
            "",
            "# TYPE vllm:kv_cache_usage_perc gauge\n",
            "vllm:kv_cache_usage_perc_other 0.5\n",
            "vllm:kv_cache_usage_perc 1.2\n",
            "vllm:kv_cache_usage_perc -0.1\n",
            "vllm:kv_cache_usage_perc NaN\n",
            "vllm:kv_cache_usage_perc +Inf\n",
            "vllm:kv_cache_usage_perc x\n",
            'vllm:kv_cache_usage_perc{a="b" 0.5\n',
            "vllm:kv_cache_usage_perc\n",
            "vllm:kv_cache_usage_perc 0.5 1 2\n",
            "vllm:kv_cache_usage_perc 0.2\nvllm:kv_cache_usage_perc 9\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_kv_usage(text))


class ReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_reads_the_page(self):
        server = Server()
        self.assertEqual(await read_kv_usage(URL, transport=server.transport()), 0.73)
        self.assertEqual([str(r.url) for r in server.requests], [URL])
        self.assertEqual(server.requests[0].method, "GET")

    async def test_what_cannot_be_read_is_unknown_and_never_logged(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        cases = {
            "an error status": Server(status=500, body="secret-ish").transport(),
            "a redirect": Server(
                status=302, headers={"location": "http://elsewhere/"}
            ).transport(),
            "no connection": httpx.MockTransport(refuse),
            "a page too large": Server(body=b"x" * (MAX_METRICS_BYTES + 1)).transport(),
            "no sample": Server(body="secret-ish 1\n").transport(),
        }
        for name, transport in cases.items():
            with self.subTest(name), self.assertNoLogs("paw_backend", level="DEBUG"):
                self.assertIsNone(await read_kv_usage(URL, transport=transport))


class ControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_configured_deployment_reports_its_usage(self):
        server = Server()
        control = CommandModelControl(
            {
                "main": DeploymentCommands(metrics_url=URL),
                "embed": DeploymentCommands(),
            },
            metrics_transport=server.transport(),
        )
        self.assertEqual(await control.kv_usage("main"), 0.73)
        self.assertIsNone(await control.kv_usage("embed"))  # no metrics_url
        self.assertIsNone(await control.kv_usage("other"))
        self.assertEqual(len(server.requests), 1)

    def test_the_url_and_the_timeout_are_checked(self):
        for url in (
            "",
            "ftp://127.0.0.1/metrics",
            "http://user:pw@127.0.0.1/metrics",
            "http://127.0.0.1/metrics?x=1",
            "http://127.0.0.1/metrics#x",
            "http://127.0.0.1:99999/metrics",
            "http:///metrics",
            "http://127.0.0.1/me trics",
            b"http://127.0.0.1/metrics",
            5,
        ):
            with self.subTest(url=url), self.assertRaises(InvalidComputeArgumentError):
                DeploymentCommands(metrics_url=url)
        for timeout in (0, -1, 6, True, "2"):
            with (
                self.subTest(timeout=timeout),
                self.assertRaises(InvalidComputeArgumentError),
            ):
                CommandModelControl({}, metrics_timeout=timeout)

    async def test_the_scheduler_counts_the_observed_usage(self):
        # The control of compute_support is replaced by a real one reading a
        # fake page: 0.95 of the pool in use refuses new work, as Decision
        # 0037, 2 ("the larger of the reservations and the observation").
        scheduler, _, fake, _ = build()
        server = Server(body="vllm:kv_cache_usage_perc 0.95\n")
        real = CommandModelControl(
            {"main": DeploymentCommands(metrics_url=URL)},
            metrics_transport=server.transport(),
        )
        fake.kv_usage = real.kv_usage
        await scheduler.refresh()
        self.assertEqual(
            scheduler.status().deployment("main").observed_kv_fraction, 0.95
        )
        admission = await scheduler.try_acquire(
            ComputeRequest(
                ResourceClass.INTERACTIVE, deployment="main", context_tokens=1_000
            )
        )
        self.assertEqual(admission.refusal, Refusal.KV_FULL)


if __name__ == "__main__":
    unittest.main()

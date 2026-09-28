"""``python -m paw_backend.cli compute-status`` (PAW-036): a read-only look at the
GPU through the same probe the scheduler uses. It prints what the scheduler would
count (total, used, headroom, available) and how many processes hold memory, but
not their pids: the processes may belong to other users. A fake probe here."""

import io
import json
import unittest

from paw_backend.cli import compute as cli
from paw_backend.cli import dispatch
from tests.compute_support import GIB, MIB, FakeProbe


def run(argv, probe):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, stdout=out, stderr=err, probe=probe)
    return code, out.getvalue(), err.getvalue()


class ComputeStatusTest(unittest.TestCase):
    def test_the_command_is_dispatched_here(self):
        self.assertIs(dispatch.command_module(["compute-status"]), cli)

    def test_it_prints_the_accounting_as_json(self):
        probe = FakeProbe(total=96 * GIB, external=70 * GIB)
        probe.resident[1234] = 4 * GIB
        code, out, err = run(["compute-status"], probe)
        self.assertEqual(code, cli.EXIT_OK, err)
        document = json.loads(out)
        (device,) = document["devices"]
        headroom = int(96 * GIB * 0.05)
        self.assertEqual(device["index"], 0)
        self.assertEqual(device["name"], "Fake GPU")
        self.assertEqual(device["total_mib"], 96 * GIB // MIB)
        self.assertEqual(device["used_mib"], 74 * GIB // MIB)
        self.assertEqual(device["headroom_mib"], headroom // MIB)
        self.assertEqual(
            device["available_mib"], (96 * GIB - headroom - 74 * GIB) // MIB
        )
        self.assertEqual(device["utilization_percent"], 10)
        self.assertEqual(device["processes"], 2)
        self.assertNotIn("1234", out)  # no pid is shown
        self.assertTrue(document["read_only"])

    def test_the_headroom_can_be_given(self):
        probe = FakeProbe(total=96 * GIB)
        code, out, _ = run(
            [
                "compute-status",
                "--headroom-min-mib",
                "10240",
                "--headroom-fraction",
                "0",
            ],
            probe,
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["devices"][0]["headroom_mib"], 10_240)

    def test_an_unavailable_probe_is_an_environment_error(self):
        probe = FakeProbe()
        probe.fail = True
        code, out, err = run(["compute-status"], probe)
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertEqual(out, "")
        self.assertIn("nvidia-smi", err)

    def test_usage_errors(self):
        for argv in (
            ["compute-status", "--headroom-fraction", "2"],
            ["compute-status", "--headroom-min-mib", "-1"],
            ["compute-status", "--nope"],
        ):
            with self.subTest(argv=argv):
                code, _, _ = run(argv, FakeProbe())
                self.assertEqual(code, cli.EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()

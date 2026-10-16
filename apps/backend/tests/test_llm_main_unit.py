"""The main LLM's vLLM unit (``deploy/systemd/paw-llm-main.service``).

The unit file is read as text (no systemd, no vLLM). Its ``ExecStart`` must carry
the coexistence values the main model's footprint was measured with (Decisions
0073 2, 0075, 0076 4: ``--gpu-memory-utilization 0.51``, ``--max-model-len
131072``, ``--max-num-seqs 8``, ``--language-model-only``; the passing runs of
``docs/benchmarks/paw-017-coexist-qwen38-2026-10.md`` and
``docs/benchmarks/paw-200-coexist-admission-2026-10.md``), and the parsers of
Decision 0083 12. Without them vLLM takes its own defaults (262,144 and 0.92 on
the 2026-10-15 smoke), which do not fit next to the other deployments.
"""

import configparser
import shlex
import unittest
from pathlib import Path

UNIT = (
    Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "paw-llm-main.service"
)


def service() -> configparser.SectionProxy:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string(UNIT.read_text(encoding="utf-8"))
    return parser["Service"]


def exec_start() -> list[str]:
    return shlex.split(service()["ExecStart"])


def option(argv: list[str], name: str) -> str | None:
    values = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == name]
    values += [item.split("=", 1)[1] for item in argv if item.startswith(name + "=")]
    if len(values) > 1:
        raise AssertionError(f"{name} is given more than once")
    return values[0] if values else None


class LlmMainUnitTest(unittest.TestCase):
    def test_the_coexistence_values_are_set(self):
        argv = exec_start()
        self.assertEqual(option(argv, "--gpu-memory-utilization"), "0.51")
        self.assertEqual(option(argv, "--max-model-len"), "131072")
        self.assertEqual(option(argv, "--max-num-seqs"), "8")
        self.assertIn("--language-model-only", argv)

    def test_the_server_and_parsers_are_unchanged(self):
        argv = exec_start()
        self.assertEqual(
            argv[:3], ["/opt/paw/runtime-venv/bin/vllm", "serve", "/srv/models/main"]
        )
        self.assertEqual(option(argv, "--served-model-name"), "main")
        self.assertEqual(option(argv, "--host"), "127.0.0.1")
        self.assertEqual(option(argv, "--port"), "18080")
        self.assertEqual(option(argv, "--reasoning-parser"), "qwen3")
        self.assertEqual(option(argv, "--tool-call-parser"), "qwen3_coder")
        self.assertIn("--enable-auto-tool-choice", argv)

    def test_an_oom_kill_stops_the_whole_unit_as_oom_kill(self):
        # Decision 0092, 1: ``OOMPolicy=stop`` whatever ``DefaultOOMPolicy=``
        # says, so the unit ends with ``Result=oom-kill`` (also when the kernel
        # kills only a child process) and the scheduler can read it; systemd
        # sees the kill only with memory accounting, which ``MemoryMax=`` turns on.
        unit = service()
        self.assertEqual(unit.get("OOMPolicy"), "stop")
        self.assertEqual(unit.get("MemoryMax"), "32G")
        self.assertNotIn("Delegate", unit)


if __name__ == "__main__":
    unittest.main()

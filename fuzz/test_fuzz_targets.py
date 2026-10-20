"""Replays every fuzz target on deterministic random inputs, without Atheris.

  python3 -m unittest discover -s fuzz -p 'test_*.py'

The Fuzzing workflow runs the same TestOneInput functions under Atheris. These
tests keep the targets working on any Python 3.11+ and show that each target's
checks catch a broken implementation of the code it fuzzes.
"""
from __future__ import annotations

import importlib
import random
import unittest
import unittest.mock

# The release-notes, switch and build-selection targets went with the code they
# fuzzed (the release tooling, now in apoorvdarshan/store-release).
TARGETS = ("fuzz_test_summary",)
RUNS = 3000


def random_inputs(name: str, runs: int = RUNS):
    rng = random.Random(name)
    yield b""
    yield b"\xff" * 64
    for _ in range(runs):
        yield rng.randbytes(rng.randint(0, 256))


def first_failure(module, runs: int = RUNS) -> AssertionError | None:
    for data in random_inputs(module.__name__, runs):
        try:
            module.TestOneInput(data)
        except AssertionError as exc:
            return exc
    return None


class FuzzTargetsTest(unittest.TestCase):
    def test_targets_hold_on_random_inputs(self) -> None:
        for name in TARGETS:
            with self.subTest(target=name):
                module = importlib.import_module(name)
                for data in random_inputs(name):
                    module.TestOneInput(data)

    def test_targets_catch_broken_implementations(self) -> None:
        summary = importlib.import_module("fuzz_test_summary")
        real_check = summary.check

        def skips_pass(counts, **kwargs):  # treats skipped tests as passing
            cleaned = {kind: {r: n for r, n in results.items() if r != "Skipped"} for kind, results in counts.items()}
            return real_check(cleaned, **kwargs)

        broken = ((summary, "check", skips_pass),)
        for module, name, replacement in broken:
            with self.subTest(target=module.__name__), unittest.mock.patch.object(module, name, replacement):
                self.assertIsNotNone(first_failure(module), f"{module.__name__} missed a broken {name}")


if __name__ == "__main__":
    unittest.main()

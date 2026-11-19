"""Run each fuzz target briefly on random inputs when Atheris is installed (CI fuzzes them properly
with ClusterFuzzLite)."""
import importlib
import os
import random

import pytest

atheris = pytest.importorskip("atheris")


@pytest.mark.parametrize("name", ["fuzz_config", "fuzz_rowhash", "fuzz_chunking"])
def test_fuzz_target_smoke(name, monkeypatch):
    monkeypatch.syspath_prepend(os.path.join(os.path.dirname(__file__), "fuzz"))
    mod = importlib.import_module(name)
    rng = random.Random(1234)
    for _ in range(2000):
        mod.TestOneInput(bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200))))

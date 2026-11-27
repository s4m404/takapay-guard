"""The replay and comparison scripts need the benchmark checkout and its
results, so they run by hand; here only their pure helpers are tested."""

import importlib.util
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_case_set_from_folder_name():
    r = load("replay_traces")
    assert r.case_set(Path("x/results")) == "main"
    assert r.case_set(Path("x/results_groq")) == "main"
    assert r.case_set(Path("x/results_groq_medium")) == "main"
    assert r.case_set(Path("x/results_groq_medium_third_party")) == "third_party"
    assert r.case_set(Path("x/results_message_payee")) == "message_payee"
    assert r.case_set(Path("x/results_adaptive")) == "adaptive"
    assert set(r.POLICIES) == {"provenance", "provenance-amount", "provenance-consistent"}


def test_percentile():
    r = load("replay_traces")
    assert r.pct([1000, 2000, 3000, 4000], 0.5) == 3.0


def test_comparison_script_imports():
    assert load("compare_with_benchmark").DEFENCES == ["provenance", "provenance-amount", "provenance-consistent"]

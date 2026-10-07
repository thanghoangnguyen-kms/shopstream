"""The Hypothesis profile that conftest.py loaded is the one the environment asked for."""

from __future__ import annotations

import os

from hypothesis import settings

EXPECTED_EXAMPLES = {"ci": 200, "dev": 200, "explore": 5000}


def expected_profile() -> str:
    return os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")


def test_the_loaded_profile_is_the_selected_one() -> None:
    loaded = settings.default
    assert loaded is not None
    name = settings.get_current_profile_name()
    assert name == expected_profile()
    assert loaded.max_examples == EXPECTED_EXAMPLES[name]


def test_dev_and_ci_run_the_same_settings() -> None:
    dev, ci = settings.get_profile("dev"), settings.get_profile("ci")
    for field in (
        "max_examples",
        "derandomize",
        "deadline",
        "database",
        "print_blob",
        "phases",
        "stateful_step_count",
        "suppress_health_check",
    ):
        assert getattr(dev, field) == getattr(ci, field), field


def test_explore_runs_5000_examples_with_no_deadline() -> None:
    explore = settings.get_profile("explore")
    assert explore.max_examples == 5000
    assert explore.deadline is None

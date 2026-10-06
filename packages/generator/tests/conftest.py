"""Hypothesis profiles. `dev` is `ci`, so `just check` and CI run the same settings."""

from __future__ import annotations

import os

from hypothesis import settings

# The built-in `ci`: derandomize, deadline=None, database=None, print_blob, no too_slow check.
settings.register_profile("ci", settings.get_profile("ci"), max_examples=200)
settings.register_profile("dev", settings.get_profile("ci"))
settings.register_profile(
    "explore", settings.get_profile("default"), max_examples=5000, deadline=None
)

settings.load_profile(
    os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev")
)

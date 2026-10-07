"""The golden manifest entry point: `python -m shopstream_generator.golden --config PATH --out PATH`.

It runs the engine over the config's whole range through the stream hasher, writes the manifest
bytes to `--out` and prints the run hash and one `name count sha256` line per stream, in the
fixed stream order. It prints counts and digests only, never a record: public CI logs are
copies erasure can't reach (ADR-005).

The package computes no repo path; the caller (the `generator-golden` recipe, or the
determinism test with a temp path) supplies both.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as model_config
from .config import ModelConfig
from .engine import Engine
from .manifest import Manifest, StreamHasher


def build_manifest(config: ModelConfig) -> Manifest:
    """Run `[start, end)` through the hasher and return the manifest for this config."""
    hasher = StreamHasher()
    for tick in Engine.new(config).run_until(config.end_us):
        hasher.add_tick(tick)
    return hasher.manifest(config.config_sha256())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the golden config JSON")
    parser.add_argument("--out", type=Path, required=True, help="where to write the manifest")
    args = parser.parse_args(argv)
    manifest = build_manifest(model_config.load(args.config))
    args.out.write_bytes(manifest.to_bytes())
    print(f"run_sha256 {manifest.run_sha256()}")
    for name, digest in manifest.streams:
        print(f"{name} {digest.count} {digest.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

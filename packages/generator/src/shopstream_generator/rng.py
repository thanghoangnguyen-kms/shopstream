"""The only place the generator constructs a random number generator (D-06, D-07).

`new_random` is the package's one RNG constructor and carries the package's one S311 suppression. Every
other module takes a `Stream` from here. A `Stream` is a named, seeded wrapper over the stdlib
Mersenne Twister that implements its own integer primitives on top of `getrandbits`, so no
float ever reaches a decision and the output doesn't depend on the internals of `randrange`,
`choices` or `random()`.

Streams are independent: the seed string is `f"{seed}:{name}"`, so adding a stream never
changes another stream's output.

Word accounting. Every draw adds `ceil(k / 32)` to `Stream.words`, the number of 32-bit words
taken from the Mersenne Twister. Replaying that many `getrandbits(32)` calls on a fresh
generator reproduces its state, which is what keeps a snapshot-plus-fast-forward checkpoint
possible later.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping, Sequence
from enum import StrEnum


class StreamName(StrEnum):
    """The closed set of stream names; later phases append, never rename."""

    CUSTOMERS = "customers"
    PRODUCTS = "products"
    ORDERS = "orders"
    LIFECYCLE = "lifecycle"
    PAYMENTS = "payments"
    REVIEWS = "reviews"
    TEXT = "text"


def new_random(seed: int, name: str) -> random.Random:
    """The stdlib generator for `(seed, name)`: the only RNG constructor in the package."""
    return random.Random(f"{seed}:{name}")  # noqa: S311


class Stream:
    """One named stream of integer draws."""

    def __init__(self, seed: int, name: StreamName) -> None:
        self.name = name
        self.words = 0
        self._random = new_random(seed, name.value)

    def bits(self, k: int) -> int:
        """`k` random bits, as an int; `k = 0` returns 0 and consumes no word."""
        if k < 0:
            raise ValueError("bits(k) needs k >= 0")
        self.words += (k + 31) // 32
        return self._random.getrandbits(k)

    def below(self, n: int) -> int:
        """A uniform int in [0, n), by rejection sampling.

        The loop is CPython's `_randbelow_with_getrandbits`, so a test can use the public
        `Random.randrange(n)` as an independent oracle. The number of words it consumes depends
        on `n` and on the rejections, so it is for config-bounded draws only.
        """
        if n <= 0:
            raise ValueError("below(n) needs n > 0")
        k = n.bit_length()
        value = self.bits(k)
        while value >= n:
            value = self.bits(k)
        return value

    def between(self, a: int, b: int) -> int:
        """A uniform int in [a, b], both ends included."""
        if b < a:
            raise ValueError("between(a, b) needs a <= b")
        return a + self.below(b - a + 1)

    # RED stubs: the next commit replaces these with the real primitives.
    def pick(self, n: int) -> int | None:
        raise NotImplementedError

    def choice_index(self, weights: Sequence[int]) -> int:
        raise NotImplementedError

    def bernoulli_ppm(self, ppm: int) -> bool:
        raise NotImplementedError

    def uuid4(self) -> uuid.UUID:
        raise NotImplementedError

    def dump(self) -> dict[str, object]:
        raise NotImplementedError

    @classmethod
    def load(cls, seed: int, name: StreamName, data: Mapping[str, object]) -> Stream:
        raise NotImplementedError

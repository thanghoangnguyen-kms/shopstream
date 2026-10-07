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

Streams and the process each one feeds:

- customers: sign-up arrivals and new-customer attributes
- products: launches and new-product attributes
- orders: order arrivals and order plans
- lifecycle: in-life update arrivals
- payments: payment plan draws
- reviews: review plan draws
- text: word-list indices

Draw rules every later plan follows:

- `pick(n)` for any index into a list or a live population. It always takes exactly two words
  (a 64-bit value modulo n; the bias is below 2**-32 for n under 2**32), so a later knob that
  changes a population size can't change how many words a base stream consumes. It is an
  addition to D-07's set, and it returns None for an empty population.
- `below` and `between` for integers bounded by config, whose word count may vary.
- `choice_index` for an integer-weighted table.
- `bernoulli_ppm` for a rate in parts per million.
- A handler draws the same number of values whether or not a branch fires (D-09), so one
  outcome never shifts a later draw or another stream.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Mapping, Sequence
from enum import StrEnum

PPM = 1_000_000
MT_WORDS = 624


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

    def pick(self, n: int) -> int | None:
        """An index into a population of `n`, or None when it is empty.

        Always consumes exactly two words, whatever `n` is and whether or not it is empty.
        """
        if n < 0:
            raise ValueError("pick(n) needs n >= 0")
        value = self.bits(64)
        return None if n == 0 else value % n

    def choice_index(self, weights: Sequence[int]) -> int:
        """An index drawn with integer weights; a zero weight is never chosen."""
        if not weights or any(type(weight) is not int or weight < 0 for weight in weights):
            raise ValueError("choice_index needs a non-empty table of non-negative ints")
        total = sum(weights)
        if total <= 0:
            raise ValueError("choice_index needs a positive total weight")
        r = self.below(total)
        cumulative = 0
        for index, weight in enumerate(weights):
            cumulative += weight
            if cumulative > r:
                return index
        raise AssertionError("unreachable: r is below the total")  # pragma: no cover

    def bernoulli_ppm(self, ppm: int) -> bool:
        """True with probability `ppm` per million (D-08); it always draws once."""
        if not 0 <= ppm <= PPM:
            raise ValueError("bernoulli_ppm needs 0 <= ppm <= 1_000_000")
        return self.below(PPM) < ppm

    def uuid4(self) -> uuid.UUID:
        """A version-4 UUID built from 128 drawn bits (CORE-04); takes four words."""
        return uuid.UUID(int=self.bits(128), version=4)

    def dump(self) -> dict[str, object]:
        """The generator state as plain JSON ints: 624 state words, the position, the count."""
        _, internal, _ = self._random.getstate()
        return {
            "mt": [int(word) for word in internal[:-1]],
            "pos": int(internal[-1]),
            "words": self.words,
        }

    @classmethod
    def load(cls, seed: int, name: StreamName, data: Mapping[str, object]) -> Stream:
        """Rebuild a stream from `dump()` output (after a JSON round trip, lists stay lists)."""
        mt, pos, words = data.get("mt"), data.get("pos"), data.get("words")
        if (
            not isinstance(mt, list)
            or len(mt) != MT_WORDS
            or any(type(word) is not int for word in mt)
            or type(pos) is not int
            or not 0 <= pos <= MT_WORDS
            or type(words) is not int
            or words < 0
        ):
            raise ValueError("stream state is malformed")
        stream = cls(seed, name)
        try:
            stream._random.setstate((3, (*mt, pos), None))
        except (TypeError, ValueError):
            raise ValueError("stream state is malformed") from None
        stream.words = words
        return stream

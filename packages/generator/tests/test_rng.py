"""Stream primitives: pinned vectors, oracle agreement, exact word accounting (CORE-03, CORE-04).

The oracle is the public `Random.randrange` of a same-seeded generator built by
`rng.new_random`. It lives in these tests only: the package never calls `randrange`, so a
future CPython change to it can only fail an oracle test, never move the golden.
"""

from __future__ import annotations

import json
import random
import uuid

import pytest
from shopstream_generator import rng
from shopstream_generator.rng import Stream, StreamName

SEED = 42


def stream(name: StreamName = StreamName.CUSTOMERS, seed: int = SEED) -> Stream:
    return Stream(seed, name)


def oracle(name: StreamName = StreamName.CUSTOMERS, seed: int = SEED) -> random.Random:
    return rng.new_random(seed, name.value)


def test_stream_names_are_exactly_the_closed_set() -> None:
    assert tuple(name.value for name in StreamName) == (
        "customers",
        "products",
        "orders",
        "lifecycle",
        "payments",
        "reviews",
        "text",
    )


def test_pinned_first_words_of_a_named_stream() -> None:
    drawn = stream()
    assert [drawn.bits(32) for _ in range(5)] == [
        2835817191,
        500954736,
        1301126823,
        808494532,
        2975048584,
    ]


def test_pinned_first_words_of_the_oracle_seed() -> None:
    generator = rng.new_random(0, "x")
    assert [generator.getrandbits(32) for _ in range(3)] == [3866281616, 3402671247, 3123923870]


@pytest.mark.parametrize("n", [1, 2, 3, 2**31, 2**32, 2**32 + 1, 10**12], ids=lambda n: f"n={n}")
def test_below_matches_randrange_of_a_same_seeded_oracle(n: int) -> None:
    drawn, reference = stream(), oracle()
    assert [drawn.below(n) for _ in range(40)] == [reference.randrange(n) for _ in range(40)]


@pytest.mark.parametrize(("a", "b"), [(0, 0), (5, 9), (-3, 3), (1, 10**12)])
def test_between_matches_randrange_with_an_inclusive_end(a: int, b: int) -> None:
    drawn, reference = stream(), oracle()
    assert [drawn.between(a, b) for _ in range(40)] == [
        reference.randrange(a, b + 1) for _ in range(40)
    ]


def test_between_of_equal_ends_returns_that_value() -> None:
    assert stream().between(7, 7) == 7


@pytest.mark.parametrize("n", [0, -1])
def test_below_rejects_a_non_positive_bound(n: int) -> None:
    with pytest.raises(ValueError, match="below"):
        stream().below(n)


def test_between_rejects_a_reversed_range() -> None:
    with pytest.raises(ValueError, match="between"):
        stream().between(5, 4)


def test_below_one_is_zero_and_draws_until_a_zero_bit() -> None:
    # k = 1 bit, rejected while it is 1: the loop is CPython's, so it is not free.
    for seed in range(20):
        drawn, reference = stream(seed=seed), oracle(seed=seed)
        expected_words = 1
        while reference.getrandbits(1) != 0:
            expected_words += 1
        assert drawn.below(1) == 0
        assert drawn.words == expected_words


def test_below_one_takes_a_single_word_when_the_first_bit_is_zero() -> None:
    seed = next(s for s in range(100) if oracle(seed=s).getrandbits(1) == 0)
    drawn = stream(seed=seed)
    assert drawn.below(1) == 0
    assert drawn.words == 1


def test_bits_of_zero_returns_zero_and_consumes_no_word() -> None:
    drawn = stream()
    assert drawn.bits(0) == 0
    assert drawn.words == 0
    assert drawn.bits(32) == 2835817191  # the first word is still unspent


def test_bits_rejects_a_negative_width() -> None:
    with pytest.raises(ValueError, match="bits"):
        stream().bits(-1)


@pytest.mark.parametrize(
    ("k", "words"),
    [(0, 0), (1, 1), (31, 1), (32, 1), (33, 2), (64, 2), (65, 3), (128, 4)],
)
def test_bits_adds_ceil_k_over_32_words(k: int, words: int) -> None:
    drawn = stream()
    drawn.bits(k)
    assert drawn.words == words


def test_replaying_the_counted_words_reproduces_the_generator_state() -> None:
    drawn = stream()
    drawn.below(10**12)
    drawn.between(1, 6)
    drawn.bits(65)
    drawn.pick(97)
    drawn.uuid4()
    drawn.bernoulli_ppm(20_000)
    replay = oracle()
    for _ in range(drawn.words):
        replay.getrandbits(32)
    _, internal, _ = replay.getstate()
    data = drawn.dump()
    assert data["mt"] == [int(x) for x in internal[:-1]]
    assert data["pos"] == int(internal[-1])


@pytest.mark.parametrize("n", [0, 1, 7, 2**40])
def test_pick_always_consumes_exactly_two_words(n: int) -> None:
    drawn = stream()
    value = drawn.pick(n)
    assert drawn.words == 2
    if n == 0:
        assert value is None
    else:
        assert value is not None
        assert 0 <= value < n


def test_pick_rejects_a_negative_population() -> None:
    with pytest.raises(ValueError, match="pick"):
        stream().pick(-1)


def test_pick_is_the_64_bit_value_modulo_n() -> None:
    assert stream().pick(1000) == oracle().getrandbits(64) % 1000


def test_choice_index_never_returns_a_zero_weight_and_matches_the_oracle() -> None:
    weights = [0, 3, 0, 5, 1, 0]
    total = sum(weights)
    drawn, reference = stream(), oracle()
    for _ in range(2000):
        index = drawn.choice_index(weights)
        r = reference.randrange(total)
        cumulative = 0
        expected = -1
        for position, weight in enumerate(weights):
            cumulative += weight
            if cumulative > r:
                expected = position
                break
        assert weights[index] > 0
        assert index == expected


@pytest.mark.parametrize(
    "weights", [[], [0, 0], [1, -1, 3], [-2]], ids=["empty", "zeros", "neg", "neg-only"]
)
def test_choice_index_rejects_an_unusable_table(weights: list[int]) -> None:
    with pytest.raises(ValueError, match="choice_index"):
        stream().choice_index(weights)


def test_bernoulli_ppm_edges_are_never_and_always() -> None:
    drawn = stream()
    assert not any(drawn.bernoulli_ppm(0) for _ in range(2000))
    assert all(drawn.bernoulli_ppm(1_000_000) for _ in range(2000))


@pytest.mark.parametrize("ppm", [-1, 1_000_001])
def test_bernoulli_ppm_rejects_an_out_of_range_rate(ppm: int) -> None:
    with pytest.raises(ValueError, match="bernoulli_ppm"):
        stream().bernoulli_ppm(ppm)


def test_bernoulli_ppm_is_below_a_million_less_than_ppm() -> None:
    drawn, reference = stream(), oracle()
    assert [drawn.bernoulli_ppm(250_000) for _ in range(200)] == [
        reference.randrange(1_000_000) < 250_000 for _ in range(200)
    ]


def test_uuid4_is_version_4_rfc_4122_and_takes_four_words() -> None:
    drawn = stream()
    for index in range(1, 1001):
        value = drawn.uuid4()
        assert value.version == 4
        assert value.variant == uuid.RFC_4122
        assert drawn.words == 4 * index


def test_dump_survives_json_and_load_continues_the_same_draws() -> None:
    original = stream()
    for _ in range(37):
        original.below(1000)
    restored = Stream.load(SEED, StreamName.CUSTOMERS, json.loads(json.dumps(original.dump())))
    assert restored.words == original.words
    assert [restored.below(10**6) for _ in range(100)] == [
        original.below(10**6) for _ in range(100)
    ]
    assert restored.words == original.words


def test_load_rejects_malformed_data() -> None:
    good = stream().dump()
    for bad in ({}, {**good, "mt": [1, 2, 3]}, {**good, "pos": "x"}, {**good, "words": -1}):
        with pytest.raises(ValueError, match="stream"):
            Stream.load(SEED, StreamName.CUSTOMERS, bad)


def test_adding_a_stream_never_changes_another() -> None:
    quiet = stream(StreamName.ORDERS)
    expected = [quiet.below(10**6) for _ in range(50)]
    for other in (StreamName.CUSTOMERS, StreamName.REVIEWS, StreamName.TEXT):
        busy = stream(other)
        for _ in range(100):
            busy.below(1000)
    later = stream(StreamName.ORDERS)
    assert [later.below(10**6) for _ in range(50)] == expected

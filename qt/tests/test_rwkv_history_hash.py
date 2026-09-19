# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The RWKV history hash is the state cache's identity, so its bytes must not
move. These tests pin the fast record encoder to the buffer writer it
replaces, and the hash chain kept as bytes to the hex-in, hex-out function.
A difference of one byte would make every stored state cache stale."""

from __future__ import annotations

import random

from aqt import rwkv_scheduler as rwkv
from aqt.rwkv_scheduler import RwkvReviewIdentity, RwkvReviewInput

GOLDEN_HASH_SEED_2 = "bf34e21b57a9a219681458fc4f7b4e2300bdeab783c8a2ea1bdcd709f0463c5e"


def _reference_record(review_id: int, review_input: RwkvReviewInput) -> bytes:
    """The encoding as it was written before: the buffer writers."""

    out = bytearray()
    rwkv._write_i64(out, review_id)
    rwkv._write_review_input(out, review_input)
    return bytes(out)


def _random_reviews(count: int, seed: int) -> list[tuple[int, RwkvReviewInput]]:
    rng = random.Random(seed)
    kinds = [None, "review", "learning", "relearning", "new", "ünïcode ✓"]

    def maybe(value: int) -> int | None:
        return None if rng.random() < 0.2 else value

    reviews = []
    for index in range(count):
        reviews.append(
            (
                rng.choice([1, 1_500_000_000_000 + index, 2**63 - 1, -(2**63)]),
                RwkvReviewInput(
                    identity=RwkvReviewIdentity(
                        card_id=rng.randrange(-(2**63), 2**63),
                        note_id=maybe(rng.randrange(-(2**63), 2**63)),
                        deck_id=maybe(rng.randrange(1, 2**40)),
                        preset_id=maybe(rng.randrange(-(2**63), 2**63)),
                    ),
                    is_query=rng.random() < 0.3,
                    ease=maybe(rng.randrange(0, 5)),
                    duration_millis=maybe(rng.randrange(0, 600_000)),
                    card_type=maybe(rng.randrange(0, 6)),
                    card_queue=maybe(rng.randrange(-3, 5)),
                    card_due=maybe(rng.randrange(-(2**40), 2**40)),
                    interval_days=maybe(rng.randrange(-100, 40_000)),
                    ease_factor=maybe(rng.randrange(0, 4000)),
                    reps=maybe(rng.randrange(0, 1000)),
                    lapses=maybe(rng.randrange(0, 100)),
                    day_offset=maybe(rng.randrange(-40_000, 40_000)),
                    current_state_kind=rng.choice(kinds),
                    current_normal_state_kind=rng.choice(kinds),
                    current_elapsed_days=maybe(rng.randrange(-1, 40_000)),
                    current_elapsed_seconds=maybe(rng.randrange(-1, 10**10)),
                ),
            )
        )
    return reviews


def test_the_record_encoding_is_byte_for_byte_the_buffer_writers() -> None:
    reviews = _random_reviews(5000, seed=1)
    # a record with every optional field absent, and one with every field set
    empty = RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=7),
        is_query=False,
        ease=None,
        duration_millis=None,
        card_type=None,
        card_queue=None,
        card_due=None,
        interval_days=None,
        ease_factor=None,
        reps=None,
        lapses=None,
        day_offset=None,
        current_state_kind=None,
        current_normal_state_kind=None,
        current_elapsed_days=None,
        current_elapsed_seconds=None,
    )
    reviews.append((0, empty))
    for review_id, review_input in reviews:
        assert rwkv._encode_rwkv_delta_record(
            review_id, review_input
        ) == _reference_record(review_id, review_input)


def test_the_hash_kept_as_bytes_is_the_hash_chain() -> None:
    reviews = _random_reviews(3000, seed=2)
    start = rwkv._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH

    expected = start
    hasher = rwkv._RwkvHistoryHasher(start)
    for index, (review_id, review_input) in enumerate(reviews):
        expected = rwkv._rwkv_history_hash_after_review(
            expected, review_id, review_input
        )
        hasher.update(review_id, review_input)
        # a hash read out in the middle is the chain's hash at that point
        if index % 500 == 0:
            assert hasher.hexdigest() == expected
    assert hasher.hexdigest() == expected
    # computed with the encoder and hash function as they were before this
    # speedup (origin/main 2e6ac3b88), so the pin does not rest on the code
    # it checks
    assert expected == GOLDEN_HASH_SEED_2


def test_the_hasher_refuses_an_invalid_starting_hash() -> None:
    for bad in ["", "0" * 63, "G" * 64, "A" * 64, " " + "0" * 63]:
        try:
            rwkv._RwkvHistoryHasher(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")

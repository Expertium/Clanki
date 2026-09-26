# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The replay inputs the backend builds are the ones the Python build gave,
field by field and bit for bit.

The backend (`rslib/src/scheduler/rwkv_inputs`) is the one builder of the
replay inputs. It reads the whole history from nothing itself; every other
read (after a cutoff, of a deck, with dynamic preset replay, or of a
collection the backend reads another way) reads its rows and presets in
Python and hands them to the same encoder. Each way of asking is compared
here with `_reference_inputs`, the Python build the backend replaced, kept
below as the parity oracle, and the two backend reads with each other.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki.collection import Collection
from aqt import rwkv_scheduler
from aqt import rwkv_scheduler as rs
from aqt.rwkv_scheduler import (
    RwkvFirstReviewElapsedSource,
    RwkvHistoricalReviewInputs,
    RwkvReplayReviews,
    RwkvReviewIdentity,
    RwkvReviewInput,
)
from aqt.rwkv_srs_benchmark import (
    _PACKED_PREDICTION_REQUEST_ROW,
    _packed_review_input_row,
    _packed_warm_up_reviews,
)

DAY_MS = 86_400 * 1000
# (ease, review kind, ease factor)
LEARNING = (3, 0, 2500)
REVIEW = (3, 1, 2500)
HARD_REVIEW = (2, 1, 2300)
RELEARNING = (1, 2, 2500)
FILTERED = (4, 3, 2500)
FORGET = (0, 4, 0)
MANUAL_RATED = (3, 5, 2500)
SHAPES = [
    [LEARNING, REVIEW, RELEARNING, HARD_REVIEW, REVIEW],
    [REVIEW, REVIEW, FORGET, REVIEW, RELEARNING],
    [LEARNING, LEARNING, REVIEW, FORGET, LEARNING, REVIEW, FILTERED],
    [REVIEW, RELEARNING, MANUAL_RATED],
    [LEARNING, FILTERED, REVIEW, LEARNING, LEARNING, REVIEW],
]


def _build(path: Path) -> Collection:
    """Cards in three presets' decks (one card in a filtered deck, whose
    home deck counts), with every shape of history: Learning starts, Forget
    cuts, relearning, filtered and manual rows, reviews spread over weeks and
    interleaved across cards, some cards created long before their first
    review and one after it, and cards that were deleted, whose reviews are
    all that is left of them (spec sched.rwkv-replay-deleted-cards)."""
    col = Collection(str(path))
    first_review = 1_600_000_000_000
    col.crt = first_review // 1000 - 30 * 86_400
    home = col.decks.id("Home")
    other = col.decks.id("Other")
    other_config = col.decks.add_config_returning_id("Other preset")
    deck = col.decks.get(other)
    deck["conf"] = other_config
    col.decks.save(deck)
    filtered = col.decks.new_filtered("Filtered")
    decks = [1, home, other]
    for n in range(40):
        card_id = first_review - (n % 3) * 20 * DAY_MS + n * 7_919
        if n == 7:
            # created after its first review
            card_id = first_review + 90 * DAY_MS
        deck_id = decks[n % 3]
        in_filtered = n % 11 == 5
        col.db.execute(
            "insert into cards (id, nid, did, ord, mod, usn, type, queue, due, "
            "ivl, factor, reps, lapses, left, odue, odid, flags, data) "
            "values (?, ?, ?, 0, 0, -1, 2, 2, 1, 20, 2500, 0, 0, 0, 0, ?, 0, '')",
            card_id,
            1_000 + n // 2,
            filtered if in_filtered else deck_id,
            deck_id if in_filtered else 0,
        )
        if n % 9 == 4:
            col.db.execute("delete from cards where id = ?", card_id)
        for offset, (ease, kind, factor) in enumerate(SHAPES[n % len(SHAPES)]):
            col.db.execute(
                "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, "
                "time, type) values (?, ?, -1, ?, ?, 5, ?, ?, ?)",
                first_review + (offset * 3 + n % 4) * DAY_MS + n * 1_013,
                card_id,
                ease,
                offset + 1,
                factor,
                700 + 37 * n + offset,
                kind,
            )
    col.close()
    # the scheduler reads the creation time when the collection opens
    return Collection(str(path))


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Collection]:
    col = _build(tmp_path / "replay-inputs.anki2")
    try:
        yield col
    finally:
        col.close()


def _reviewer(col: Collection) -> SimpleNamespace:
    return SimpleNamespace(mw=SimpleNamespace(col=col))


def _from_rows(
    col: Collection, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> RwkvHistoricalReviewInputs:
    """The inputs of rows Python read, as a read the backend does not do
    itself gets them."""
    with monkeypatch.context() as patch:
        patch.setattr(
            rwkv_scheduler,
            "_backend_historical_rwkv_review_inputs",
            lambda *_args, **_kwargs: None,
        )
        return rwkv_scheduler._historical_rwkv_review_inputs(_reviewer(col), **kwargs)


def _all_three(
    col: Collection, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[
    RwkvHistoricalReviewInputs, RwkvHistoricalReviewInputs, RwkvHistoricalReviewInputs
]:
    """The inputs the backend read itself, the ones of rows Python read, and
    the reference."""
    built = []
    real = rwkv_scheduler._backend_historical_rwkv_review_inputs

    def backend(*args: Any, **inner: Any) -> RwkvHistoricalReviewInputs | None:
        result = real(*args, **inner)
        built.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(rwkv_scheduler, "_backend_historical_rwkv_review_inputs", backend)
        from_backend = rwkv_scheduler._historical_rwkv_review_inputs(
            _reviewer(col), **kwargs
        )
    assert built and built[0] is from_backend, "the backend did not read the inputs"
    return (
        from_backend,
        _from_rows(col, monkeypatch, **kwargs),
        _reference_inputs(_reviewer(col), **kwargs),
    )


def _assert_same(
    built: RwkvHistoricalReviewInputs, reference: RwkvHistoricalReviewInputs
) -> None:
    assert isinstance(built.reviews, RwkvReplayReviews)
    assert built == reference
    # the fields equality leaves out, and what it cannot see: the order of
    # the per-card maps, and bool against int
    assert (
        built.prepared_checkpoint_histories == reference.prepared_checkpoint_histories
    )
    for history, other in (
        (built, reference),
        *(
            (built.prepared_checkpoint_histories[key], value)
            for key, value in reference.prepared_checkpoint_histories.items()
        ),
    ):
        for field in (
            "previous_review_id_by_card",
            "previous_interval_days_by_card",
            "review_count_by_card",
        ):
            assert list(getattr(history, field).items()) == list(
                getattr(other, field).items()
            )
        assert repr(
            (history.last_review_id, history.review_count, history.history_hash)
        ) == repr((other.last_review_id, other.review_count, other.history_hash))
    assert type(built.review_ids) is list
    assert repr(built.review_ids) == repr(reference.review_ids)
    for built_review, reference_review in zip(
        built.reviews, reference.reviews, strict=True
    ):
        assert repr(built_review) == repr(reference_review)
        assert [type(value) for value in vars(built_review).values()] == [
            type(value) for value in vars(reference_review).values()
        ]
        assert [type(value) for value in vars(built_review.identity).values()] == [
            type(value) for value in vars(reference_review.identity).values()
        ]
    # the warm-up hands the builder's rows to the model as they are: they are
    # the rows the Python packer makes of the reference's inputs
    assert _packed_warm_up_reviews(built.reviews) == _packed_warm_up_reviews(
        list(reference.reviews)
    )


def _assert_all_same(
    col: Collection, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> RwkvHistoricalReviewInputs:
    backend, rows, reference = _all_three(col, monkeypatch, **kwargs)
    _assert_same(backend, reference)
    _assert_same(rows, reference)
    return backend


def test_the_backend_builds_the_whole_history_as_python_did(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _assert_all_same(col, monkeypatch)
    assert backend.review_count > 100
    # the history has what makes the fields differ from review to review
    assert {review.card_type for review in backend.reviews} >= {0, 2, 3, 4}
    presets = {review.identity.preset_id for review in backend.reviews}
    assert len(presets - {None}) == 2
    assert any(review.current_elapsed_days == -1 for review in backend.reviews)
    assert any(review.current_elapsed_seconds > 86_400 for review in backend.reviews)
    assert len({review.day_offset for review in backend.reviews}) > 10


def test_a_deleted_cards_reviews_are_replayed_without_ids(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins spec sched.rwkv-replay-deleted-cards: the reviews of a card that
    is gone stay in the replay, in both reads, with no note, deck or preset,
    and the fingerprint accepts the history."""
    backend = _assert_all_same(col, monkeypatch)
    existing = set(col.db.list("select id from cards"))
    deleted = [
        review for review in backend.reviews if review.identity.card_id not in existing
    ]
    assert len({review.identity.card_id for review in deleted}) == 4
    assert all(
        (review.identity.note_id, review.identity.deck_id, review.identity.preset_id)
        == (None, None, None)
        for review in deleted
    )
    assert all(
        review.identity.note_id is not None
        for review in backend.reviews
        if review.identity.card_id in existing
    )
    fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
        _reviewer(col),
        expected_identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=backend.last_review_id,
            review_count=backend.review_count,
            history_hash=backend.history_hash,
        ),
    )
    assert fingerprint is not None and fingerprint.history_is_valid


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hash_history": False},
        {"prepare_recovery_checkpoint": True},
        {"first_review_elapsed_source": RwkvFirstReviewElapsedSource.CARD_CREATION},
        {"first_review_elapsed_source": RwkvFirstReviewElapsedSource.MISSING},
        {"between_steps": lambda: None, "progress": lambda *_args: None},
    ],
    ids=["no-hash", "checkpoint", "card-creation", "missing", "in-steps"],
)
def test_every_way_of_asking_gets_the_python_inputs(
    col: Collection, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    backend = _assert_all_same(col, monkeypatch, **kwargs)
    if kwargs.get("prepare_recovery_checkpoint"):
        assert backend.prepared_checkpoint_histories


def test_ignored_reviews_leave_the_history_before_its_start_rows(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins spec sched.rwkv-replay-start-row: the ignored reviews leave the
    rows before each card's start row is found, in both reads, as the
    fingerprint drops them. An ignored start row is no start, so its card
    starts at another row, and the fingerprint accepts the history."""
    reviewer = _reviewer(col)
    whole = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    starts = [
        review_id
        for review_id, review in zip(whole.review_ids, whole.reviews, strict=True)
        if review.card_type == 0
    ]
    # a start row, two plain rows, and a review that is not in the history
    ignored = frozenset({starts[1], whole.review_ids[5], whole.review_ids[-1], 12345})
    for kwargs in (
        {"ignored_review_ids": ignored},
        {"ignored_review_ids": ignored, "prepare_recovery_checkpoint": True},
    ):
        backend = _assert_all_same(col, monkeypatch, **kwargs)
    assert backend.ignored_review_ids == tuple(sorted(ignored - {12345}))
    assert not ignored & set(backend.review_ids)
    # the card of the ignored start row starts at another row now
    card_id = next(
        review.identity.card_id
        for review_id, review in zip(whole.review_ids, whole.reviews, strict=True)
        if review_id == starts[1]
    )
    card_starts = [
        review_id
        for review_id, review in zip(backend.review_ids, backend.reviews, strict=True)
        if review.identity.card_id == card_id and review.card_type == 0
    ]
    assert len(card_starts) == 1 and card_starts[0] != starts[1]
    fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
        reviewer,
        ignored_review_ids=backend.ignored_review_ids,
        expected_identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=backend.last_review_id,
            review_count=backend.review_count,
            history_hash=backend.history_hash,
        ),
    )
    assert fingerprint is not None and fingerprint.history_is_valid
    assert fingerprint.active_ignored_review_ids == backend.ignored_review_ids


@pytest.mark.parametrize("fraction", [0.0, 0.3, 0.7, 0.97, 1.0])
def test_a_read_after_a_cutoff_goes_on_from_the_stored_state(
    col: Collection, monkeypatch: pytest.MonkeyPatch, fraction: float
) -> None:
    """A read after a cutoff (the state cache's restore, the exact rebuild's
    catch-up) goes on from the per-card state and the hash the whole read
    had at the cutoff, as the Python build did, and ends where the whole
    read ends."""
    reviewer = _reviewer(col)
    whole = rwkv_scheduler._historical_rwkv_review_inputs(
        reviewer, prepare_recovery_checkpoint=True
    )
    prefix = max(1, int(len(whole.review_ids) * fraction))
    base = rwkv_scheduler._rwkv_history_prefix(whole, prefix)
    kwargs: dict[str, Any] = {
        "after_review_id": base.last_review_id,
        "previous_review_id_by_card": dict(base.previous_review_id_by_card),
        "previous_interval_days_by_card": dict(base.previous_interval_days_by_card),
        "review_count_by_card": dict(base.review_count_by_card),
        "previous_history_hash": base.history_hash,
        "previous_replay_key": base.replay_key,
    }
    for extra in ({}, {"prepare_recovery_checkpoint": True}):
        built = _from_rows(col, monkeypatch, **kwargs, **extra)
        reference = _reference_inputs(reviewer, **kwargs, **extra)
        if prefix == len(whole.review_ids):
            # nothing after the cutoff: the stored state, unchanged
            assert built == reference and not built.reviews
            continue
        _assert_same(built, reference)
        assert built.history_hash == whole.history_hash
        assert built.review_count == whole.review_count
        assert built.previous_review_id_by_card == whole.previous_review_id_by_card
        assert list(built.reviews) == list(whole.reviews[prefix:])
    # without the stored state, the rows before the cutoff are counted, not
    # replayed, and the cards start afresh after it
    built = _from_rows(col, monkeypatch, after_review_id=base.last_review_id)
    if built.reviews:
        _assert_same(
            built, _reference_inputs(reviewer, after_review_id=base.last_review_id)
        )


def test_a_deck_is_read_with_its_subdecks_as_python_did(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer = _reviewer(col)
    for name in ("Home", "Other", "Default"):
        deck_id = col.decks.id_for_name(name)
        assert deck_id is not None
        built = _from_rows(col, monkeypatch, deck_id=deck_id)
        reference = _reference_inputs(reviewer, deck_id=deck_id)
        _assert_same(built, reference)
        assert built.reviews and built.deck_id == deck_id


def test_an_add_on_preset_rule_moves_cards_the_same_way(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    card_ids = sorted(col.db.list("select id from cards"))
    col.set_config(
        "fsrsPresetOverlay",
        {
            "presets": [
                {
                    "id": "addon:test:moved",
                    "name": "Moved",
                    "params": [],
                    "desired_retention": 0.9,
                    "historical_retention": 0.9,
                }
            ],
            "rules": [
                {
                    "search": f"cid:{card_ids[3]},{card_ids[9]}",
                    "preset_id": "addon:test:moved",
                }
            ],
            "simulator_rules": [],
        },
    )
    backend = _assert_all_same(col, monkeypatch)
    moved = rwkv_scheduler._stable_preset_id("addon:test:moved")
    assert {
        review.identity.card_id
        for review in backend.reviews
        if review.identity.preset_id == moved
    } == {card_ids[3], card_ids[9]}


def test_a_collection_the_backend_reads_another_way_is_read_in_python(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer = _reviewer(col)
    # a card whose home deck is gone: Python gives it no preset of a deck
    col.db.execute(
        "update cards set did = 987654321 where id = (select min(id) from cards)"
    )
    assert (
        rwkv_scheduler._backend_historical_rwkv_review_inputs(
            reviewer,
            replay_key="",
            first_review_elapsed_source=RwkvFirstReviewElapsedSource.DECK_CONFIG,
            ignored_review_ids=frozenset(),
            hash_history=True,
            prepare_recovery_checkpoint=False,
            steps=rwkv_scheduler._RwkvPreparationSteps(None),
            progress=None,
        )
        is None
    )
    built = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    assert built.review_count > 100
    _assert_same(built, _reference_inputs(reviewer))


def test_dynamic_preset_replay_routes_reviews_as_python_did(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dynamic preset replay is read in Python and encoded by the backend:
    a simulator rule moves a card's reviews to its preset while the card's
    review count and previous interval lie in its bounds."""
    card_ids = sorted(col.db.list("select id from cards"))
    col.set_config(
        "fsrsPresetOverlay",
        {
            "presets": [
                {
                    "id": "addon:test:young",
                    "name": "Young",
                    "params": [],
                    "desired_retention": 0.9,
                    "historical_retention": 0.9,
                }
            ],
            "rules": [],
            "simulator_rules": [
                {
                    "search": f"cid:{','.join(map(str, card_ids[:20]))}",
                    "preset_id": "addon:test:young",
                    "min_reps": 1,
                    "max_interval_days": 2.5,
                },
                {
                    "preset_id": "addon:test:young",
                    "max_reps": 0,
                },
            ],
        },
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: True,
    )
    reviewer = _reviewer(col)
    assert (
        rwkv_scheduler._backend_historical_rwkv_review_inputs(
            reviewer,
            replay_key="",
            first_review_elapsed_source=RwkvFirstReviewElapsedSource.DECK_CONFIG,
            ignored_review_ids=frozenset(),
            hash_history=True,
            prepare_recovery_checkpoint=False,
            steps=rwkv_scheduler._RwkvPreparationSteps(None),
            progress=None,
        )
        is None
    )
    built = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    reference = _reference_inputs(reviewer)
    _assert_same(built, reference)
    young = rwkv_scheduler._stable_preset_id("addon:test:young")
    routed = [review for review in built.reviews if review.identity.preset_id == young]
    assert routed and len(routed) < len(built.reviews)


def test_an_unknown_feature_layout_fails(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder is keyed by the model's input layout: a layout it has no
    encoder for gives no inputs, rather than another layout's."""
    monkeypatch.setattr(rwkv_scheduler, "_RWKV_FEATURE_LAYOUT", "another-layout")
    with pytest.raises(Exception, match="unknown RWKV feature layout"):
        rwkv_scheduler._historical_rwkv_review_inputs(_reviewer(col))


def test_the_replay_reviews_are_a_sequence_of_inputs(col: Collection) -> None:
    """The backend's reviews are made into inputs only where one is asked
    for; a slice is a view of the same rows, and the view equals the inputs
    it holds, as a list of them would."""
    history = rwkv_scheduler._historical_rwkv_review_inputs(_reviewer(col))
    reviews = history.reviews
    assert isinstance(reviews, RwkvReplayReviews)
    inputs = list(reviews)
    assert len(inputs) == len(reviews) > 10
    assert reviews[-1] == inputs[-1] and reviews[3] == inputs[3]
    assert reviews[2:9] == inputs[2:9] and inputs[2:9] == reviews[2:9]
    assert isinstance(reviews[2:9], RwkvReplayReviews)
    assert reviews[2:9][1:3] == inputs[3:5]
    assert reviews[::3] == inputs[::3]
    assert reviews[5:2] == [] and len(reviews[5:2]) == 0
    assert reviews != inputs[:-1]
    assert reviews == reviews[:] and reviews[1:] != reviews[:-1]
    with pytest.raises(IndexError):
        reviews[len(inputs)]
    assert inputs.index(reviews[7]) == 7
    # the answered reviews, read off the rows' presence bits
    answered = [
        review_id
        for review_id, review in zip(history.review_ids, inputs, strict=True)
        if review.ease is not None
    ]
    assert reviews.answered_review_ids(history.review_ids) == answered
    assert reviews[3:8].answered_review_ids(history.review_ids[3:8]) == answered[3:8]
    assert _PACKED_PREDICTION_REQUEST_ROW.size == rs._RWKV_PACKED_REVIEW_ROW.size
    assert bytes(reviews[2:4].packed_warm_up_rows()) == b"".join(
        _packed_review_input_row(review) for review in inputs[2:4]
    )


def test_the_fingerprint_accepts_the_backends_history(col: Collection) -> None:
    """The state cache stores the backend's history hash and validates it
    with the fingerprint, which hashes through the same encoder."""
    reviewer = _reviewer(col)
    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    fingerprint = rwkv_scheduler._rwkv_historical_review_fingerprint(
        reviewer,
        expected_identity=rwkv_scheduler._RwkvHistoryPrefixIdentity(
            last_review_id=history.last_review_id,
            review_count=history.review_count,
            history_hash=history.history_hash,
        ),
    )
    assert fingerprint is not None and fingerprint.history_is_valid


def test_a_stopped_caller_does_not_start_the_backend_build(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = col._backend.rwkv_historical_review_inputs_raw
    monkeypatch.setattr(
        col._backend,
        "rwkv_historical_review_inputs_raw",
        lambda message: calls.append(1) or real(message),
    )

    def stop() -> None:
        raise InterruptedError()

    with pytest.raises(InterruptedError):
        rwkv_scheduler._historical_rwkv_review_inputs(
            _reviewer(col), between_steps=stop
        )
    assert calls == []


def test_a_state_cache_of_another_model_is_not_read(col: Collection) -> None:
    """Pins spec/ui.md#ui.rwkv-curve-stored-s90: the state cache, and with it
    the stored curves and their S90s, names the model (its SHA-256); a cache
    of another model is not read, so no curve or S90 of that model is
    used."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    metadata = rwkv_scheduler._rwkv_state_cache_metadata(
        reviewer, history, snapshot_review_id=history.last_review_id
    )
    model = metadata["model"]
    assert isinstance(model, dict) and model.get("sha256")

    def compatible(metadata: dict[str, object]) -> bool:
        return rwkv_scheduler._rwkv_state_cache_metadata_compatible(reviewer, metadata)

    assert compatible(metadata)
    assert not compatible({**metadata, "model": {**model, "sha256": "0" * 64}})


def test_a_state_cache_of_another_feature_layout_is_not_read(col: Collection) -> None:
    """The state cache names the feature layout its states were replayed
    with. A cache written before the name existed holds today's layout and
    stays usable; a cache of another layout is rebuilt."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    metadata = rwkv_scheduler._rwkv_state_cache_metadata(
        reviewer, history, snapshot_review_id=history.last_review_id
    )
    assert metadata["featureLayout"] == "published-92"

    def compatible(metadata: dict[str, object]) -> bool:
        return rwkv_scheduler._rwkv_state_cache_metadata_compatible(reviewer, metadata)

    assert compatible(metadata)
    untagged = {key: value for key, value in metadata.items() if key != "featureLayout"}
    assert compatible(untagged)
    assert not compatible({**metadata, "featureLayout": "another-layout"})


def test_the_backend_build_keeps_the_card_presets_python_keeps(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Python read of the presets leaves every history card's preset in
    `_resolved_preset_id_cache`, which the preset-routing check of a changed
    card reads; the backend's own read leaves the same."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    key = rwkv_scheduler._preset_id_cache_key(reviewer)
    kept = []
    for backend in (True, False):
        rwkv_scheduler._resolved_preset_id_cache.pop(key, None)
        with monkeypatch.context() as patch:
            if not backend:
                patch.setattr(
                    rwkv_scheduler,
                    "_backend_historical_rwkv_review_inputs",
                    lambda *_args, **_kwargs: None,
                )
            rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
        kept.append(dict(rwkv_scheduler._resolved_preset_id_cache[key]))
    assert len(kept[0]) > 30
    assert kept[0] == kept[1]


def test_a_card_preset_kept_from_before_is_read_in_python(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python reads with the presets it kept; where one differs from the
    collection's, the backend's own read would differ, so Python reads the
    rows and the presets itself."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    key = rwkv_scheduler._preset_id_cache_key(reviewer)
    card_id = min(col.db.list("select id from cards"))
    rwkv_scheduler._resolved_preset_id_cache[key] = {card_id: "424242"}
    try:
        built = rwkv_scheduler._backend_historical_rwkv_review_inputs(
            reviewer,
            replay_key="",
            first_review_elapsed_source=RwkvFirstReviewElapsedSource.DECK_CONFIG,
            ignored_review_ids=frozenset(),
            hash_history=True,
            prepare_recovery_checkpoint=False,
            steps=rwkv_scheduler._RwkvPreparationSteps(None),
            progress=None,
        )
        assert built is None
        assert rwkv_scheduler._resolved_preset_id_cache[key] == {card_id: "424242"}
        history = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
        assert {
            review.identity.preset_id
            for review in history.reviews
            if review.identity.card_id == card_id
        } == {424242}
    finally:
        rwkv_scheduler._resolved_preset_id_cache.pop(key, None)


# --- The parity oracle -------------------------------------------------------
#
# `_reference_inputs` is the Python build of the replay inputs that the
# backend's builder replaced (`_historical_rwkv_review_inputs` before
# rslib/src/scheduler/rwkv_inputs built every read), kept here line for line
# as the reference the builder must match, field by field and bit for bit.
# Nothing in the app runs it. It reads the rows and the presets with the same
# helpers the app still uses for a read the backend does not do itself.


def _reference_preset_id_for_review(
    rules: list[Any],
    *,
    card_id: int,
    interval_days: int,
    review_count: int,
) -> str | None:
    for rule in rules:
        if rule.card_ids is not None and card_id not in rule.card_ids:
            continue
        if rule.min_reps is not None and review_count < rule.min_reps:
            continue
        if rule.max_reps is not None and review_count > rule.max_reps:
            continue
        if (
            rule.min_interval_days is not None
            and interval_days < rule.min_interval_days
        ):
            continue
        if (
            rule.max_interval_days is not None
            and interval_days > rule.max_interval_days
        ):
            continue
        return rule.preset_id
    return None


def _reference_recovery_cutoff_review_id(
    raw_rows: Sequence[Sequence[object]],
    after_review_id: int | None,
) -> int | None:
    for raw_row_index in range(len(raw_rows) - 1, -1, -1):
        row = raw_rows[raw_row_index]
        if (
            rs._retained_historical_review_state(row) is None
            or len(row) < 9
            or not isinstance(row[0], int)
            or (after_review_id is not None and row[0] <= after_review_id)
        ):
            continue
        return row[0] - rs._RWKV_STATE_CACHE_CHECKPOINT_MAX_AGE_MILLIS
    return None


def _reference_inputs(  # noqa: PLR0912, PLR0913, PLR0915
    reviewer: object,
    *,
    after_review_id: int | None = None,
    deck_id: int | None = None,
    previous_review_id_by_card: dict[int, int] | None = None,
    previous_interval_days_by_card: dict[int, int] | None = None,
    review_count_by_card: dict[int, int] | None = None,
    previous_history_hash: str | None = None,
    previous_replay_key: str | None = None,
    first_review_elapsed_source: RwkvFirstReviewElapsedSource = (
        RwkvFirstReviewElapsedSource.DECK_CONFIG
    ),
    ignored_review_ids: frozenset[int] = frozenset(),
    prepare_recovery_checkpoint: bool = False,
    hash_history: bool = True,
    **_ignored: Any,
) -> RwkvHistoricalReviewInputs:
    previous_ids = dict(previous_review_id_by_card or {})
    previous_intervals = dict(previous_interval_days_by_card or {})
    review_counts = dict(review_count_by_card or {})
    replay_key = rs._rwkv_replay_semantics_key(
        reviewer, first_review_elapsed_source=first_review_elapsed_source
    )
    history_hash = (
        previous_history_hash
        if previous_history_hash is not None
        else rs._RWKV_STATE_CACHE_EMPTY_HISTORY_HASH
    )
    have_previous_state = all(
        value is not None
        for value in (
            previous_review_id_by_card,
            previous_interval_days_by_card,
            review_count_by_card,
        )
    )
    if after_review_id is not None and have_previous_state:
        if not rs._historical_rwkv_review_rows(
            reviewer,
            after_review_id=after_review_id,
            deck_id=deck_id,
            limit=1,
            ignored_review_ids=ignored_review_ids,
        ):
            return RwkvHistoricalReviewInputs(
                reviews=[],
                review_ids=[],
                previous_review_id_by_card=previous_ids,
                previous_interval_days_by_card=previous_intervals,
                review_count_by_card=review_counts,
                last_review_id=after_review_id,
                review_count=sum(review_counts.values()),
                deck_id=deck_id,
                history_hash=history_hash,
                replay_key=replay_key,
                ignored_review_ids=rs._active_ignored_review_ids(
                    reviewer, ignored_review_ids
                ),
            )
    timing = rs._timing_today(reviewer)
    days_elapsed = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    assert isinstance(days_elapsed, int) and isinstance(next_day_at, int)

    incremental = after_review_id is not None and have_previous_state
    raw_rows = list(
        rs._historical_rwkv_review_rows(
            reviewer,
            after_review_id=after_review_id if incremental else None,
            deck_id=deck_id,
            ignored_review_ids=ignored_review_ids,
        )
    )
    active_ignored_review_ids = rs._active_ignored_review_ids(
        reviewer, ignored_review_ids
    )
    recovery_cutoff_review_id = (
        _reference_recovery_cutoff_review_id(raw_rows, after_review_id)
        if prepare_recovery_checkpoint
        else None
    )
    retained_states = [rs._retained_historical_review_state(row) for row in raw_rows]

    def retained_rows() -> Iterator[Sequence[object]]:
        for index, row in enumerate(raw_rows):
            if retained_states[index] is None:
                continue
            if after_review_id is not None and (
                not isinstance(row[0], int) or row[0] <= after_review_id
            ):
                continue
            yield row

    dynamic_preset_replay = rs._rwkv_dynamic_preset_replay_enabled_for_collection(
        reviewer
    )
    historical_preset_rules = (
        rs._historical_preset_rules(reviewer) if dynamic_preset_replay else []
    )
    deck_configs_by_deck_id = rs._historical_deck_configs_by_deck_id(
        reviewer, retained_rows()
    )
    preset_id_by_card = rs._historical_deck_config_ids_by_card(
        reviewer, retained_rows(), deck_configs_by_deck_id=deck_configs_by_deck_id
    )
    preset_id_by_card.update(
        rs._resolved_fsrs_preset_ids(
            reviewer,
            rs._historical_rwkv_review_card_ids(
                row for row in retained_rows() if len(row) >= 4 and row[3] is not None
            ),
        )
    )
    reviews: list[RwkvReviewInput] = []
    review_ids: list[int] = []
    last_review_id = after_review_id or 0
    retained_review_count = sum(review_counts.values()) if incremental else 0
    review_count = retained_review_count
    prepared_checkpoint_histories: dict[int, RwkvHistoricalReviewInputs] = {}
    for raw_row_index, row in enumerate(raw_rows):
        historical_state = retained_states[raw_row_index]
        if historical_state is None:
            continue
        review_id_value = row[0] if row else None
        if isinstance(review_id_value, int):
            retained_review_count += 1
            if review_id_value <= last_review_id:
                review_count = retained_review_count
        if after_review_id is not None and (
            not isinstance(review_id_value, int) or review_id_value <= after_review_id
        ):
            continue
        if len(row) < 9:
            continue
        (
            review_id,
            card_id,
            note_id,
            row_deck_id,
            ease,
            duration_millis,
            review_kind,
            interval_days,
            ease_factor,
        ) = row[:9]
        if not (
            isinstance(review_id, int)
            and isinstance(card_id, int)
            and (note_id is None or isinstance(note_id, int))
            and (row_deck_id is None or isinstance(row_deck_id, int))
            and isinstance(ease, int)
            and isinstance(duration_millis, int)
            and isinstance(review_kind, int)
            and isinstance(interval_days, int)
            and isinstance(ease_factor, int)
        ):
            continue
        if (
            recovery_cutoff_review_id is not None
            and review_id > recovery_cutoff_review_id
            and reviews
            and not prepared_checkpoint_histories
        ):
            checkpoint_review_count = len(reviews)
            prepared_checkpoint_histories[checkpoint_review_count] = (
                RwkvHistoricalReviewInputs(
                    reviews=[],
                    review_ids=[],
                    previous_review_id_by_card=dict(previous_ids),
                    previous_interval_days_by_card=dict(previous_intervals),
                    review_count_by_card=dict(review_counts),
                    last_review_id=review_ids[-1],
                    review_count=checkpoint_review_count,
                    deck_id=deck_id,
                    history_hash=history_hash if hash_history else "",
                    replay_key=replay_key,
                    ignored_review_ids=active_ignored_review_ids,
                )
            )
        day_offset = rs._historical_review_day_offset(
            review_id, days_elapsed=days_elapsed, next_day_at=next_day_at
        )
        previous_review_id = previous_ids.get(card_id)
        if previous_review_id is not None:
            elapsed_seconds = max(0, (review_id - previous_review_id) // 1000)
            elapsed_days = max(
                0,
                day_offset
                - rs._historical_review_day_offset(
                    previous_review_id,
                    days_elapsed=days_elapsed,
                    next_day_at=next_day_at,
                ),
            )
        else:
            deck_config = deck_configs_by_deck_id.get(row_deck_id)
            elapsed_source = first_review_elapsed_source
            if elapsed_source == RwkvFirstReviewElapsedSource.DECK_CONFIG:
                elapsed_source = (
                    RwkvFirstReviewElapsedSource.CARD_CREATION
                    if isinstance(deck_config, dict)
                    and rs._rwkv_review_first_review_elapsed_from_card_creation(
                        deck_config
                    )
                    else RwkvFirstReviewElapsedSource.MISSING
                )
            elapsed_seconds = (
                max(0, (review_id - card_id) // 1000)
                if elapsed_source == RwkvFirstReviewElapsedSource.CARD_CREATION
                and historical_state == int(rs.RwkvReviewState.LEARN_START)
                and review_kind == 0
                else -1
            )
            elapsed_days = elapsed_seconds // 86_400 if elapsed_seconds >= 0 else -1
        review_count_so_far = review_counts.get(card_id, 0)
        historical_interval_days = previous_intervals.get(card_id, 0)
        historical_preset_id = _reference_preset_id_for_review(
            historical_preset_rules,
            card_id=card_id,
            interval_days=historical_interval_days,
            review_count=review_count_so_far,
        )
        if row_deck_id is None:
            base_preset_id: int | str | None = None
        elif historical_preset_id is not None:
            base_preset_id = historical_preset_id
        else:
            base_preset_id = preset_id_by_card[card_id]
        preset_id = (
            rs._stable_preset_id(str(base_preset_id))
            if base_preset_id is not None
            else None
        )
        previous_ids[card_id] = review_id
        previous_intervals[card_id] = interval_days
        review_counts[card_id] = review_count_so_far + 1
        last_review_id = max(last_review_id, review_id)
        review_count = retained_review_count
        state_kind, normal_state_kind = rs._historical_review_state_kinds(review_kind)
        review_input = RwkvReviewInput(
            identity=RwkvReviewIdentity(
                card_id=card_id,
                note_id=note_id,
                deck_id=row_deck_id,
                preset_id=preset_id,
            ),
            is_query=False,
            ease=ease,
            duration_millis=duration_millis,
            card_type=historical_state,
            card_queue=rs._historical_review_queue(review_kind),
            card_due=None,
            interval_days=interval_days,
            ease_factor=ease_factor,
            reps=None,
            lapses=None,
            day_offset=day_offset,
            current_state_kind=state_kind,
            current_normal_state_kind=normal_state_kind,
            current_elapsed_days=elapsed_days,
            current_elapsed_seconds=elapsed_seconds,
        )
        review_ids.append(review_id)
        reviews.append(review_input)
        if hash_history:
            history_hash = rs._rwkv_history_hash_after_review(
                history_hash, review_id, review_input
            )
    return RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=review_ids,
        previous_review_id_by_card=previous_ids,
        previous_interval_days_by_card=previous_intervals,
        review_count_by_card=review_counts,
        last_review_id=last_review_id,
        review_count=review_count,
        deck_id=deck_id,
        history_hash=history_hash if hash_history else "",
        replay_key=replay_key,
        ignored_review_ids=active_ignored_review_ids,
        prepared_checkpoint_histories=prepared_checkpoint_histories,
    )

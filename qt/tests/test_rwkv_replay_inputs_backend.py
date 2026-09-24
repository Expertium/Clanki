# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The whole history's replay inputs, built by the backend, are the ones
Python builds, value for value.

`_historical_rwkv_review_inputs` of the whole history from nothing comes from
the backend (`rslib/src/scheduler/rwkv_inputs`); every other read, and every
collection the backend would read another way, stays in Python. The state
cache, the recording pass and Total Knowledge all read
these inputs, so each of their ways of asking is compared here with the
Python build of the same collection.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from anki.collection import Collection
from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import (
    RwkvFirstReviewElapsedSource,
    RwkvHistoricalReviewInputs,
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
    review and one after it."""
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


def _both(
    col: Collection, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> tuple[RwkvHistoricalReviewInputs, RwkvHistoricalReviewInputs]:
    """The inputs built by the backend, and by Python."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    built = []
    real = rwkv_scheduler._backend_historical_rwkv_review_inputs

    def backend(*args: Any, **inner: Any) -> RwkvHistoricalReviewInputs | None:
        result = real(*args, **inner)
        built.append(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(rwkv_scheduler, "_backend_historical_rwkv_review_inputs", backend)
        from_backend = rwkv_scheduler._historical_rwkv_review_inputs(reviewer, **kwargs)
    assert built and built[0] is from_backend, "the backend did not build the inputs"
    with monkeypatch.context() as patch:
        patch.setattr(
            rwkv_scheduler,
            "_backend_historical_rwkv_review_inputs",
            lambda *_args, **_kwargs: None,
        )
        from_python = rwkv_scheduler._historical_rwkv_review_inputs(reviewer, **kwargs)
    return from_backend, from_python


def _assert_same(
    backend: RwkvHistoricalReviewInputs, python: RwkvHistoricalReviewInputs
) -> None:
    assert backend == python
    # the fields equality leaves out, and what it cannot see: the order of
    # the per-card maps, and bool against int
    assert backend.prepared_checkpoint_histories == python.prepared_checkpoint_histories
    for field in (
        "previous_review_id_by_card",
        "previous_interval_days_by_card",
        "review_count_by_card",
    ):
        assert list(getattr(backend, field).items()) == list(
            getattr(python, field).items()
        )
    for backend_review, python_review in zip(
        backend.reviews, python.reviews, strict=True
    ):
        assert repr(backend_review) == repr(python_review)
        assert [type(value) for value in vars(backend_review).values()] == [
            type(value) for value in vars(python_review).values()
        ]


def test_the_backend_builds_the_whole_history_as_python_does(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, python = _both(col, monkeypatch)
    assert backend.review_count > 100
    # the history has what makes the fields differ from review to review
    assert {review.card_type for review in backend.reviews} >= {0, 2, 3, 4}
    assert len({review.identity.preset_id for review in backend.reviews}) == 2
    assert any(review.current_elapsed_days == -1 for review in backend.reviews)
    assert any(review.current_elapsed_seconds > 86_400 for review in backend.reviews)
    assert len({review.day_offset for review in backend.reviews}) > 10
    _assert_same(backend, python)


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
    backend, python = _both(col, monkeypatch, **kwargs)
    _assert_same(backend, python)
    if kwargs.get("prepare_recovery_checkpoint"):
        assert backend.prepared_checkpoint_histories


def test_ignored_reviews_leave_the_history_before_its_start_rows(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins spec sched.rwkv-replay-start-row: the ignored reviews leave the
    rows before each card's start row is found, in the backend's build and in
    Python's, as the fingerprint drops them. An ignored start row is no
    start, so its card starts at another row, and the fingerprint accepts
    the history both builds give."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    whole = rwkv_scheduler._historical_rwkv_review_inputs(reviewer)
    starts = [
        review_id
        for review_id, review in zip(whole.review_ids, whole.reviews, strict=True)
        if review.card_type == 0
    ]
    # a start row, two plain rows, and a review that is not in the history
    ignored = frozenset({starts[1], whole.review_ids[5], whole.review_ids[-1], 12345})
    backend, python = _both(col, monkeypatch, ignored_review_ids=ignored)
    _assert_same(backend, python)
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
    backend, python = _both(col, monkeypatch)
    _assert_same(backend, python)
    moved = rwkv_scheduler._stable_preset_id("addon:test:moved")
    assert {
        review.identity.card_id
        for review in backend.reviews
        if review.identity.preset_id == moved
    } == {card_ids[3], card_ids[9]}


def test_a_collection_the_backend_reads_another_way_is_built_in_python(
    col: Collection,
) -> None:
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    python_only = []
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
    python_only.append(rwkv_scheduler._historical_rwkv_review_inputs(reviewer))
    assert python_only[0].review_count > 100


def test_dynamic_preset_replay_is_built_in_python(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler,
        "_rwkv_dynamic_preset_replay_enabled_for_collection",
        lambda reviewer: True,
    )
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
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


def test_the_fingerprint_accepts_the_backends_history(col: Collection) -> None:
    """The state cache stores the backend's history hash and validates it
    with the fingerprint, which hashes through the same encoder."""
    reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
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
    real = col._backend.rwkv_historical_review_inputs
    monkeypatch.setattr(
        col._backend,
        "rwkv_historical_review_inputs",
        lambda **kwargs: calls.append(1) or real(**kwargs),
    )

    def stop() -> None:
        raise InterruptedError()

    with pytest.raises(InterruptedError):
        rwkv_scheduler._historical_rwkv_review_inputs(
            SimpleNamespace(mw=SimpleNamespace(col=col)), between_steps=stop
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
    """The Python build leaves every history card's preset in
    `_resolved_preset_id_cache`, which the preset-routing check of a changed
    card reads; the backend build leaves the same."""
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


def test_a_card_preset_kept_from_before_is_built_in_python(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python builds with the presets it kept; where one differs from the
    collection's, the backend's inputs would differ, so Python builds them."""
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
